#!/usr/bin/env python3
"""Resolve the pinned standalone ROS 2 DDS bridge and upstream archive digest."""

import argparse
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen


MANIFEST = Path(__file__).resolve().parents[1] / "config/zenoh-release.json"
REPOSITORY = "https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds"


def load_release():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def resolve_artifact(version, target, download_base="", *, release=None, fetch=None):
    release = load_release() if release is None else release
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("ZENOH_VERSION must name an exact stable release, such as 1.10.2")
    if target not in {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"}:
        raise ValueError(f"Unsupported bridge target: {target}")
    archive = f"zenoh-plugin-ros2dds-{version}-{target}-standalone.zip"
    base = f"{REPOSITORY}/releases/download/{version}"
    digest = release.get("sha256", {}).get(target) if version == release["version"] else None
    if not digest:
        metadata_url = f"https://api.github.com/repos/eclipse-zenoh/zenoh-plugin-ros2dds/releases/tags/{version}"
        if fetch is None:
            def fetch(url):
                request = Request(url, headers={"User-Agent": "HORUS-Connector"})
                with urlopen(request, timeout=30) as response:
                    payload = response.read(2 * 1024 * 1024 + 1)
                if len(payload) > 2 * 1024 * 1024:
                    raise ValueError("Zenoh release metadata exceeds its size limit")
                return json.loads(payload)
        metadata = fetch(metadata_url)
        if metadata.get("tag_name") != version or metadata.get("draft") or metadata.get("prerelease"):
            raise ValueError(f"No published stable Zenoh release matches {version}")
        asset = next((asset for asset in metadata.get("assets", []) if asset.get("name") == archive), None)
        if asset is None:
            raise ValueError(f"Release {version} has no {archive}")
        digest = str(asset.get("digest", "")).removeprefix("sha256:")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError(f"No upstream SHA-256 digest is available for {archive}")
    return archive, f"{(download_base or base).rstrip('/')}/{archive}", digest.lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-version", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--target")
    parser.add_argument("--download-base", default="")
    args = parser.parse_args()
    try:
        release = load_release()
        if args.default_version:
            print(release["version"])
            return
        if not args.target:
            parser.error("--target is required to resolve an archive")
        print("\t".join(resolve_artifact(args.version or release["version"], args.target,
                                        args.download_base, release=release)))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot resolve Zenoh release: {error}\n")


if __name__ == "__main__":
    main()
