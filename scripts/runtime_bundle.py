#!/usr/bin/env python3
"""Pack or install the connector's platform-specific native runtime."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tarfile
import tempfile
from bridge_dependency import digest as sha256


FILES = ("zenoh-bridge-ros2dds", "zenoh-bridge-ros2dds.source.json", "horus_media_node",
         "horus_unity_bridge_node", "libgstrsrtp.so")
LIMIT = 256 * 1024 * 1024
PAIRINGS = {"22.04": "humble", "24.04": "jazzy"}
REVISION = re.compile(r"[a-f0-9]{7,64}(?:-dirty)?")


def host_platform(ros):
    release = platform.freedesktop_os_release()
    arch = platform.machine()
    version = release.get("VERSION_ID")
    if release.get("ID") != "ubuntu" or arch not in {"x86_64", "aarch64"} or PAIRINGS.get(version) != ros:
        raise ValueError(f"No runtime for {release.get('ID')} {version}, {arch}, ROS {ros}")
    return {"ubuntu": version, "arch": arch, "ros": ros}


def validate(directory, expected, zenoh_pin, bridge_revision=None):
    manifest = json.loads((directory / "runtime.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("platform") != expected:
        raise ValueError(f"Runtime does not match this host ({expected})")
    revision = manifest.get("bridge_revision")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError("Runtime manifest is missing a valid HORUS ROS 2 revision")
    if bridge_revision is not None and revision != bridge_revision:
        raise ValueError(f"Expected HORUS ROS 2 revision {bridge_revision}, but runtime contains {revision}")
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(FILES):
        raise ValueError("Runtime manifest must specify each required component")
    for name, digest in hashes.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"Runtime component changed or missing: {name}")
    receipt = json.loads((directory / "zenoh-bridge-ros2dds.source.json").read_text())
    for field in ("version", "commit", "patch_sha256", "cyclors", "features"):
        if receipt.get(field) != zenoh_pin[field]:
            raise ValueError(f"Runtime was not built from this checkout's Zenoh pin ({field})")
    if receipt.get("binary_sha256") != hashes["zenoh-bridge-ros2dds"]:
        raise ValueError("Zenoh binary does not match its build receipt")
    return manifest


def pack(source, output, expected, zenoh_pin, bridge_revision):
    if not REVISION.fullmatch(bridge_revision):
        raise ValueError("Record the HORUS ROS 2 source revision used to build the runtime")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for name in FILES:
            shutil.copyfile(source / name, directory / name)
        (directory / "runtime.json").write_text(json.dumps({
            "schema": 1, "platform": expected, "bridge_revision": bridge_revision,
            "sha256": {name: sha256(directory / name) for name in FILES},
        }, indent=2) + "\n")
        validate(directory, expected, zenoh_pin)
        with tarfile.open(output, "x:gz") as archive:
            for name in ("runtime.json", *FILES):
                archive.add(directory / name, arcname=name, recursive=False)
    return sha256(output)


def install(archive_path, root, digest, expected, zenoh_pin, bridge_revision=None):
    if sha256(archive_path) != digest:
        raise ValueError("Runtime archive SHA-256 does not match the release checksum")
    root.mkdir(parents=True, exist_ok=True)
    final = root / digest
    if not final.exists():
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            directory = Path(tmp)
            with tarfile.open(archive_path, "r:gz") as archive:
                seen = set()
                for member in archive:
                    name = member.name
                    limit = 64 * 1024 if name.endswith(".json") else LIMIT
                    if name not in {"runtime.json", *FILES} or name in seen or not member.isfile() or not 0 < member.size <= limit:
                        raise ValueError(f"Unexpected or invalid runtime archive entry: {name!r}")
                    seen.add(name)
                    with archive.extractfile(member) as data, (directory / name).open("xb") as out:
                        shutil.copyfileobj(data, out)
                    (directory / name).chmod(0o600 if name.endswith(".json") else 0o700)
            validate(directory, expected, zenoh_pin, bridge_revision)
            try:
                os.rename(directory, final)
            except OSError:
                if not final.is_dir():
                    raise
    validate(final, expected, zenoh_pin, bridge_revision)
    return final.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "install", "resolve"))
    parser.add_argument("--ros", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--bridge-revision")
    args = parser.parse_args()
    try:
        expected = host_platform(args.ros)
        pin = json.loads((Path(__file__).resolve().parents[1] / "config/zenoh-source.json").read_text())
        if args.action == "pack":
            if not (args.source and args.archive and args.bridge_revision):
                parser.error("pack needs --source, --archive and --bridge-revision")
            print(pack(args.source, args.archive, expected, pin, args.bridge_revision))
        else:
            if not re.fullmatch(r"[a-f0-9]{64}", args.sha256 or ""):
                parser.error("Supply the lowercase SHA-256 published with the runtime bundle")
            if args.action == "install":
                if not args.archive:
                    parser.error("install needs --archive")
                directory = install(args.archive, args.root, args.sha256, expected, pin, args.bridge_revision)
            else:
                directory = (args.root / args.sha256).resolve()
                validate(directory, expected, pin, args.bridge_revision)
            print(directory)
    except (OSError, KeyError, TypeError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"HORUS runtime: {error}\n")


if __name__ == "__main__":
    main()
