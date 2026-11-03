import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import json5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_tf_is_not_sampled_and_bulk_does_not_inherit_control_priority():
    for role in ("robot", "machine", "teammate"):
        plugin = json5.loads((ROOT / f"config/zenoh_{role}.json5").read_text())["plugins"]["ros2dds"]
        assert not any(re.search(pattern.rsplit("=", 1)[0], "/tf") or
                       re.search(pattern.rsplit("=", 1)[0], "/tf_static")
                       for pattern in plugin.get("pub_max_frequencies", []))
        assert plugin["reliable_routes_blocking"]
        if role != "teammate":
            for topic in ("/horus/field/mesh/indexed", "/horus/mesh_map/chunk", "/cloud_map", "/horus/gaussian_splat/chunk_item"):
                priority = next(value for rule in plugin["pub_priorities"]
                                for pattern, value in [rule.rsplit("=", 1)] if re.search(pattern, topic))
                assert priority == "6", (role, topic, priority)


@pytest.mark.parametrize("role", ["robot", "machine", "teammate", "cloud"])
def test_default_batching_is_bounded_and_preserves_express_control(role):
    config = json5.loads((ROOT / f"config/zenoh_{role}.json5").read_text())
    batching = config["transport"]["link"]["tx"]["queue"]["batching"]
    assert batching == {"enabled": True, "time_limit": 1}
    if role != "cloud":
        priorities = config["plugins"]["ros2dds"]["pub_priorities"]
        command = "/field/guidance/target" if role == "teammate" else "/cmd_vel"
        for topic in ("/tf", "/tf_static", command):
            priority = next(value for rule in priorities
                            for pattern, value in [rule.rsplit("=", 1)]
                            if re.search(pattern, topic))
            assert priority.endswith(":express"), (role, topic, priority)


def test_routes_extend_custom_commands_without_broadening_other_interfaces():
    from transport_policy import resolve
    result = resolve(ROOT / "config/zenoh_machine.json5", {"publishers": ["/fmu/in/trajectory_setpoint"]})
    patterns = result["plugins"]["ros2dds"]["allow"]["publishers"]
    assert any(re.fullmatch(pattern, "/fmu/in/trajectory_setpoint") for pattern in patterns)
    assert not any(re.fullmatch(pattern, "/unregistered/arbitrary_input") for pattern in patterns)
    with pytest.raises(ValueError, match="TF"):
        resolve(ROOT / "config/zenoh_robot.json5", {"max_frequencies": {"/robot/tf": 20}})


def test_explicit_media_ownership_uses_rust_compatible_regular_expressions():
    from transport_policy import resolve
    result = resolve(ROOT / "config/zenoh_robot.json5", {}, [
        {"input_topic": "/camera/image_raw", "transport": "webrtc_only"},
        {"input_topic": "/camera/rear/image_raw", "transport": "webrtc_only"}], role="robot")
    pattern, = result["plugins"]["ros2dds"]["allow"]["publishers"]
    assert "(?=" not in pattern and "(?!" not in pattern
    assert not re.fullmatch(pattern, "/camera/image_raw")
    assert not re.fullmatch(pattern, "/camera/rear/image_raw")
    for topic in ("/tf", "/camera/image_raw/metadata", "/camera", "/camera/rear", "/camera/rear/image_raw_extra"):
        assert re.fullmatch(pattern, topic), topic


def test_fleet_graph_identity_preserves_domains_and_interfaces():
    from horus_fleet import graph_identity
    assert graph_identity({}) == graph_identity({"ROS_DOMAIN_ID": "0"})
    assert graph_identity({"ROS_DOMAIN_ID": "1"}) != graph_identity({"ROS_DOMAIN_ID": "2"})
    assert graph_identity({"HORUS_DDS_INTERFACE": "lo"}) != graph_identity({"HORUS_DDS_INTERFACE": "eth0"})


def test_fleet_shares_only_compatible_graphs(tmp_path, monkeypatch):
    import horus_fleet
    prepared = []
    members = tmp_path / "fleet.json"
    members.write_text(json.dumps({"members": [{"name": "one", "ros_domain_id": 1},
        {"name": "two", "ros_domain_id": 2}, {"name": "three", "ros_domain_id": 1}]}))
    monkeypatch.setattr(horus_fleet, "launch_prepared", lambda root, base, rows, *_: prepared.extend(rows) or 0)
    args = SimpleNamespace(root=str(tmp_path), base_env=str(tmp_path / "absent.env"), config=str(members), role="machine")
    assert horus_fleet.command_launch(args) == 0
    assert [row[-1]["HORUS_ZENOH_ENABLED"] for row in prepared] == ["1", "1", "0"]


def test_sdk_control_contract_includes_waypoint_and_teleop_commands():
    from transport_policy import registration_routes
    registration = {"control": {"drive_topic": "/rover/drive", "teleop": {
        "command_topic": "/fmu/in/command", "head_pose_topic": "/operator/head"},
        "tasks": {"waypoint": {"path_topic": "/rover/waypoints"},
                  "go_to_point": {"goal_topic": "/uav/target", "cancel_topic": "/uav/cancel"}}}}
    for role, key in (("machine", "publishers"), ("robot", "subscribers")):
        assert set(registration_routes([registration], role)[key]) == {
            "/rover/drive", "/fmu/in/command", "/operator/head", "/rover/waypoints", "/uav/target", "/uav/cancel"}


def test_padded_array_owns_pixels_after_source_changes():
    from ros_image_io import packed_image_array
    source = bytearray([1, 2, 3, 99, 4, 5, 6, 99])
    packed = packed_image_array(source, 4, 3, 2)
    source[:] = bytes(8)
    assert bytes(packed) == bytes([1, 2, 3, 4, 5, 6])


def test_bitrate_budget_cannot_silently_exceed_session_limit(tmp_path, monkeypatch):
    from webrtc_streams import load_streams
    file = tmp_path / "streams.json"
    file.write_text(json.dumps({"version": 2, "session": {"bitrate_budget_kbit": 1000},
        "streams": [{"id": "front", "min_bitrate_kbit": 800}, {"id": "rear", "min_bitrate_kbit": 800}]}))
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(file))
    with pytest.raises(ValueError, match="budget"):
        load_streams(tmp_path, "robot")


def test_partial_encoder_update_restores_and_reports_actual_bitrates():
    from gst_webrtc_h264_robot import H264RobotSender
    class Encoder:
        def __init__(self, fail=False):
            self.value, self.fail = 500, fail
        def find_property(self, _):
            return True
        def get_property(self, _):
            return self.value
        def set_property(self, _, value):
            if self.fail and value != 500:
                raise RuntimeError("runtime update rejected")
            self.value = value
    sender = object.__new__(H264RobotSender)
    sender.profile = {"GST_H264_ENCODER_NAME": "x264enc"}
    sender.current_bitrate_kbit = 1000
    stream = {"min_bitrate_kbit": 100, "max_bitrate_kbit": 1000, "bitrate_kbit": 500}
    sender.encoders = [(stream, Encoder()), (stream, Encoder(True))]
    assert not sender._set_encoder_bitrate(1200)
    assert sender.current_bitrate_kbit == 1000
    assert sender.applied_track_bitrates == [500, 500]


@pytest.mark.parametrize("connected,expected", [(False, False), (True, True)])
def test_first_encoder_output_watchdog_requires_a_connected_peer(connected, expected, monkeypatch):
    from gst_webrtc_h264_robot import H264RobotSender
    from media_health import RecoveryBackoff
    sender = object.__new__(H264RobotSender)
    sender.stopped = False
    sender.webrtc = SimpleNamespace(get_property=lambda _: SimpleNamespace(value_nick="connected" if connected else "new"))
    sender.recovering = False
    sender.negotiation_started = time.monotonic() - 20
    sender.negotiation_in_progress = False
    sender.streams = [{"id": "front"}]
    sender.rtp_last, sender.rtp_frames = {}, {}
    sender.sources_by_id = {"front": SimpleNamespace(last_push_sec=time.monotonic(), rate_dropped=0, total_received=10, total_pushed=10)}
    sender.recovery_timer = None
    sender.recovery_backoff = RecoveryBackoff()
    sender.generation, sender.current_bitrate_kbit = 1, 1000
    sender.signaling = SimpleNamespace(connected=SimpleNamespace(is_set=lambda: False))
    invoked = []
    sender._schedule_recovery = lambda: invoked.append(True)
    monkeypatch.delenv("HORUS_MEDIA_STATUS_FILE", raising=False)
    assert sender._media_health()
    assert bool(invoked) == expected


def test_readiness_does_not_confuse_process_and_frames(tmp_path):
    from service_readiness import check
    from service_runner import process_start_time
    child = os.getpid()
    (tmp_path / "webrtc.service.json").write_text(json.dumps({"phase": "running", "child_pid": child,
        "child_start": process_start_time(child)}))
    assert not check(tmp_path, "webrtc")["ready"]
    (tmp_path / "webrtc.media.json").write_text(json.dumps({"updated_wall_sec": time.time(), "pid": child,
        "tracks": {"front": {"state": "waiting"}}}))
    result = check(tmp_path, "webrtc")
    assert result["ready"] and not result["data_ready"]
    assert result["stage"] == "media_initialized"
    (tmp_path / "webrtc.media.json").write_text(json.dumps({"updated_wall_sec": time.time(), "pid": child + 1,
        "tracks": {"front": {"state": "flowing"}}}))
    assert not check(tmp_path, "webrtc")["ready"]


def test_monitor_distinguishes_restart_backoff_from_running_child(tmp_path):
    import horus_monitor
    pid = os.getpid()
    (tmp_path / "webrtc.pid").write_text(str(pid))
    (tmp_path / "webrtc.pid.start").write_text(horus_monitor.process_start_time(pid))
    service = {"supervisor_pid": pid, "phase": "backoff", "child_pid": pid,
               "child_start": horus_monitor.process_start_time(pid)}
    path = tmp_path / "webrtc.service.json"
    path.write_text(json.dumps(service))
    state = horus_monitor.process_state(tmp_path, "webrtc")
    assert state.supervisor_running and not state.running and state.phase == "backoff"
    service["phase"] = "running"
    path.write_text(json.dumps(service))
    assert horus_monitor.process_state(tmp_path, "webrtc").running
    service["child_start"] = "reused"
    path.write_text(json.dumps(service))
    assert not horus_monitor.process_state(tmp_path, "webrtc").running


def test_codec_failure_invalidates_only_managed_profile(tmp_path, monkeypatch):
    import media_runtime
    path = tmp_path / "profile.env"
    path.write_text("GST_H264_ENCODER_NAME=x264enc\n")
    assert not media_runtime.invalidate_profile(path)
    marker = path.with_suffix(".env.fingerprint")
    marker.write_text("old-runtime")
    assert media_runtime.invalidate_profile(path)
    assert path.exists() and not marker.exists() and path.with_suffix(".env.invalid").exists()
    calls = []
    monkeypatch.setattr(media_runtime, "validate", lambda *a: calls.append("validate"))
    monkeypatch.setattr(media_runtime, "ensure_profile", lambda *a: calls.append("probe"))
    media_runtime.recover_profile(SimpleNamespace(root=str(tmp_path), profile=str(path)), "robot")
    assert calls == ["validate", "probe"]


def test_gpu_telemetry_does_not_invent_unsupported_values():
    from gpu_metrics import parse_devices
    device, = parse_devices("GPU-123, 40, 10, 400, 1000, [N/A], 12\n")
    assert device["gpu_percent"] == 40 and device["decoder_percent"] == 12
    assert device["encoder_percent"] is None
    assert device["scope"] == "device_total_not_connector_only"


def test_native_media_recycling_requires_an_owned_supervisor(monkeypatch):
    from media_health import recycle_media_process
    calls = []
    owner = SimpleNamespace(stop=lambda: calls.append("stopped"))
    monkeypatch.delenv("HORUS_MEDIA_SUPERVISED", raising=False)
    assert not recycle_media_process(owner) and not calls
    monkeypatch.setenv("HORUS_MEDIA_SUPERVISED", "1")
    monkeypatch.setenv("WEBRTC_MAX_PIPELINE_REBUILDS", "2")
    assert not recycle_media_process(owner)
    assert recycle_media_process(owner) and calls == ["stopped"] and owner.codec_exit_code == 75


def test_dds_preflight_reads_quoted_generated_configuration(tmp_path):
    import dds_preflight
    path = tmp_path / "zenoh.json"
    path.write_text(json.dumps({"plugins": {"ros2dds": {"ros_localhost_only": True,
                    "allow": {"publishers": ["^/custom$"]}}}}))
    assert dds_preflight.load_allow_patterns(path) == {"publishers": ["^/custom$"]}
    assert dds_preflight.zenoh_config_requests_localhost(path)


def test_dds_data_probe_requires_a_real_sample_result(monkeypatch):
    import dds_preflight
    monkeypatch.setattr(dds_preflight, "find_ros_setup", lambda *a: "")
    monkeypatch.setattr(dds_preflight, "run_command", lambda *a, **k: (0, '{"topic":"/tf","received":true}', ""))
    result = dds_preflight.ros_data_probe(ROOT, "robot", "/tf")
    assert result["received"] and not result["skipped"]
    monkeypatch.setattr(dds_preflight, "run_command", lambda *a, **k: (124, "", "deadline exceeded"))
    result = dds_preflight.ros_data_probe(ROOT, "robot", "/tf")
    assert not result["received"] and "deadline" in result["error"]


def test_doctor_honors_explicit_role_and_absolute_profile(tmp_path):
    config = tmp_path / "connector.env"
    run = tmp_path / "run"
    config.write_text("HORUS_ROLE=robot\nHORUS_TOPOLOGY=direct\nZENOH_CONFIG=auto\nHORUS_CLOUD_RUN_TURN=0\n")
    env = dict(os.environ, HORUS_ENV=str(config), HORUS_RUN_DIR=str(run))
    result = subprocess.run([str(ROOT / "horus"), "doctor", "cloud"], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    verdict = json.loads((run / "dds_preflight.json").read_text())
    assert verdict["role"] == "cloud" and verdict["zenoh_config"] == str(ROOT / "config/zenoh_cloud.json5")
    custom = tmp_path / "custom profile.json5"
    custom.write_text((ROOT / "config/zenoh_cloud.json5").read_text())
    config.write_text(f"HORUS_ROLE=robot\nHORUS_TOPOLOGY=direct\nZENOH_CONFIG='{custom}'\nHORUS_CLOUD_RUN_TURN=0\n")
    result = subprocess.run([str(ROOT / "horus"), "doctor", "cloud"], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads((run / "dds_preflight.json").read_text())["zenoh_config"] == str(custom)


def test_unsynchronized_cross_host_timing_is_not_reported_as_latency():
    from collections import deque
    from gst_webrtc_h264_machine import H264MachineReceiver
    receiver = H264MachineReceiver.__new__(H264MachineReceiver)
    receiver.args = SimpleNamespace(clock_offset_ms=0, clock_offset_source="none", fresh_deadline_ms=150)
    receiver.observation_start = 0
    receiver.frame_latency_samples = deque()
    receiver._append_matched_latency({"sent_ns": 1_000_000, "rtp_timestamp": 1},
                                    {"receive_wall_ns": 2_000_000, "now": 1, "output_stage": "decoded"}, "front")
    sample = receiver.frame_latency_samples[0]
    assert sample["latency_ms"] is None and sample["raw_latency_ms"] == 1
    assert not sample["timing_valid"] and not sample["fresh"] and not sample["clock_synchronized"]


def test_cloud_runtime_never_requires_optional_media_or_ros(monkeypatch):
    import builtins
    from media_runtime import validate
    original = builtins.__import__
    def checked(name, *args, **kwargs):
        if name in {"aiortc", "gi", "rclpy"}:
            pytest.fail(f"Cloud signaling unexpectedly imported {name}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", checked)
    validate("jpeg", "cloud", False)


def test_shared_config_roundtrips_literal_quotes_without_shell_execution(tmp_path):
    from connector_config import load_env_file, quote
    values = {"NAME": "operator's $literal \\ value", "JSON": '{"camera":"front"}', "SPACE": "hello world"}
    config = tmp_path / "settings.env"
    config.write_text("".join(f"{key}={quote(value)}\n" for key, value in values.items()))
    assert load_env_file(config) == values
    config.write_text("BASE=hello\nDERIVED=\"${BASE} world\"\n")
    assert load_env_file(config) == {"BASE": "hello", "DERIVED": "${BASE} world"}
    marker = tmp_path / "executed"
    config.write_text(f"BAD=$(touch {marker})\ntouch {marker}\n")
    assert load_env_file(config) == {"BAD": f"$(touch {marker})"}
    assert not marker.exists()


def test_calibration_accounts_for_letterboxing_and_stereo_baseline():
    from ros_image_io import pixel_transform, transform_intrinsics
    transform = pixel_transform(640, 480, 1280, 720)
    assert transform["offset_x"] == 160 and transform["offset_y"] == 0
    assert transform_intrinsics([400, 0, 320, 0, 400, 240, 0, 0, 1], 3, transform) == [600, 0, 640, 0, 600, 360, 0, 0, 1]
    projection = [400, 0, 320, -40, 0, 400, 240, 0, 0, 0, 1, 0]
    transformed = transform_intrinsics(projection, 4, transform)
    assert transformed[3] / transformed[0] == -.1


def test_ros_packed_yuv_aliases_are_not_swapped():
    from ros_image_io import ros_encoding_to_gst
    assert ros_encoding_to_gst("yuv422") == ("UYVY", 2)
    assert ros_encoding_to_gst("yuv422_yuy2") == ("YUY2", 2)


def test_late_metadata_remains_available_after_latency_correlation():
    from media_health import FrameMatcher
    matcher = FrameMatcher()
    matcher.packet(100, 99)
    assert matcher.decoded(100, {"stage": "decoded"}) is None
    source = {"sec": 10, "nanosec": 0, "frame_id": "front"}
    assert matcher.marker({"rtp_timestamp": 99, "source": source})
    assert matcher.metadata_for_pts(100) == source
    assert matcher.metadata_for_pts(101) is None


def test_delivery_waits_for_metadata_without_blocking_caller():
    from frame_delivery import FrameDelivery
    metadata = {}
    delivered = []
    ready = threading.Event()
    publisher = SimpleNamespace(preserve_source_header=True, camera_info_topic="",
        publish_sample=lambda frame, data, **kwargs: (delivered.append(data), ready.set()), close=lambda: None)
    worker = FrameDelivery(publisher, metadata.get, wait_sec=.2)
    try:
        frame = SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=1))
        start = time.monotonic()
        assert worker.submit(frame)
        assert time.monotonic() - start < .1
        metadata[1] = {"frame_id": "source", "sec": 1, "nanosec": 0}
        worker.wake()
        assert ready.wait(1)
        assert delivered == [metadata[1]]
    finally:
        assert worker.close()


def test_delivery_drops_missing_metadata_and_resets_pending_generation():
    from frame_delivery import FrameDelivery
    publisher = SimpleNamespace(preserve_source_header=True, camera_info_topic="",
        publish_sample=lambda *_: pytest.fail("unmatched frame published"), close=lambda: None)
    worker = FrameDelivery(publisher, lambda _: None, capacity=2, wait_sec=.02)
    try:
        for index in range(20):
            worker.submit(SimpleNamespace(get_buffer=lambda index=index: SimpleNamespace(pts=index)))
        assert len(worker.pending) <= 2
        time.sleep(.1)
        assert worker.expired > 0 and worker.replaced > 0
        worker.reset()
        assert not worker.pending
        assert not worker.submit(SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=30)), epoch=worker.epoch - 1)
    finally:
        assert worker.close()


def test_delivery_counts_rejected_publication_separately_from_worker_errors():
    from frame_delivery import FrameDelivery
    publisher = SimpleNamespace(preserve_source_header=False, camera_info_topic="",
        publish_sample=lambda *a, **k: False, close=lambda: None)
    worker = FrameDelivery(publisher, lambda _: None)
    try:
        worker.submit(SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=1)))
        deadline = time.monotonic() + 1
        while not worker.rejected and time.monotonic() < deadline:
            time.sleep(.01)
        assert worker.rejected == 1 and worker.failures == 0
    finally:
        assert worker.close()


def test_rate_reports_recent_window_not_lifetime_average():
    from media_health import RollingRate
    rate = RollingRate(window_sec=2)
    for instant in (1, 1.1, 1.2, 1.3):
        rate.add(instant)
    assert rate.rate(2) == 2
    assert rate.rate(5) == 0


def test_camera_info_requests_latched_calibration_separately():
    pytest.importorskip("rclpy")
    from ros_image_io import camera_info_qos_profiles
    from rclpy.qos import DurabilityPolicy
    assert {qos.durability for qos in camera_info_qos_profiles()} == {
        DurabilityPolicy.VOLATILE, DurabilityPolicy.TRANSIENT_LOCAL}


def test_process_sampler_detects_pid_reuse_and_returns_interval_cpu(monkeypatch):
    import process_metrics
    from process_metrics import ProcessSampler
    sampler = ProcessSampler()
    times = iter((1., 2., 3.))
    monkeypatch.setattr(process_metrics.time, "monotonic", lambda: next(times))
    values = [{1: (100, 10, 1000)}, {1: (100, 20, 1000)}, {1: (101, 1, 1000)}]
    monkeypatch.setattr(sampler, "_tree", lambda _: values.pop(0))
    assert sampler.sample(1)["cpu_percent"] is None
    assert sampler.sample(1)["cpu_percent"] is not None
    assert sampler.sample(1)["cpu_percent"] is None
