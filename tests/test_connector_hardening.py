import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_horus_env_loader_does_not_execute_shell(tmp_path):
    marker = tmp_path / "executed"
    env_file = tmp_path / "horus.env"
    run_dir = tmp_path / "run"
    env_file.write_text(
        "\n".join(
            [
                "HORUS_ROLE=machine",
                "HORUS_TOPOLOGY=direct",
                "HORUS_MACHINE_IP=127.0.0.1",
                f"MALICIOUS=$(touch {marker})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HORUS_ENV"] = str(env_file)
    env["HORUS_RUN_DIR"] = str(run_dir)
    result = subprocess.run(
        [str(ROOT / "horus"), "status"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_horus_env_loader_ignores_non_assignment_shell_syntax(tmp_path):
    marker = tmp_path / "executed"
    env_file = tmp_path / "horus.env"
    run_dir = tmp_path / "run"
    env_file.write_text(
        "\n".join(
            [
                "HORUS_ROLE=machine",
                "HORUS_TOPOLOGY=direct",
                "HORUS_MACHINE_IP=127.0.0.1",
                f"touch {marker}",
                f"`touch {marker}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HORUS_ENV"] = str(env_file)
    env["HORUS_RUN_DIR"] = str(run_dir)
    result = subprocess.run(
        [str(ROOT / "horus"), "status"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_shell_helpers_do_not_source_connector_env_files():
    shell_files = [
        ROOT / "horus",
        SCRIPTS / "bootstrap.sh",
        SCRIPTS / "run_uav_sim_horus_machine.sh",
    ]

    unsafe_patterns = [
        'source "${ENV_FILE}"',
        "source ${ENV_FILE}",
        "source .env",
        ". ${ENV_FILE}",
        ". .env",
    ]

    for shell_file in shell_files:
        text = shell_file.read_text(encoding="utf-8")
        for pattern in unsafe_patterns:
            assert pattern not in text, f"{shell_file} still sources env with {pattern}"


def test_zenoh_tls_renderer_injects_tls_block(tmp_path):
    base = ROOT / "config" / "zenoh_cloud.json5"
    out = tmp_path / "zenoh_rendered.json5"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render_zenoh_config.py"),
            "--base",
            str(base),
            "--out",
            str(out),
            "--root-ca",
            "/tmp/hub.cert.pem",
            "--listen-private-key",
            "/tmp/hub.key.pem",
            "--listen-certificate",
            "/tmp/hub.cert.pem",
            "--verify-name-on-connect",
            "0",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = out.read_text(encoding="utf-8")
    assert "tls: {" in rendered
    assert 'root_ca_certificate: "/tmp/hub.cert.pem"' in rendered
    assert 'listen_private_key: "/tmp/hub.key.pem"' in rendered
    assert "verify_name_on_connect: false" in rendered


def test_relay_rejects_non_loopback_without_auth():
    import webrtc_signal_relay

    args = SimpleNamespace(
        host="0.0.0.0",
        port=9876,
        auth_token="",
        allow_unauthenticated=False,
        state_file="",
        max_pending_per_peer=2,
        max_pending_bytes=1024,
        pending_ttl_sec=30.0,
        max_message_bytes=1024,
    )

    with pytest.raises(SystemExit):
        import asyncio

        asyncio.run(webrtc_signal_relay.main_async(args))


def test_relay_pending_queue_is_bounded():
    import webrtc_signal_relay

    relay = webrtc_signal_relay.Relay(max_pending_per_peer=2, max_pending_bytes=1024, pending_ttl_sec=0)
    target = ("room", "machine")

    relay._queue_pending_locked(target, "one")
    relay._queue_pending_locked(target, "two")
    relay._queue_pending_locked(target, "three")

    assert [text for _created_at, text in relay.pending[target]] == ["two", "three"]


def test_json_signaling_pending_queue_is_bounded(monkeypatch):
    monkeypatch.setenv("HORUS_WEBRTC_SIGNAL_PENDING_MAX", "2")
    import gst_webrtc_common

    signaling = gst_webrtc_common.JsonSignaling(lambda _message: None)
    signaling._queue_pending("one")
    signaling._queue_pending("two")
    signaling._queue_pending("three")

    queued = []
    while not signaling.pending.empty():
        queued.append(signaling.pending.get_nowait())
    assert queued == ["two", "three"]


def test_legacy_webrtc_sender_clamps_and_rejects_cmd_vel():
    import webrtc_camera_sender

    published = []

    class FakePublisher:
        def publish(self, msg):
            published.append(msg)

    def make_twist():
        return SimpleNamespace(
            linear=SimpleNamespace(x=0.0, y=0.0, z=0.0),
            angular=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        )

    publisher = webrtc_camera_sender.RosCmdPublisher.__new__(
        webrtc_camera_sender.RosCmdPublisher
    )
    publisher.publisher = FakePublisher()
    publisher.twist_type = make_twist
    publisher.max_linear_mps = 0.5
    publisher.max_angular_rps = 1.0

    assert not publisher.publish({"linear_x": "nan"})
    assert published == []

    assert publisher.publish({"linear_x": 4.0, "angular_z": -3.0})
    assert published[0].linear.x == 0.5
    assert published[0].angular.z == -1.0


def test_legacy_webrtc_control_channel_open_close_publishes_zero():
    import webrtc_camera_sender

    published = []
    handlers = {}

    class FakeChannel:
        def on(self, name):
            def decorator(callback):
                handlers[name] = callback
                return callback

            return decorator

    class FakePublisher:
        def publish(self, command):
            published.append(command)
            return True

    webrtc_camera_sender.attach_control_channel(FakeChannel(), FakePublisher())

    handlers["open"]()
    handlers["close"]()

    assert published == [
        {
            "linear_x": 0.0,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": 0.0,
        },
        {
            "linear_x": 0.0,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": 0.0,
        },
    ]


def test_webrtc_control_requires_matching_token():
    import webrtc_control

    command = {"type": "cmd_vel", "control_token": "secret"}

    assert webrtc_control.command_authorized(command, "secret")
    assert not webrtc_control.command_authorized(command, "different")
    assert not webrtc_control.command_authorized(command, "")
    assert not webrtc_control.command_authorized({"type": "cmd_vel"}, "secret")
    assert webrtc_control.command_authorized(
        {"type": "cmd_vel"},
        "",
        allow_unauthenticated=True,
    )


def test_ros_image_short_payload_is_rejected():
    import ros_image_io

    with pytest.raises(ValueError):
        ros_image_io.contiguous_image_bytes(bytes([1, 2, 3]), step=4, expected_step=4, height=2)


def test_cmd_vel_values_are_finite_and_clamped():
    import gst_webrtc_h264_robot

    class Vector:
        x = 0.0
        y = 0.0
        z = 0.0

    class Twist:
        def __init__(self):
            self.linear = Vector()
            self.angular = Vector()

    class Publisher:
        def __init__(self):
            self.messages = []

        def publish(self, message):
            self.messages.append(message)

    publisher = Publisher()
    command = gst_webrtc_h264_robot.RosCmdPublisher("", max_linear_mps=0.5, max_angular_rps=1.0)
    command.publisher = publisher
    command.twist_type = Twist

    assert command.publish({"linear_x": 4.0, "angular_z": -8.0})
    assert publisher.messages[-1].linear.x == 0.5
    assert publisher.messages[-1].angular.z == -1.0
    assert not command.publish({"linear_x": "nan"})


def test_h264_control_channel_teardown_publishes_zero():
    import gst_webrtc_h264_robot

    published = []

    class Control:
        def publish(self, command):
            published.append(command)
            return True

    sender = gst_webrtc_h264_robot.H264RobotSender.__new__(
        gst_webrtc_h264_robot.H264RobotSender
    )
    sender.control = Control()

    sender._on_control_channel_close(None)
    sender._on_control_channel_error(None, "closed by peer")

    assert len(published) == 2
    for command in published:
        assert command == {
            "linear_x": 0.0,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": 0.0,
        }
