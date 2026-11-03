#!/usr/bin/env python3
"""Build the pinned independent-bulk DDS bridge without modifying system installs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from bridge_dependency import prepare

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments, cwd=None):
    return subprocess.run(arguments, cwd=cwd, check=True, text=True)


def build(work, output, jobs=4, toolchain="stable"):
    manifest = json.loads((ROOT / "config/zenoh-source.json").read_text())
    patch = ROOT / manifest["patch"]
    if digest(patch) != manifest["patch_sha256"]:
        raise ValueError("Bridge patch digest does not match its source manifest")
    if not 1 <= jobs <= 16:
        raise ValueError("jobs must be between 1 and 16")
    work = work.resolve()
    output = output.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    if not source.exists():
        run(["git", "clone", "--depth", "1", "--branch", manifest["version"],
             manifest["repository"], str(source)])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != manifest["commit"]:
        raise ValueError("Existing source checkout is not the pinned upstream commit; use a fresh work directory")
    for relative in manifest.get("new_files", []):
        if (source / relative).exists() and subprocess.run(
                ["git", "ls-files", "--error-unmatch", relative], cwd=source,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            run(["git", "add", "-N", "--", relative], source)
    current = subprocess.check_output(["git", "diff", "--binary"], cwd=source)
    if current:
        # Never reset an existing checkout or silently include unrelated source edits.
        if current != patch.read_bytes():
            raise ValueError("Existing checkout has unexpected edits; use a fresh work directory")
    else:
        run(["git", "apply", "--check", str(patch)], source)
        run(["git", "apply", str(patch)], source)
        for relative in manifest.get("new_files", []):
            run(["git", "add", "-N", "--", relative], source)
    dependency = prepare(ROOT, work, manifest["cyclors"])
    run(["cargo", f"+{toolchain}", "--config",
         'patch.crates-io.cyclors.path='+json.dumps(str(dependency)),
         "build", "--locked", "--release", "-p",
         "zenoh-bridge-ros2dds", "-j", str(jobs), "--target-dir", str(work / "target")], source)
    artifact = work / "target/release/zenoh-bridge-ros2dds"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".new")
    shutil.copy2(artifact, temporary)
    temporary.chmod(0o755)
    os.replace(temporary, output)
    manifest.update(binary_sha256=digest(output), cargo_lock_sha256=digest(source / "Cargo.lock"),
                    rustc=subprocess.check_output(["rustc", f"+{toolchain}", "--version"], text=True).strip())
    output.with_name(output.name + ".source.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", default=4, type=int)
    parser.add_argument("--toolchain", default="stable", help="An already installed Rust toolchain")
    args = parser.parse_args()
    build(args.work_dir, args.output, args.jobs, args.toolchain)


if __name__ == "__main__":
    main()
