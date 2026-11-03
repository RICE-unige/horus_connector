"""Exercise the real installer against tiny offline release archives."""

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import zenoh_release


def test_default_release_is_pinned_and_does_not_need_live_api():
    release = zenoh_release.load_release()
    for target, sha in release["sha256"].items():
        name, url, digest = zenoh_release.resolve_artifact(release["version"], target,
            fetch=lambda _: pytest.fail("pinned release contacted the live API"))
        assert name.endswith(f"{target}-standalone.zip")
        assert url.startswith(zenoh_release.REPOSITORY + "/releases/download/")
        assert digest == sha
    assert f"ZENOH_VERSION={release['version']}" in (ROOT / ".env.example").read_text()


def test_other_exact_releases_use_asset_digest_and_keep_mirror_support():
    target = "x86_64-unknown-linux-gnu"
    name = "zenoh-plugin-ros2dds-1.9.0-" + target + "-standalone.zip"
    record = {"tag_name": "1.9.0", "assets": [{"name": name, "digest": "sha256:" + "a" * 64}]}
    result = zenoh_release.resolve_artifact("1.9.0", target, "https://mirror.example/release/",
                                           fetch=lambda _: record)
    assert result == (name, "https://mirror.example/release/" + name, "a" * 64)
    record["assets"][0].pop("digest")
    with pytest.raises(ValueError, match="SHA-256"):
        zenoh_release.resolve_artifact("1.9.0", target, fetch=lambda _: record)


@pytest.mark.parametrize("version,target", [
    ("latest", "x86_64-unknown-linux-gnu"),
    ("1.10.2", "unsupported"),
    ("1.10.2;exit", "x86_64-unknown-linux-gnu"),
])
def test_invalid_release_selection_is_reported_before_network(version, target):
    with pytest.raises(ValueError):
        zenoh_release.resolve_artifact(version, target, fetch=lambda _: pytest.fail("unexpected network"))


def installer_fixture(tmp_path, *, staged_version="1.10.2", plugin=True, bad_checksum=False, old_version="1.9.0", startup_log=False):
    root = tmp_path / "connector with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("bootstrap.sh", "zenoh_release.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    archives = tmp_path / "archives"
    archives.mkdir()
    machine = platform.machine()
    target = {"x86_64": "x86_64", "aarch64": "aarch64"}[machine] + "-unknown-linux-gnu"
    archive = archives / f"zenoh-plugin-ros2dds-1.10.2-{target}-standalone.zip"
    with zipfile.ZipFile(archive, "w") as output:
        prefix = "printf '\\033[32m INFO zenoh_bridge_ros2dds: startup\\033[0m\\n'\n" if startup_log else ""
        output.writestr("zenoh-bridge-ros2dds", f"#!/bin/sh\n{prefix}printf 'zenoh-bridge-ros2dds v{staged_version}\\n'\n")
        if plugin:
            output.writestr("libzenoh_plugin_ros2dds.so", b"new matching plugin")
    digest = "0" * 64 if bad_checksum else hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / "config/zenoh-release.json").write_text(json.dumps({"version": "1.10.2", "sha256": {target: digest}}))
    binary = root / "zenoh-bridge-ros2dds"
    binary.write_text(f"#!/bin/sh\nprintf 'zenoh-bridge-ros2dds v{old_version}\\n'\n")
    binary.chmod(0o755)
    (root / "libzenoh_plugin_ros2dds.so").write_bytes(b"old matching plugin")
    (root / ".zenoh_profile.env").write_text("ZENOH_BRIDGE_IMAGE='old:1.9.0'\n")
    env = dict(os.environ, ZENOH_VERSION="1.10.2", ZENOH_DOWNLOAD_BASE=archives.as_uri())
    return root, env


def install(root, env, *, fail_profile=False):
    if fail_profile:
        command = ["bash", "-c", 'source "$1"; write_zenoh_profile() { return 1; }; install_zenoh',
                   "installer-test", str(root / "scripts/bootstrap.sh")]
    else:
        command = ["bash", str(root / "scripts/bootstrap.sh"), "--zenoh-only"]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("old_version", ["1.9.0", "1.10.20"])
def test_zenoh_only_update_uses_exact_version_and_preserves_rollback(tmp_path, old_version):
    root, env = installer_fixture(tmp_path, old_version=old_version)
    before = (root / "zenoh-bridge-ros2dds").read_bytes()
    result = install(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".env").exists(), "Zenoh-only update ran full environment/bootstrap setup"
    version = subprocess.check_output([root / "zenoh-bridge-ros2dds", "--version"], text=True).strip()
    assert version == "zenoh-bridge-ros2dds v1.10.2"
    assert (root / "libzenoh_plugin_ros2dds.so").read_bytes() == b"new matching plugin"
    backup, = (root / ".zenoh-backups").iterdir()
    assert (backup / "zenoh-bridge-ros2dds").read_bytes() == before
    assert (backup / "libzenoh_plugin_ros2dds.so").read_bytes() == b"old matching plugin"
    assert (backup / ".zenoh_profile.env").read_text() == "ZENOH_BRIDGE_IMAGE='old:1.9.0'\n"
    assert "ZENOH_BRIDGE_VERSION='1.10.2'" in (root / ".zenoh_profile.env").read_text()
    assert "eclipse/zenoh-bridge-ros2dds:1.10.2" in (root / ".zenoh_profile.env").read_text()
    shutil.rmtree(Path(env["ZENOH_DOWNLOAD_BASE"].removeprefix("file://")))
    repeat = install(root, env)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "already installed" in repeat.stdout
    assert len(list((root / ".zenoh-backups").iterdir())) == 1


@pytest.mark.parametrize("options", [
    {"bad_checksum": True}, {"plugin": False}, {"staged_version": "1.10.20"},
])
def test_invalid_archive_never_changes_existing_installation(tmp_path, options):
    root, env = installer_fixture(tmp_path, **options)
    paths = [root / name for name in ("zenoh-bridge-ros2dds", "libzenoh_plugin_ros2dds.so", ".zenoh_profile.env")]
    before = [path.read_bytes() for path in paths]
    result = install(root, env)
    assert result.returncode != 0
    assert [path.read_bytes() for path in paths] == before
    assert not [path for path in root.glob(".zenoh-install.*") if path.is_dir()]
    assert not list(root.glob(".zenoh-download.*"))


def test_upstream_stdout_startup_log_does_not_reject_matching_version(tmp_path):
    root, env = installer_fixture(tmp_path, startup_log=True)
    first = install(root, env)
    assert first.returncode == 0, first.stdout + first.stderr
    shutil.rmtree(Path(env["ZENOH_DOWNLOAD_BASE"].removeprefix("file://")))
    repeat = install(root, env)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "already installed" in repeat.stdout


def test_upstream_stdout_startup_log_still_rejects_wrong_version(tmp_path):
    root, env = installer_fixture(tmp_path, startup_log=True, staged_version="1.10.20")
    before = (root / "zenoh-bridge-ros2dds").read_bytes()
    result = install(root, env)
    assert result.returncode != 0
    assert (root / "zenoh-bridge-ros2dds").read_bytes() == before


def test_failed_final_profile_write_restores_binary_plugin_and_profile(tmp_path):
    root, env = installer_fixture(tmp_path)
    paths = [root / name for name in ("zenoh-bridge-ros2dds", "libzenoh_plugin_ros2dds.so", ".zenoh_profile.env")]
    before = [path.read_bytes() for path in paths]
    result = install(root, env, fail_profile=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert [path.read_bytes() for path in paths] == before
    assert "restored the previous installation" in result.stderr
