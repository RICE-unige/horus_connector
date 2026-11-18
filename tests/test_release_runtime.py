import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cloud_media_config import validate as validate_cloud
import horus_setup
from runtime_bundle import FILES, install, pack, sha256
from transport_policy import resolve


PLATFORM = {"ubuntu": "24.04", "arch": "x86_64", "ros": "jazzy"}
PIN = json.loads((ROOT / "config/zenoh-source.json").read_text())


def run_bootstrap(tmp_path, config, role, **overrides):
    profile = tmp_path / "connector.env"
    profile.write_text(config)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HORUS_", "ROS_", "RMW_"))}
    env.update(HORUS_ENV=str(profile), RUN_APT="no", **overrides)
    return subprocess.run([str(ROOT / "scripts/bootstrap.sh"), role], env=env,
                          capture_output=True, text=True, timeout=10)


def test_bootstrap_lets_explicit_ros_install_override_profile(tmp_path):
    config = "HORUS_INSTALL_ROS=0\nROS_DISTRO=horus-test-absent\n"
    default = run_bootstrap(tmp_path, config, "machine")
    assert default.returncode != 0 and "ROS setup not found" in default.stderr
    requested = run_bootstrap(tmp_path, config, "machine", HORUS_INSTALL_ROS="1")
    assert requested.returncode != 0 and "ROS installation needs apt enabled" in requested.stderr


@pytest.mark.parametrize("config", ["HORUS_RUNTIME_SHA256=" + "a" * 64, "HORUS_RUNTIME_BUNDLE=/"])
def test_bootstrap_rejects_incomplete_runtime_selection(tmp_path, config):
    result = run_bootstrap(tmp_path, config + "\n", "cloud")
    assert result.returncode != 0 and "Set HORUS_RUNTIME_BUNDLE" in result.stderr
    assert "unbound variable" not in result.stderr


def test_native_plugin_path_keeps_versioned_host_plugins(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    env = dict(os.environ, ROOT=str(tmp_path), HORUS_MEDIA_GST_PLUGIN_DIR=str(bundle),
               WEBRTC_VENV=str(tmp_path / "venv"), GST_PLUGIN_PATH_1_0="/active/vendor/plugins",
               GST_PLUGIN_PATH="/inactive/generic/plugins")
    command = 'source "$1"; horus_native_plugin_path'
    result = subprocess.check_output(["bash", "-euc", command, "test", str(ROOT / "scripts/managed_runtime.sh")],
                                     env=env, text=True)
    assert result == f"{bundle}:/active/vendor/plugins"


@pytest.mark.parametrize("version,distro", [("22.04", "humble"), ("24.04", "jazzy")])
def test_setup_accepts_a_fresh_host_without_ros(tmp_path, monkeypatch, version, distro):
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    monkeypatch.setattr(horus_setup, "freedesktop_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": version})
    monkeypatch.setattr(horus_setup, "read_shell_default", lambda key: "")
    monkeypatch.setattr(horus_setup, "detect_ros_distros", lambda: [])
    monkeypatch.setattr(horus_setup, "detect_ros_setup_candidates", lambda distro: [])
    args = SimpleNamespace(root=tmp_path, env=tmp_path / ".env", template=None,
                           no_color=True, yes=True, ros_distro=None, ros_setup_path=None, ros_domain_id="233")
    wizard = horus_setup.Wizard(args)
    assert wizard.resolve_ros_distro() == distro
    assert wizard.resolve_ros_setup_path(distro, "machine") == ""
    with pytest.raises(SystemExit, match="Invalid value for ROS_DOMAIN_ID"):
        wizard.resolve_ros_domain_id()


def test_runtime_bundle_verifies_and_reuses_install(tmp_path):
    for name in FILES:
        (tmp_path / name).write_text(name)
    (tmp_path / "zenoh-bridge-ros2dds.source.json").write_text(json.dumps({
        **PIN, "binary_sha256": sha256(tmp_path / "zenoh-bridge-ros2dds"),
    }))
    bundle = tmp_path / "runtime.tar.gz"
    digest = pack(tmp_path, bundle, PLATFORM, PIN, "0123456")
    target = tmp_path / "install"
    with pytest.raises(ValueError, match="SHA-256"):
        install(bundle, target, "0" * 64, PLATFORM, PIN)
    with pytest.raises(ValueError, match="does not match"):
        install(bundle, target, digest, {**PLATFORM, "arch": "aarch64"}, PIN)
    with pytest.raises(ValueError, match="Expected HORUS ROS 2 revision"):
        install(bundle, target, digest, PLATFORM, PIN, "fedcba9")
    assert not (target / digest).exists()
    first = install(bundle, target, digest, PLATFORM, PIN, "0123456")
    assert install(bundle, target, digest, PLATFORM, PIN) == first
    with pytest.raises(ValueError, match="Expected HORUS ROS 2 revision"):
        install(bundle, target, digest, PLATFORM, PIN, "fedcba9")
    executable = first / "horus_media_node"
    assert executable.stat().st_mode & 0o100
    executable.write_text("damaged")
    with pytest.raises(ValueError, match="changed or missing"):
        install(bundle, target, digest, PLATFORM, PIN)


def test_runtime_rejects_archive_paths_and_links(tmp_path):
    for index, name in enumerate(("../unexpected", "horus_media_node")):
        bundle = tmp_path / f"runtime-{index}.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            if index:
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                archive.addfile(member)
            else:
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
        with pytest.raises(ValueError, match="invalid runtime archive entry"):
            install(bundle, tmp_path / "install", sha256(bundle), PLATFORM, PIN)
    assert not (tmp_path / "unexpected").exists()


def test_ros_resolution_only_sources_once_and_restores_profile(tmp_path):
    setup = tmp_path / "ROS overlay.bash"
    setup.write_text('export ROS_DISTRO=jazzy\nexport ROS_DOMAIN_ID=1\n'
                     'export RMW_IMPLEMENTATION=overlaid\nprintf "setup\\n"\n')
    env = dict(os.environ, ROS_DOMAIN_ID="051", ROS_DISTRO="jazzy", ROS_SETUP_PATH=str(setup),
               RMW_IMPLEMENTATION="rmw_cyclonedds_cpp", HORUS_TERMINAL_ROS_DOMAIN_ID="51")
    shell = 'source "$1"; horus_ros_environment; horus_ros_environment; echo "$ROS_DOMAIN_ID $RMW_IMPLEMENTATION"'
    result = subprocess.run(["bash", "-euc", shell, "test", str(ROOT / "scripts/ros_environment.sh")],
                            env=env, check=True, text=True, capture_output=True)
    assert result.stdout.splitlines() == ["setup", "51 rmw_cyclonedds_cpp"]
    result = subprocess.run(["bash", "-euc", shell, "test", str(ROOT / "scripts/ros_environment.sh")],
                            env={**env, "HORUS_TERMINAL_ROS_DOMAIN_ID": "55"}, text=True, capture_output=True)
    assert result.returncode != 0 and "ROS_DOMAIN_ID conflict" in result.stderr


def test_zenoh_domain_overlay_preserves_filter_and_rejects_mismatch(tmp_path):
    profile = tmp_path / "bridge.json"
    profile.write_text('{"plugins":{"ros2dds":{"deny":{"publishers":["/debug"]}}}}')
    plugin = resolve(profile, {}, dds_domain=51)["plugins"]["ros2dds"]
    assert plugin == {"domain": 51, "deny": {"publishers": ["/debug"]}}
    profile.write_text('{"plugins":{"ros2dds":{"domain":52}}}')
    with pytest.raises(ValueError, match="conflicts with ROS_DOMAIN_ID=51"):
        resolve(profile, {}, dds_domain=51)


def test_cloud_media_uses_only_authenticated_signaling_routes(tmp_path):
    tls = {"enable_mtls": True}
    for name in ("root_ca_certificate", "connect_private_key", "connect_certificate"):
        path = tmp_path / name
        path.touch()
        tls[name] = str(path)
    allow = {"publishers": ["^/horus/remote/webrtc/client_signal$", "^/horus/cloud/webrtc/server_signal$"],
             "subscribers": ["^/horus/remote/webrtc/server_signal$", "^/horus/cloud/webrtc/client_signal$"]}
    config = {"plugins": {"ros2dds": {"domain": 209, "allow": allow}},
              "listen": {"endpoints": []}, "connect": {"endpoints": ["tls/127.0.0.1:7447"]},
              "transport": {"link": {"tls": tls}}}
    validate_cloud(config, 209)
    with pytest.raises(ValueError, match="same dedicated"):
        validate_cloud(config, 51)
    allow["publishers"].append("^/cmd_vel$")
    with pytest.raises(ValueError, match="only the four"):
        validate_cloud(config, 209)
