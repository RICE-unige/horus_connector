import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_exact_matching_does_not_label_an_old_frame_with_a_new_marker():
    from media_health import FrameMatcher
    matcher = FrameMatcher(capacity=3)
    matcher.packet(100, 7)
    assert matcher.marker({"rtp_timestamp": 8, "sent_ns": 200}) is None
    assert matcher.decoded(100, {"received": 1000}) is None
    matched = matcher.marker({"rtp_timestamp": 7, "sent_ns": 1})
    assert matched == ({"rtp_timestamp": 7, "sent_ns": 1}, {"received": 1000})


def test_frame_correlation_is_bounded_and_handles_reordering():
    from media_health import FrameMatcher
    matcher = FrameMatcher(capacity=2)
    for index in range(10):
        matcher.packet(index, index)
        matcher.marker({"rtp_timestamp": index})
    assert len(matcher.packets) == len(matcher.markers) == 2
    assert matcher.decoded(0, {}) is None
    assert matcher.decoded(9, {}) == ({"rtp_timestamp": 9}, {})


def test_constrained_bitrate_allocation_reports_actual_total():
    from media_health import allocate_bitrates
    streams = [{"min_bitrate_kbit": 500, "max_bitrate_kbit": 600, "bitrate_kbit": 550},
               {"min_bitrate_kbit": 100, "max_bitrate_kbit": 5000, "bitrate_kbit": 3000}]
    with pytest.raises(ValueError, match="minima"):
        allocate_bitrates(streams, 1)
    for target in (600, 601, 2000, 10000):
        assigned = allocate_bitrates(streams, target)
        assert sum(assigned) == max(600, min(target, 5600))
        assert 500 <= assigned[0] <= 600 and 100 <= assigned[1] <= 5000


def test_connected_signaling_caller_never_performs_socket_send():
    from gst_webrtc_common import JsonSignaling
    signaling = JsonSignaling(lambda _: None)
    signaling.ws = SimpleNamespace(send=lambda _: pytest.fail("Caller performed socket I/O"))
    signaling._begin_connection()
    signaling.send({"type": "ice"})
    assert not signaling.pending.empty()
    signaling.ws = None
    signaling.close()


def test_direct_signaling_replies_with_compatible_registration():
    from gst_webrtc_common import ServerSignaling
    signaling = ServerSignaling("127.0.0.1", 0, lambda _: None)
    signaling._dispatch_text('{"type":"register","role":"robot","room":"test"}')
    _, text = signaling.pending.get_nowait()
    assert json.loads(text)["peer_ready"] is True
    signaling.close()


def test_signaling_expires_old_and_superseded_negotiations():
    from gst_webrtc_common import JsonSignaling
    signaling = JsonSignaling(lambda _: None)
    delivered = []
    signaling.ws = SimpleNamespace(send=delivered.append)
    signaling._begin_connection()
    signaling.pending.put((time.monotonic() - 60, '{"type":"offer"}'))
    signaling.negotiation_id = "new"
    signaling.send({"type": "ice", "negotiation_id": "old"})
    signaling.send({"type": "offer"})
    signaling._flush_pending()
    assert len(delivered) == 1
    assert json.loads(delivered[0])["negotiation_id"] == "new"
    signaling.ws = None
    signaling.close()


def test_client_writer_waits_for_registration_ack():
    from gst_webrtc_common import JsonSignaling
    delivered = []
    signaling = JsonSignaling(lambda _: None, wait_for_registration=True)
    signaling.ws = SimpleNamespace(send=delivered.append)
    generation = signaling._begin_connection()
    signaling.send({"type": "offer"})
    signaling._flush_pending()
    assert delivered == [] and not signaling.pending.empty()
    signaling._dispatch_text('{"type":"registered"}', generation)
    signaling._flush_pending()
    assert len(delivered) == 1
    signaling.ws = None
    signaling.close()


def test_sender_registration_is_the_single_reconnect_startup_owner(monkeypatch):
    from gst_webrtc_h264_robot import H264RobotSender, GLib
    sender = H264RobotSender.__new__(H264RobotSender)
    sender.stopped, sender.relay_mode, sender.peer_ready = False, True, True
    sender.pipeline = object()
    sender.args = SimpleNamespace(ice_servers="")
    starts = []
    sender._start_relay_media_session = lambda force_rebuild=False: starts.append(force_rebuild)
    monkeypatch.setattr(GLib, "idle_add", lambda *_: pytest.fail("Duplicate relay startup callback"))
    sender._on_signaling_connected()
    sender._handle_signaling_message_in_loop({"type": "registered", "peer_ready": True,
                                              "peer_session_id": "machine-session"})
    sender._handle_signaling_message_in_loop({"type": "peer-ready", "role": "machine",
                                              "peer_session_id": "machine-session"})
    assert starts == [True]
    sender._handle_signaling_message_in_loop({"type": "peer-ready", "role": "machine",
                                              "peer_session_id": "replacement"})
    assert starts == [True, True]


def test_late_peer_ready_does_not_destroy_its_already_received_offer():
    from gst_webrtc_h264_machine import H264MachineReceiver
    receiver = H264MachineReceiver.__new__(H264MachineReceiver)
    receiver.stopped = False
    receiver.peer_session_id = "sender-session"
    receiver.last_remote_offer_sdp = "already received fresh offer"
    recoveries = []
    receiver._recover_pipeline = lambda **kwargs: recoveries.append(kwargs)
    receiver._handle_signaling_message_in_loop({"type": "peer-ready", "role": "robot",
                                                "peer_session_id": "sender-session"})
    assert recoveries == []
    receiver._handle_signaling_message_in_loop({"type": "peer-ready", "role": "robot",
                                                "peer_session_id": "replacement"})
    assert recoveries == [{"request_offer": False}]


def test_relay_peer_notifications_identify_the_actual_endpoint_session():
    from webrtc_signal_relay import Relay
    class Socket:
        def __init__(self):
            self.messages = []
        async def send(self, text):
            self.messages.append(json.loads(text))
    relay = Relay()
    robot, machine = Socket(), Socket()
    async def exercise():
        await relay.register(robot, "robot", "demo", "robot", "robot-session")
        await relay.register(machine, "machine", "demo", "machine", "machine-session")
    asyncio.run(exercise())
    assert machine.messages[0]["peer_session_id"] == "robot-session"
    assert robot.messages[-1]["peer_session_id"] == "machine-session"


def test_relay_rejects_expired_offer_on_registration():
    from webrtc_signal_relay import Relay
    class Socket:
        def __init__(self):
            self.messages = []
        async def send(self, text):
            self.messages.append(json.loads(text))
    relay = Relay(pending_ttl_sec=30)
    relay.pending[("demo", "machine")] = [(time.monotonic() - 120, '{"type":"offer"}')]
    socket = Socket()
    asyncio.run(relay.register(socket, "machine", "demo", "machine", "test"))
    assert [message["type"] for message in socket.messages] == ["registered"]


def test_track_mapping_is_exact():
    from gst_webrtc_h264_machine import H264MachineReceiver
    receiver = H264MachineReceiver.__new__(H264MachineReceiver)
    receiver.streams = [{"id": "front"}, {"id": "front_wide"}]
    receiver.attached_streams = set()
    pad = SimpleNamespace(find_property=lambda _: True, get_property=lambda _: "front_wide track-id", get_name=lambda: "src_0")
    assert receiver._stream_for_pad(pad)["id"] == "front_wide"
    pad.get_property = lambda _: "unknown"
    assert receiver._stream_for_pad(pad) is None
    pad.get_property = lambda _: ""
    assert receiver._stream_for_pad(pad) is None
    receiver.streams = [{"id": "only_camera"}]
    assert receiver._stream_for_pad(pad)["id"] == "only_camera"


def test_probe_profiles_follow_configured_camera_sizes(tmp_path, monkeypatch):
    from media_runtime import stream_profiles
    config = tmp_path / "streams.json"
    config.write_text(json.dumps({"version": 2, "session": {"mode": "bundled"}, "streams": [
        {"id": "front", "width": 1920, "height": 1080, "fps": 60},
        {"id": "rear", "width": 640, "height": 480, "fps": 15}]}))
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))
    assert stream_profiles(tmp_path, "robot") == [(640, 480, 15), (1920, 1080, 60)]


def test_fleet_false_and_session_room_are_isolated(tmp_path):
    from horus_fleet import load_config, member_env
    env = member_env({}, {"name": "demo", "room": "room-b", "zenoh_enabled": False}, "machine")
    assert env["HORUS_ZENOH_ENABLED"] == "0"
    assert env["HORUS_WEBRTC_SESSION_ROOM"] == "room-b"
    source = tmp_path / "fleet.json"
    source.write_text(json.dumps([{"name": "robot one"}, {"name": "robot_one"}]))
    with pytest.raises(ValueError, match="duplicate"):
        load_config(source)


def test_horus_reply_routes_cover_sdk_contract():
    import re
    from dds_preflight import load_allow_patterns
    root = Path(__file__).resolve().parents[1]
    for profile, direction in (("machine", "publishers"), ("robot", "subscribers")):
        patterns = load_allow_patterns(root / f"config/zenoh_{profile}.json5")[direction]
        for topic in ("registration_ack", "heartbeat", "teleop/runtime_state", "robot_description/request",
                      "multi_operator/sdk_registration_replay_request", "multi_operator_presence"):
            assert any(re.fullmatch(pattern, "/horus/" + topic) for pattern in patterns)


def test_relay_retains_latest_sdp_when_candidate_queue_fills():
    from webrtc_signal_relay import Relay
    relay = Relay(max_pending_per_peer=2)
    key = ("test", "machine")
    for message in [{"type": "offer", "negotiation_id": "current"}, *[{"type": "ice", "candidate": index} for index in range(10)]]:
        relay._queue_pending_locked(key, json.dumps(message))
    assert json.loads(relay.pending[key][0][1])["type"] == "offer"
    assert len(relay.pending[key]) == 2


def test_fleet_rollback_does_not_stop_preexisting_service(tmp_path, monkeypatch):
    import horus_fleet
    run = tmp_path / "run"
    run.mkdir()
    (run / "metrics.pid").write_text("100")
    (run / "metrics.pid.start").write_text("200")
    (run / "signal.pid").write_text("300")
    (run / "signal.pid.start").write_text("400")
    receipt = tmp_path / "receipt"
    receipt.write_text("signal\t300\t400\nmetrics\t999\t777\n")
    calls = []
    monkeypatch.setattr(horus_fleet, "run_horus", lambda *args: calls.append(args[-1]))
    horus_fleet.rollback_launches(tmp_path, [(tmp_path / "env", run, receipt)])
    assert calls == [["stop", "--services", "signal"]]


def test_fleet_environment_quotes_roundtrip(tmp_path):
    from horus_fleet import load_env, write_env
    values = {"NAME": "demo 'front' \"rear\"", "EDGE": "'literal'", "BOOL": "0"}
    path = tmp_path / "env"
    write_env(path, values)
    assert load_env(path) == values


def test_signaling_overflow_preserves_current_offer(monkeypatch):
    from gst_webrtc_common import JsonSignaling
    monkeypatch.setenv("HORUS_WEBRTC_SIGNAL_PENDING_MAX", "3")
    signaling = JsonSignaling(lambda _: None)
    signaling.send({"type": "offer"})
    for index in range(20):
        signaling.send({"type": "ice", "candidate": index})
    entries = list(signaling.pending.queue)
    assert len(entries) == 3
    assert json.loads(entries[0][1])["type"] == "offer"
    signaling.close()


def test_recovery_backoff_caps_and_resets():
    from media_health import RecoveryBackoff
    backoff = RecoveryBackoff()
    assert [backoff.next_delay() for _ in range(8)] == [2, 4, 8, 16, 30, 30, 30, 30]
    backoff.reset()
    assert backoff.next_delay() == 2


def test_status_write_error_does_not_kill_media(tmp_path, caplog):
    from media_health import write_status
    parent = tmp_path / "not_a_directory"
    parent.touch()
    for _ in range(5):
        write_status(parent / "health.json", {"state": "running"})
    assert sum("Could not save media health" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("name", ["signaling", "launcher.lock", ".", ".."])
def test_fleet_names_cannot_collide_with_launcher_state(tmp_path, name):
    from horus_fleet import load_config
    config = tmp_path / "fleet.json"
    config.write_text(json.dumps([{"name": name}]))
    with pytest.raises(ValueError, match="invalid"):
        load_config(config)


def test_missing_explicit_stream_config_fails_before_launch(tmp_path, monkeypatch):
    from webrtc_streams import load_document
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", "does-not-exist.json")
    with pytest.raises(ValueError, match="not found"):
        load_document(tmp_path)


def test_negative_benchmark_timing_cannot_be_fresh():
    from run_distributed_benchmark import recompute_samples
    payload = {"samples": [{"raw_latency_ms": 5}, {"raw_latency_ms": -1}, {"raw_latency_ms": 200}]}
    clock = {"offset_ms": 0, "drift_ms": 0, "pre": {"offset_ms": 0}, "post": {"offset_ms": 0}}
    samples, latencies, count = recompute_samples(payload, clock)
    assert count == 1 and latencies == [5, 200]
    assert not samples[1]["timing_valid"] and not samples[1]["fresh"]


def test_isolated_stream_preserves_camera_metadata_configuration(tmp_path, monkeypatch):
    from gst_webrtc_h264_robot import H264RobotSender
    from gst_webrtc_h264_machine import H264MachineReceiver
    config = tmp_path / "cameras.json"
    config.write_text(json.dumps({"streams": [{"id": "front", "room": "front-room",
        "input_camera_info_topic": "/camera/info", "output_camera_info_topic": "/remote/info",
        "preserve_source_header": True}]}))
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))
    for endpoint in (H264RobotSender, H264MachineReceiver):
        app = endpoint.__new__(endpoint)
        app.args = SimpleNamespace(streams_config=str(config), root=str(tmp_path), stream_id="front")
        session, streams = app._load_stream_configuration()
        assert session["mode"] == "isolated" and session["room"] == "front-room"
        assert len(streams) == 1 and streams[0]["preserve_source_header"]
        assert streams[0]["output_camera_info_topic"] == "/remote/info"


def test_renderer_excludes_endpoint_only_measurement_from_capture_sla(monkeypatch):
    import render_transport_benchmark as renderer
    monkeypatch.setattr(renderer, "read_json", lambda _: {"measurement_stage": "sender_rtp_frame_to_decoded_output",
                       "video_decoded_fps": 20, "video_latency_ms_p95": 2})
    result = renderer.webrtc_result("720p30", "lan")
    assert result["status"] == "endpoint_timing_only" and "fresh_frame_sla_percent" not in result
    assert renderer.corrected_stats([{"latency_ms": -1, "fresh": True}, {"latency_ms": 50}])["fresh_samples"] == 1
