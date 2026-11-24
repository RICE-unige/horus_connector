import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from camera_preflight import IMAGE, missing_frame_state, sample_cameras
from connector_config import load_env_file
import horus_setup


def test_optional_profile_defaults_do_not_erase_working_dds(tmp_path):
    profile = tmp_path / ".env"
    profile.write_text("CYCLONEDDS_URI=\nRMW_IMPLEMENTATION=\nROS_DOMAIN_ID=51\n"
                       "ROS_LOCALHOST_ONLY=0\nHORUS_WEBRTC_CLIENT_TOKEN=\n")
    env = dict(os.environ, ROOT=str(ROOT), CYCLONEDDS_URI="file:///working.xml",
               RMW_IMPLEMENTATION="rmw_cyclonedds_cpp", ROS_DOMAIN_ID="90",
               ROS_LOCALHOST_ONLY="1", HORUS_WEBRTC_CLIENT_TOKEN="not-part-of-this-profile")
    command = ('source "$ROOT/scripts/config_env.sh"; horus_load_config "$1" python3; '
               'printf "%s\n" "$CYCLONEDDS_URI" "$RMW_IMPLEMENTATION" "$ROS_DOMAIN_ID" '
               '"$ROS_LOCALHOST_ONLY" "$HORUS_WEBRTC_CLIENT_TOKEN"')
    result = subprocess.check_output(["bash", "-euc", command, "test", str(profile)], env=env, text=True)
    assert result.splitlines() == ["file:///working.xml", "rmw_cyclonedds_cpp", "51", "0", ""]
    profile.write_text(profile.read_text().replace("CYCLONEDDS_URI=\n", "CYCLONEDDS_URI=file:///explicit.xml\n"))
    result = subprocess.check_output(["bash", "-euc", command, "test", str(profile)], env=env, text=True)
    assert result.splitlines()[0] == "file:///explicit.xml"


def test_setup_persists_dds_and_replaces_primary_camera(tmp_path):
    profile = tmp_path / ".env"
    streams = tmp_path / "config/webrtc_streams.json"
    env = dict(os.environ, ROS_DOMAIN_ID="90", ROS_DISTRO="humble", ROS_LOCALHOST_ONLY="0",
               ROS_AUTOMATIC_DISCOVERY_RANGE="SUBNET", RMW_IMPLEMENTATION="rmw_cyclonedds_cpp",
               CYCLONEDDS_URI="file:///working.xml")
    args = [sys.executable, str(ROOT / "scripts/horus_setup.py"), "--root", str(tmp_path),
            "--env", str(profile), "--template", str(ROOT / ".env.example"),
            "--yes", "--role", "robot", "--topology", "hub", "--room", "test",
            "--cloud-ip", "127.0.0.1", "--ros-distro", "humble", "--ros-domain-id", "90"]
    subprocess.run([*args, "--camera-topic", "/camera/image_raw", "--stream-count", "2"],
                   env=env, check=True, capture_output=True)
    subprocess.run([*args, "--camera-topic", "/zed/image_rect_color"], env=env, check=True, capture_output=True)
    profile_data = load_env_file(profile)
    assert profile_data["CYCLONEDDS_URI"] == "file:///working.xml"
    assert profile_data["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert profile_data["ROS_LOCALHOST_ONLY"] == "0"
    assert profile_data["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "SUBNET"
    assert profile_data["WEBRTC_ROS_IMAGE_INPUT_TOPIC"] == "/zed/image_rect_color"
    assert [s["input_topic"] for s in json.loads(streams.read_text())["streams"]] == [
        "/zed/image_rect_color", "/camera/camera_2/image_raw",
    ]


def test_setup_does_not_offer_another_ros_distro(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "humble")
    monkeypatch.setenv("ROS_SETUP_PATH", "/opt/ros/humble/setup.bash")
    monkeypatch.setenv("AMENT_PREFIX_PATH", "/other/overlay")
    monkeypatch.setattr(horus_setup, "read_shell_default", lambda _: "")
    monkeypatch.setattr(horus_setup, "add_setup_candidate", lambda candidates, path: candidates.append(path) if path else None)
    assert horus_setup.detect_ros_setup_candidates("jazzy") == [
        "/opt/ros/jazzy/setup.bash", "/opt/ros/jazzy/local_setup.bash",
    ]


@pytest.mark.parametrize("types,compatible,expected", [
    ([], True, "no_publisher"),
    (["sensor_msgs/msg/CompressedImage"], True, "wrong_type"),
    ([IMAGE], False, "qos_mismatch"),
    ([IMAGE], True, "waiting_for_frames"),
])
def test_preflight_explains_missing_frames(types, compatible, expected):
    endpoints = [SimpleNamespace(topic_type=t) for t in types]
    assert missing_frame_state(endpoints, lambda _: compatible) == expected


def test_preflight_reads_real_ros_frames_and_rejects_compressed_topic(monkeypatch):
    rclpy = pytest.importorskip("rclpy")
    pytest.importorskip("gi")
    from sensor_msgs.msg import CompressedImage, Image
    from rclpy.context import Context

    monkeypatch.setenv("ROS_DOMAIN_ID", "193")
    monkeypatch.setenv("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")
    monkeypatch.delenv("ROS_LOCALHOST_ONLY", raising=False)
    monkeypatch.delenv("CYCLONEDDS_URI", raising=False)
    ctx = Context()
    rclpy.init(context=ctx)
    node = rclpy.create_node("horus_camera_fixture", context=ctx, enable_rosout=False)
    raw = node.create_publisher(Image, "/camera_probe/raw", 1)
    compressed = node.create_publisher(CompressedImage, "/camera_probe/compressed", 1)
    node.create_publisher(Image, "/camera_probe/silent", 1)
    done = threading.Event()

    def publish():
        msg = Image(width=3, height=2, encoding="rgb8", step=9, data=bytes(18))
        while not done.wait(.03):
            raw.publish(msg)
            compressed.publish(CompressedImage(format="jpeg", data=b"fixture"))

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        result = sample_cameras([
            {"id": name, "input_topic": f"/camera_probe/{name}", "ros_image_qos": "auto"}
            for name in ("raw", "compressed", "silent", "missing")
        ], 2)
        assert result["tracks"]["raw"]["state"] == "sample_received"
        assert result["tracks"]["compressed"]["state"] == "wrong_type"
        assert result["tracks"]["silent"]["state"] == "waiting_for_frames"
        assert result["tracks"]["missing"]["state"] == "no_publisher"
        assert result["visible_raw_topics"] == ["/camera_probe/raw", "/camera_probe/silent"]
    finally:
        done.set()
        thread.join()
        node.destroy_node()
        ctx.try_shutdown()


def test_relay_media_startup_failure_releases_partial_pipeline_then_retries(capsys):
    pytest.importorskip("gi")
    from gst_webrtc_h264_robot import H264RobotSender

    calls = []

    def fail_start():
        raise RuntimeError("could not create DDS participant")

    sender = SimpleNamespace(
        stopped=False, recovering=True, _start_pipeline=fail_start,
        _stop_media_pipeline=lambda: calls.append("stop"),
        _schedule_recovery=lambda: calls.append("retry"),
    )
    assert not H264RobotSender._try_start_media_pipeline(sender)
    assert calls == ["stop", "retry"]
    assert sender.media_start_error == "could not create DDS participant"
    assert "will retry" in capsys.readouterr().out
    sender._start_pipeline = lambda: calls.append("started")
    assert H264RobotSender._try_start_media_pipeline(sender)
    assert sender.media_start_error == ""
    assert not sender.recovering
