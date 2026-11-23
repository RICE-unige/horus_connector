#!/usr/bin/env python3
"""Resolve the pinned standalone ROS 2 DDS bridge and upstream archive digest."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.request import Request, urlopen


MANIFEST = Path(__file__).resolve().parents[1] / "config/zenoh-release.json"
REPOSITORY = "https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds"
X86_GNU = "x86_64-unknown-linux-gnu"
X86_MUSL = "x86_64-unknown-linux-musl"


def load_release():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def resolve_artifact(version, target, download_base="", *, release=None, fetch=None, glibc=""):
    release = load_release() if release is None else release
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("ZENOH_VERSION must name an exact stable release, such as 1.10.2")
    if target not in {X86_GNU, "aarch64-unknown-linux-gnu"}:
        raise ValueError(f"Unsupported bridge target: {target}")
    compat = release.get("x86_64_compat")
    host_version = re.fullmatch(r"glibc ([0-9]+(?:\.[0-9]+)+)", glibc.strip())
    compat_selected = (
        target == X86_GNU and version == release["version"] and compat and host_version
        and tuple(map(int, host_version[1].split("."))) < tuple(map(int, compat["native_min_glibc"].split(".")))
    )
    if compat_selected:
        target = X86_MUSL
    archive = f"zenoh-plugin-ros2dds-{version}-{target}-standalone.zip"
    base = f"{REPOSITORY}/releases/download/{version}"
    digest = compat["sha256"] if compat_selected else (
        release.get("sha256", {}).get(target) if version == release["version"] else None
    )
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


def stage_compat_runtime(staging, release):
    """Add the pinned musl loader and libgcc without installing OS packages."""
    compat = release["x86_64_compat"]
    digest_key = hashlib.sha256(
        "\n".join([compat["sha256"], *(entry["sha256"] for entry in compat["runtime"])]).encode("ascii")
    ).hexdigest()
    runtime_dir = staging / ".zenoh-compat" / digest_key
    runtime_dir.mkdir(parents=True)
    downloaded = staging / "runtime.apk"
    for entry in compat["runtime"]:
        subprocess.run(
            ["curl", "-fLsS", "--retry", "3", "--retry-max-time", "120", "--connect-timeout", "15",
             "--max-time", "90", "--max-filesize", "4194304", "-o", str(downloaded), entry["url"]],
            check=True,
        )
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Zenoh runtime checksum failed for {entry['url']}")
        # APK v2 contains concatenated signature, control and data tar streams.
        with tarfile.open(downloaded, "r:gz", ignore_zeros=True) as package:
            member = package.getmember(entry["member"])
            if not member.isfile() or not 0 < member.size < 4 * 1024 * 1024:
                raise ValueError(f"Invalid Zenoh runtime member {member.name}")
            library = runtime_dir / Path(member.name).name
            with package.extractfile(member) as contents:
                library.write_bytes(contents.read())
            library.chmod(0o755 if library.name.startswith("ld-musl-") else 0o644)
    downloaded.unlink()
    wrapper = staging / "zenoh-bridge-ros2dds"
    wrapper.rename(runtime_dir / "bridge")
    wrapper.write_text(
        "#!/bin/sh\n"
        'root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1\n'
        f'runtime="$root/.zenoh-compat/{digest_key}"\n'
        'exec "$runtime/ld-musl-x86_64.so.1" --library-path "$runtime" "$runtime/bridge" "$@"\n',
        encoding="ascii",
    )
    wrapper.chmod(0o755)
    return digest_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-version", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--target")
    parser.add_argument("--download-base", default="")
    parser.add_argument("--glibc", default="")
    parser.add_argument("--stage-compat-runtime", type=Path)
    args = parser.parse_args()
    try:
        release = load_release()
        if args.default_version:
            print(release["version"])
            return
        if args.stage_compat_runtime is not None:
            print(stage_compat_runtime(args.stage_compat_runtime, release))
            return
        if not args.target:
            parser.error("--target is required to resolve an archive")
        print("\t".join(resolve_artifact(args.version or release["version"], args.target,
                                        args.download_base, release=release, glibc=args.glibc)))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f"Cannot resolve Zenoh release: {error}\n")


if __name__ == "__main__":
    main()
