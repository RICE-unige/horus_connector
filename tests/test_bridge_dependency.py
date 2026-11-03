"""Verify the pinned native serialization patch, including partial-copy bounds."""

import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bridge_dependency import prepare

ORIGINAL = '''static void cdds_to_ser(const struct ddsi_serdata *serdata_common, size_t off, size_t sz, void *buf)
{
  CY_DEBUG("Called <cdds_to_ser> \\n");
  CY_DEBUG_WA("Called <cdds_to_ser> offset = %zu\\n", off);
  CY_DEBUG_WA("Called <cdds_to_ser> size = %zu\\n", sz);
  CY_DEBUG_WA("Called <cdds_to_ser> buf = %p\\n", buf);
  struct cdds_ddsi_payload *pl = (struct cdds_ddsi_payload *)serdata_common;
  memcpy(buf, pl->payload, pl->size);
}

static struct ddsi_serdata *cdds_from_psmx (const struct ddsi_sertype *type, dds_loaned_sample_t *loaned_sample)
'''


def fixture(tmp_path):
    archive = tmp_path / "source.crate"
    with tarfile.open(archive, "w:gz") as output:
        entry = tarfile.TarInfo("cyclors-0.4.0/cyclocut/src/cdds_blob.c")
        payload = ORIGINAL.encode()
        entry.size = len(payload)
        output.addfile(entry, io.BytesIO(payload))
        entry = tarfile.TarInfo("cyclors-0.4.0/build.rs")
        payload = ('                    println!("cargo:rerun-if-changed=src/lib.rs");\n'
                   '                    println!("cargo:rerun-if-changed=src/functions.template");\n').encode()
        entry.size = len(payload)
        output.addfile(entry, io.BytesIO(payload))
    patch = ROOT / "patches/cyclors-0.4.0-serialized-slices.patch"
    return dict(version="0.4.0", url=archive.as_uri(), crate_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                patch=str(patch), patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest())


def test_private_dependency_is_repeatable_and_rejects_unexpected_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "empty-cargo"))
    spec = fixture(tmp_path)
    work = tmp_path / "work"
    source = prepare(ROOT, work, spec)
    assert prepare(ROOT, work, spec) == source
    assert 'rerun-if-changed=cyclocut' in (source / "build.rs").read_text()
    (source / "unexpected.txt").write_text("do not overwrite")
    with pytest.raises(ValueError, match="unexpected edits"):
        prepare(ROOT, work, spec)
    assert (source / "unexpected.txt").read_text() == "do not overwrite"


def test_bad_archive_checksum_never_reaches_build(tmp_path, monkeypatch):
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "empty-cargo"))
    spec = dict(fixture(tmp_path), crate_sha256="0"*64)
    with pytest.raises(ValueError, match="checksum"):
        prepare(ROOT, tmp_path / "work", spec)


def test_real_c_function_copies_only_requested_slice(tmp_path, monkeypatch):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler unavailable")
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "empty-cargo"))
    fixed = prepare(ROOT, tmp_path / "work", fixture(tmp_path))
    for label, text, succeeds in (("original", ORIGINAL, False),
                                  ("fixed", (fixed / "cyclocut/src/cdds_blob.c").read_text(), True)):
        function = re.search(r"static void cdds_to_ser\(.*?\n\}", text, re.S).group()
        source = tmp_path / (label+".c")
        source.write_text('''#include <assert.h>
#include <string.h>
#include <stddef.h>
#define CY_DEBUG(...)
#define CY_DEBUG_WA(...)
struct ddsi_serdata {int unused;};
struct cdds_ddsi_payload {struct ddsi_serdata sd; size_t size; unsigned char *payload;};
'''+function+'''
int main(void) {
  unsigned char input[1024], output[1056];
  for(size_t i=0; i<sizeof(input); ++i) input[i]=(unsigned char)i;
  struct cdds_ddsi_payload sample = {{0}, sizeof(input), input};
  memset(output, 0xa5, sizeof(output));
  cdds_to_ser(&sample.sd, 777, 17, output+8);
  for(size_t i=0; i<8; ++i) if(output[i]!=0xa5) return 1;
  if(memcmp(output+8,input+777,17)) return 2;
  for(size_t i=25; i<sizeof(output); ++i) if(output[i]!=0xa5) return 3;
  cdds_to_ser(&sample.sd, sizeof(input), 0, output+25);
  for(size_t i=25; i<sizeof(output); ++i) if(output[i]!=0xa5) return 4;
  cdds_to_ser(&sample.sd, 0, sizeof(input), output);
  if(memcmp(output,input,sizeof(input))) return 5;
  sample.size=1021;
  memset(output, 0xa5, sizeof(output));
  cdds_to_ser(&sample.sd,1019,5,output+8);
  if(memcmp(output+8,input+1019,2)) return 6;
  for(size_t i=10; i<13; ++i) if(output[i]!=0) return 7;
  for(size_t i=13; i<sizeof(output); ++i) if(output[i]!=0xa5) return 8;
  return 0;
}
''')
        binary = tmp_path / label
        subprocess.run([compiler, "-std=c11", "-O2", str(source), "-o", str(binary)], check=True, timeout=30)
        result = subprocess.run([str(binary)], timeout=5)
        assert (result.returncode == 0) == succeeds


def test_dependency_patch_and_upstream_archive_are_locked():
    spec = json.loads((ROOT / "config/zenoh-source.json").read_text())["cyclors"]
    assert len(spec["crate_sha256"]) == 64
    assert hashlib.sha256((ROOT / spec["patch"]).read_bytes()).hexdigest() == spec["patch_sha256"]
