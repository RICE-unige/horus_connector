"""Exercise role and feature selection without touching the host package manager."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts/bootstrap.sh"
UNRELATED = {
    "cmake", "build-essential", "libopus-dev", "libdatachannel-dev", "coturn", "v4l-utils",
    "mesa-va-drivers", "intel-media-va-driver", "intel-media-va-driver-non-free", "gstreamer1.0-vaapi",
    "python3-websocket", "python3-websockets", "python3-pip",
}


def plan(tmp_path, role, streams=None, zenoh_only=False, install_ros=False, **settings):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LC_ALL": "C",
           "ROS_DISTRO": "jazzy", "HORUS_STREAMS_CONFIG": str(tmp_path / "streams.json"), **settings}
    (tmp_path / "streams.json").write_text(json.dumps({
        "streams": [{"id": "front"}] if streams is None else streams,
    }))
    result = subprocess.run(
        ["bash", "-euc", 'source "$1"; ROLE="$2"; ZENOH_ONLY="$3"; install_ros="$4"; '
         'select_bootstrap_features; build_apt_packages selected; '
         'printf "%s %s %s\n" "$PREPARE_CAMERA" "$PREPARE_DECODER" "$PREPARE_SIGNALING"; '
         'printf "%s\n" "${selected[@]}"', "bootstrap-test", str(BOOTSTRAP), role,
         str(int(zenoh_only)), str(int(install_ros))],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    flags, *packages = result.stdout.splitlines()
    assert len(packages) == len(set(packages))
    return flags, set(packages)


def test_standard_robot_only_selects_camera_sender_runtime(tmp_path):
    flags, packages = plan(tmp_path, "robot")
    assert flags == "1 0 1"
    assert {"gstreamer1.0-plugins-ugly", "gstreamer1.0-nice", "python3-venv"} <= packages
    assert not packages.intersection(UNRELATED | {"gstreamer1.0-libav", "libopencv-core406t64"})


def test_decoder_follows_role_or_custom_camera_source(tmp_path):
    flags, packages = plan(tmp_path, "machine")
    assert flags == "1 1 1"
    assert {"gstreamer1.0-plugins-ugly", "gstreamer1.0-libav"} <= packages
    _, gateway = plan(tmp_path, "robot", [{
        "video_source": "gst", "source_pipeline": "uridecodebin uri=file:///capture.mp4",
    }])
    assert "gstreamer1.0-libav" in gateway
    _, ignored = plan(tmp_path, "robot", [{
        "video_source": "ros2", "source_pipeline": "uridecodebin uri=file:///capture.mp4",
    }])
    assert "gstreamer1.0-libav" not in ignored


@pytest.mark.parametrize("role", ["robot", "machine", "teammate", "cloud"])
def test_zenoh_only_skips_camera_signaling_and_optional_native_media(tmp_path, role):
    flags, packages = plan(
        tmp_path, role, zenoh_only=True, HORUS_SOURCE_MEDIA_ENABLED="1",
        HORUS_CLOUD_MEDIA_CONFIG="cloud-media.json", HORUS_CLOUD_RUN_TURN="1",
        HORUS_MEDIA_RELAY_CONFIG="media.json", WEBRTC_MEDIA_MODE="jpeg", FIELD_TEAMMATE_RAW_IMAGE="1",
    )
    assert flags == "0 0 0"
    assert not packages.intersection(UNRELATED)
    assert not any(pkg.startswith(("gstreamer", "libopencv", "python3-opencv")) for pkg in packages)


@pytest.mark.parametrize("streams", [[], [{"enabled": False}]])
def test_explicit_no_cameras_skips_video_dependencies(tmp_path, streams):
    flags, packages = plan(tmp_path, "robot", streams=streams)
    assert flags == "0 0 0"
    assert not any(pkg.startswith("gstreamer") for pkg in packages)


def test_native_runtime_bundle_alone_does_not_install_media_libraries(tmp_path):
    flags, packages = plan(tmp_path, "robot", streams=[], HORUS_RUNTIME_BUNDLE="/staged/bundle.tar.gz")
    assert flags == "0 0 0"
    assert not any(pkg.startswith(("gstreamer", "libopencv")) for pkg in packages)
    flags, native = plan(tmp_path, "robot", streams=[], HORUS_SOURCE_MEDIA_ENABLED="1", ROS_DISTRO="humble")
    assert flags == "0 0 0"
    assert {"libopencv-core4.5d", "gstreamer1.0-plugins-ugly"} <= native
    assert "gstreamer1.0-nice" not in native and "python3-gi" not in native


def test_other_roles_only_prepare_enabled_features(tmp_path):
    flags, cloud = plan(tmp_path, "cloud", HORUS_CLOUD_RUN_TURN="1", HORUS_CLOUD_RUN_SIGNALING="0")
    assert flags == "0 0 0"
    assert "coturn" in cloud and not any(pkg.startswith("gstreamer") for pkg in cloud)
    _, teammate = plan(tmp_path, "teammate", FIELD_TEAMMATE_RAW_IMAGE="1")
    assert {"python3-opencv", "python3-numpy"} <= teammate
    assert not teammate.intersection(UNRELATED)
    _, ros_install = plan(tmp_path, "robot", install_ros=True, zenoh_only=True, ROS_DISTRO="humble")
    assert {"ros-humble-ros-base", "ros-humble-rmw-cyclonedds-cpp"} <= ros_install


@pytest.mark.skipif(not Path("/usr/bin/apt-get").exists(), reason="APT is unavailable")
@pytest.mark.parametrize("missing", ["", "gstreamer1.0-nice"])
def test_apt_installs_missing_packages_only_and_disables_recommends(tmp_path, missing):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "RUN_APT": "auto",
           "HORUS_STREAMS_CONFIG": str(tmp_path / "streams.json")}
    (tmp_path / "streams.json").write_text('{"streams":[{"id":"front"}]}')
    result = subprocess.check_output(
        ["bash", "-euc", 'source "$1"; ROLE=robot; absent="$2"; '
         'id() { echo 0; }; apt_package_installed() { [[ "$1" != "$absent" ]]; }; '
         'apt-get() { printf "apt: %s\n" "$*"; }; select_bootstrap_features; run_apt',
         "apt-test", str(BOOTSTRAP), missing],
        cwd=tmp_path, env=env, text=True,
    )
    if missing:
        install = next(line for line in result.splitlines() if " install " in line)
        assert install.endswith("install -y --no-install-recommends gstreamer1.0-nice")
        assert len([line for line in result.splitlines() if line.startswith("apt:")]) == 2
    else:
        assert "skipping APT" in result and "apt:" not in result


def test_workspace_does_not_discover_the_headset_voice_plugin():
    plugin = ROOT / "native/horus_voice"
    assert (plugin / "COLCON_IGNORE").is_file()
    assert (plugin / "CMakeLists.txt").is_file()
    if shutil.which("colcon"):
        result = subprocess.check_output(
            ["colcon", "--log-base", "/dev/null", "list", "--base-paths", str(ROOT)], text=True,
        )
        assert "horus_voice_native" not in result
