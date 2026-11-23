"""Exercise the real installer against tiny offline release archives."""

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
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
                                           fetch=lambda _: record, glibc="glibc 2.35")
    assert result == (name, "https://mirror.example/release/" + name, "a" * 64)
    record["assets"][0].pop("digest")
    with pytest.raises(ValueError, match="SHA-256"):
        zenoh_release.resolve_artifact("1.9.0", target, fetch=lambda _: record)


@pytest.mark.parametrize("target,glibc,musl", [
    (zenoh_release.X86_GNU, "glibc 2.35", True),
    (zenoh_release.X86_GNU, "glibc 2.38", False),
    (zenoh_release.X86_GNU, "glibc 2.9", True),
    (zenoh_release.X86_GNU, "glibc 2.39", False),
    (zenoh_release.X86_GNU, "", False),
    ("aarch64-unknown-linux-gnu", "glibc 2.35", False),
])
def test_pinned_bridge_is_selected_for_the_container_abi(target, glibc, musl):
    release = zenoh_release.load_release()
    asset, url, digest = zenoh_release.resolve_artifact(
        release["version"], target, glibc=glibc,
        fetch=lambda _: pytest.fail("compatibility path contacted the live API"),
    )
    assert asset.endswith(f"{zenoh_release.X86_MUSL if musl else target}-standalone.zip")
    assert url.endswith("/" + asset)
    assert digest == (release["x86_64_compat"]["sha256"] if musl else release["sha256"][target])


@pytest.mark.parametrize("version,target", [
    ("latest", "x86_64-unknown-linux-gnu"),
    ("1.10.2", "unsupported"),
    ("1.10.2;exit", "x86_64-unknown-linux-gnu"),
])
def test_invalid_release_selection_is_reported_before_network(version, target):
    with pytest.raises(ValueError):
        zenoh_release.resolve_artifact(version, target, fetch=lambda _: pytest.fail("unexpected network"))


def installer_fixture(tmp_path, *, staged_version="1.10.2", plugin=True, bad_checksum=False, old_version="1.9.0", startup_log=False, incompatible=False):
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
        output.writestr("zenoh-bridge-ros2dds", "#!/bin/sh\n" + (
            "echo 'libc.so.6: version GLIBC_2.38 not found' >&2\nexit 1\n"
            if incompatible else f"{prefix}printf 'zenoh-bridge-ros2dds v{staged_version}\\n'\n"
        ))
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


def install(root, env, *, overrides=""):
    if overrides:
        command = ["bash", "-c", 'source "$1"; ' + overrides + '; install_zenoh',
                   "installer-test", str(root / "scripts/bootstrap.sh")]
    else:
        command = ["bash", str(root / "scripts/bootstrap.sh"), "--zenoh-only"]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)


def compat_fixture(tmp_path, *, corrupt=False):
    root, env = installer_fixture(tmp_path)
    manifest = root / "config/zenoh-release.json"
    release = json.loads(manifest.read_text())
    archives = tmp_path / "archives"
    asset = archives / f"zenoh-plugin-ros2dds-1.10.2-{zenoh_release.X86_MUSL}-standalone.zip"
    shutil.copyfile(next(archives.glob("*.zip")), asset)
    runtime = []
    for member, data in {
        "lib/ld-musl-x86_64.so.1": (
            b'#!/bin/sh\ntest "$1" = --library-path && test -r "$2/libgcc_s.so.1" || exit 1\n'
            b'shift 2\nexec "$@"\n'
        ),
        "usr/lib/libgcc_s.so.1": b"libgcc-mock",
    }.items():
        package = archives / (Path(member).name + ".apk")
        with tarfile.open(package, "w:gz") as archive:
            entry = tarfile.TarInfo(member)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        runtime.append({"url": package.as_uri(), "member": member,
                        "sha256": hashlib.sha256(package.read_bytes()).hexdigest()})
    if corrupt:
        runtime[-1]["sha256"] = "0" * 64
    release["x86_64_compat"] = {"native_min_glibc": "2.38",
                               "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                               "runtime": runtime}
    manifest.write_text(json.dumps(release))
    return root, env


JAMMY_X86 = 'getconf() { echo "glibc 2.35"; }; rust_target() { echo x86_64-unknown-linux-gnu; }'


def test_jammy_installs_compatible_bridge_and_reuses_it_offline(tmp_path):
    root, env = compat_fixture(tmp_path)
    # Only the compatible archive is available.
    archives = tmp_path / "archives"
    next(archives.glob("*-linux-gnu-standalone.zip")).unlink()
    result = install(root, env, overrides=JAMMY_X86)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "project-local Zenoh compatibility runtime" in result.stdout
    runtime, = (root / ".zenoh-compat").iterdir()
    assert {p.name for p in runtime.iterdir()} == {"bridge", "ld-musl-x86_64.so.1", "libgcc_s.so.1"}
    assert (root / "libzenoh_plugin_ros2dds.so").read_bytes() == b"new matching plugin"
    assert "ZENOH_BRIDGE_RUNTIME='binary'" in (root / ".zenoh_profile.env").read_text()
    shutil.rmtree(archives)
    repeat = install(root, env, overrides=JAMMY_X86)
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert "already installed" in repeat.stdout


@pytest.mark.parametrize("bad_runtime", [True, False])
def test_failed_compat_install_preserves_the_previous_pair(tmp_path, bad_runtime):
    root, env = compat_fixture(tmp_path, corrupt=bad_runtime)
    paths = [root / name for name in ("zenoh-bridge-ros2dds", "libzenoh_plugin_ros2dds.so", ".zenoh_profile.env")]
    before = [path.read_bytes() for path in paths]
    overrides = JAMMY_X86 if bad_runtime else JAMMY_X86 + "; write_zenoh_profile() { return 1; }"
    result = install(root, env, overrides=overrides)
    assert result.returncode != 0
    message = "runtime checksum failed" if bad_runtime else "restored the previous installation"
    assert message in result.stderr
    assert [path.read_bytes() for path in paths] == before
    assert not [path for path in root.glob(".zenoh-install.*") if path.is_dir()]


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
    result = install(root, env, overrides="write_zenoh_profile() { return 1; }")
    assert result.returncode != 0, result.stdout + result.stderr
    assert [path.read_bytes() for path in paths] == before
    assert "restored the previous installation" in result.stderr


@pytest.mark.parametrize("docker", ["missing", "unreachable", "reachable"])
def test_incompatible_binary_requires_usable_docker_and_reports_real_glibc(tmp_path, docker):
    root, env = installer_fixture(tmp_path, incompatible=True)
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, status in (("docker", int(docker != "reachable")), ("sudo", 1)):
        command = commands / name
        command.write_text(f'#!/bin/sh\nprintf "%s %s\\n" {name} "$*" >> "$TRACE"\nexit {status}\n')
        command.chmod(0o755)
    trace = tmp_path / "trace"
    env.update(PATH=f"{commands}:{env['PATH']}", TRACE=str(trace))
    profile = root / ".zenoh_profile.env"
    before = profile.read_bytes()
    result = install(root, env, overrides=(
        'have() { [[ "$1" != docker ]] && command -v "$1" >/dev/null 2>&1; }'
        if docker == "missing" else ""
    ))
    if docker == "reachable":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ZENOH_BRIDGE_RUNTIME='docker'" in profile.read_text()
        assert trace.read_text().splitlines() == ["docker ps", "docker pull eclipse/zenoh-bridge-ros2dds:1.10.2"]
    else:
        assert result.returncode == 1
        assert profile.read_bytes() == before
        assert "GLIBC_2.38 not found" in result.stderr
        assert "requires glibc 2.38" in result.stderr
        assert "2.28" not in result.stderr
        assert "Configured Zenoh Docker fallback" not in result.stdout
        if docker == "missing":
            assert not trace.exists()
        else:
            assert trace.read_text().splitlines() == ["docker ps", "sudo -n docker ps"]
