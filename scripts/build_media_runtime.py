#!/usr/bin/env python3
"""Build the pinned GCC/pacing plugin in an isolated, removable runtime."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(work, output, jobs=3, toolchain="stable"):
    if not 1 <= jobs <= 16:
        raise ValueError("jobs must be between 1 and 16")
    manifest = json.loads((ROOT / "config/media-runtime-source.json").read_text())
    work, output = work.resolve(), output.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    if not source.exists():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", manifest["version"],
                        manifest["repository"], str(source)], check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != manifest["commit"] or digest(source / "Cargo.lock") != manifest["cargo_lock_sha256"]:
        raise ValueError("Source/lockfile differs from the pinned runtime; use a fresh work directory")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source):
        raise ValueError("Runtime source has local changes; refusing to overwrite or build them")
    subprocess.run(["cargo", f"+{toolchain}", "build", "--locked", "--release", "-p", manifest["package"],
                    "--target-dir", str(work / "target"), "-j", str(jobs)], cwd=source, check=True)
    plugins = output / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    artifact = plugins / manifest["library"]
    temporary = artifact.with_suffix(".new")
    shutil.copy2(work / "target/release" / manifest["library"], temporary)
    os.replace(temporary, artifact)
    shutil.copy2(source / "LICENSE-MPL-2.0", output / "LICENSE-MPL-2.0")
    env = dict(os.environ, GST_PLUGIN_PATH=str(plugins) + (":" + os.environ["GST_PLUGIN_PATH"]
                                                        if os.environ.get("GST_PLUGIN_PATH") else ""))
    subprocess.run(["gst-inspect-1.0", "rtpgccbwe"], env=env, check=True)
    manifest["binary_sha256"] = digest(artifact)
    manifest["rustc"] = subprocess.check_output(["rustc", f"+{toolchain}", "--version"], text=True).strip()
    (output / "source.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Native media plugin ready at {plugins}; no system plugins were replaced.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/media-gcc")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--toolchain", default="stable")
    args = parser.parse_args()
    build(args.work_dir, args.output, args.jobs, args.toolchain)


if __name__ == "__main__":
    main()
