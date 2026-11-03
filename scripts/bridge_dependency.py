"""Prepare a checksum-locked, project-local Cyclors serialization fix."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_digest(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result.update(path.relative_to(root).as_posix().encode()+b"\0")
            result.update(bytes.fromhex(digest(path)))
    return result.hexdigest()


def prepare(root, work, spec):
    patch = root / spec["patch"]
    if digest(patch) != spec["patch_sha256"]:
        raise ValueError("Cyclors patch digest does not match its manifest")
    destination = work / "dependencies" / ("cyclors-"+spec["version"])
    receipt = destination.with_suffix(".receipt.json")
    import json
    if destination.exists():
        if not receipt.is_file():
            raise ValueError("Existing local Cyclors has no build receipt; use a fresh work directory")
        saved = json.loads(receipt.read_text())
        if saved.get("source") != spec or saved.get("tree_sha256") != tree_digest(destination):
            raise ValueError("Local Cyclors has unexpected edits; no dependency files were overwritten")
        return destination
    cache = Path(os.environ.get("CARGO_HOME", Path.home()/".cargo")) / "registry/cache"
    archives = list(cache.glob(f"*/cyclors-{spec['version']}.crate"))
    archive = next((p for p in archives if digest(p) == spec["crate_sha256"]), None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if archive is None:
        archive = destination.parent / (destination.name+".crate")
        if not archive.exists() or digest(archive) != spec["crate_sha256"]:
            temporary = archive.with_suffix(".download")
            with urllib.request.urlopen(spec["url"], timeout=60) as response, temporary.open("xb") as output:
                shutil.copyfileobj(response, output)
            if digest(temporary) != spec["crate_sha256"]:
                raise ValueError("Downloaded Cyclors archive failed checksum validation")
            os.replace(temporary, archive)
    with tarfile.open(archive) as package:
        # No Cargo registry source or system DDS installation is modified.
        package.extractall(destination.parent, filter="data")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=destination, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=destination, check=True)
    receipt.write_text(json.dumps(dict(source=spec, tree_sha256=tree_digest(destination)), indent=2)+"\n")
    return destination
