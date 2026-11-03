import io
import json
import math
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_field_teammate_relay_contract_matches_v1_topic_shapes():
    import field_teammate_hololens_relay as relay

    contract = relay._topic_contract("field_teammate_1")

    assert contract["contract_version"] == "field_teammate.v1"
    assert contract["tf_topic"] == "/tf"
    assert contract["base_frame"] == "field_teammate_1/base"
    assert contract["camera_frame"] == "field_teammate_1/camera"
    assert contract["publishes"] == {
        "status": "/field_teammate_1/status",
        "localization_confidence": "/field_teammate_1/localization_confidence",
        "first_person_video_raw": "/field_teammate_1/fpv/image_raw",
        "first_person_video": "/field_teammate_1/fpv/image_raw/compressed",
        "first_person_video_camera_info": "/field_teammate_1/fpv/camera_info",
        "first_person_video_camera_pose": "/field_teammate_1/fpv/camera_pose",
        "guidance_response": "/field_teammate_1/guidance/response",
        "guidance_state": "/field_teammate_1/guidance/state",
    }
    assert contract["subscribes"] == {
        "guidance_request": "/field_teammate_1/guidance/request",
        "guidance_annotation": "/field_teammate_1/guidance/annotation",
        "guidance_route": "/field_teammate_1/guidance/route",
        "guidance_warning": "/field_teammate_1/guidance/warning",
        "audio_message": "/field_teammate_1/audio/message",
    }


def test_field_teammate_relay_sanitizes_names_like_the_sdk_contract():
    import field_teammate_hololens_relay as relay

    contract = relay._topic_contract("field-teammate 1/dev")

    assert contract["base_frame"] == "field_teammate_1_dev/base"
    assert contract["camera_frame"] == "field_teammate_1_dev/camera"
    assert contract["publishes"]["first_person_video"] == (
        "/field_teammate_1_dev/fpv/image_raw/compressed"
    )
    assert contract["publishes"]["first_person_video_raw"] == (
        "/field_teammate_1_dev/fpv/image_raw"
    )
    assert contract["publishes"]["first_person_video_camera_info"] == (
        "/field_teammate_1_dev/fpv/camera_info"
    )
    assert contract["publishes"]["first_person_video_camera_pose"] == (
        "/field_teammate_1_dev/fpv/camera_pose"
    )
    assert contract["subscribes"]["audio_message"] == (
        "/field_teammate_1_dev/audio/message"
    )


def test_zenoh_teammate_profile_declares_remote_transport_scope():
    text = (ROOT / "config" / "zenoh_teammate.json5").read_text(encoding="utf-8")

    # Transported observability path.
    assert "^/[^/]+/status$" in text
    assert "^/[^/]+/localization_confidence$" in text
    assert "^/[^/]+/fpv/image_raw/compressed$" in text
    assert "^/[^/]+/fpv/camera_info$" in text
    assert "^/[^/]+/fpv/camera_pose$" in text
    assert "^/[^/]+/guidance/response$" in text
    assert "^/[^/]+/guidance/state$" in text

    # Transported guidance downlink path.
    assert "^/[^/]+/guidance/request$" in text
    assert "^/[^/]+/guidance/annotation$" in text
    assert "^/[^/]+/guidance/route$" in text
    assert "^/[^/]+/guidance/warning$" in text

    # These are deliberately local-only in this profile.
    assert "^/[^/]+/fpv/image_raw$" not in text
    assert "^/[^/]+/audio/message$" not in text


def test_field_teammate_voice_config_is_automatic_and_tokenless_by_default():
    import field_teammate_hololens_relay as relay

    args = relay.build_parser().parse_args(
        ["--hololens-host", "192.168.1.30", "--voice-signaling-host", "192.168.1.20"]
    )
    command = relay._build_voice_config_command(args)

    assert command == {
        "type": "configure_voice",
        "signalingUrl": "ws://192.168.1.20:8765",
        "room": "field_teammate_1",
        "clientId": "field_teammate:field_teammate_1",
        "token": "",
        "trustedDevelopment": True,
        "autoConnect": True,
        "relayOnly": False,
        "fallbackIceServers": [],
    }


def test_mapping_video_profile_preserves_source_detail():
    import field_teammate_hololens_relay as relay

    args = relay.build_parser().parse_args(
        ["--hololens-host", "192.168.1.30", "--video-profile", "mapping"]
    )

    assert relay._build_video_profile_command(args) == {
        "type": "set_video_profile",
        "enabled": True,
        "profile": "mapping",
        "captureMode": "auto",
        "width": 640,
        "height": 360,
        "fps": 15,
        "jpegQuality": 80,
    }


def test_hololens_json_headers_are_bounded():
    import field_teammate_hololens_relay as relay

    assert relay._read_json_line(io.BytesIO(b'{"type":"pose"}\n')) == {"type": "pose"}
    with pytest.raises(ValueError, match="exceeds 64 KiB"):
        relay._read_json_line(io.BytesIO(b"x" * (64 * 1024 + 1)))


def test_video_pose_fallback_uses_nearest_head_pose_on_capture_clock():
    import field_teammate_hololens_relay as relay

    samples = [
        relay.TimedCameraPose(10.00, 20.00, (1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0)),
        relay.TimedCameraPose(10.04, 20.04, (4.0, 5.0, 6.0), (0.0, 0.0, 0.1, 0.995)),
    ]

    assert relay._nearest_camera_pose(samples, 10.03) == samples[1]
    assert relay._nearest_camera_pose(samples, 10.20) is None
    assert relay._nearest_camera_pose(samples, float("nan")) is None


@pytest.mark.parametrize("payload,valid", [
    ({}, True),
    ({"confidence": 1.0}, True),
    ({"tracking_valid": True, "confidence": 0.9}, True),
    ({"tracking_valid": False, "confidence": 1.0}, False),
    ({"tracking_valid": True, "confidence": 0.0}, False),
    ({"confidence": float("nan")}, False),
    ({"confidence": float("inf")}, False),
    ({"confidence": "invalid"}, False),
    ({"tracking_valid": "false"}, False),
])
def test_tracking_loss_is_not_advertised_as_a_live_pose(payload, valid):
    import field_teammate_hololens_relay as relay
    assert relay._pose_tracking_valid(payload) is valid


@pytest.mark.parametrize("revision", [0, 1, 100])
def test_revision_aware_video_never_fabricates_a_pose_during_recovery(revision):
    import field_teammate_hololens_relay as relay
    assert not relay._camera_pose_fallback_allowed(
        {"tracking_valid": False, "tracking_origin_revision": revision})
    assert relay._camera_pose_fallback_allowed({"tracking_valid": False})
    assert relay._camera_pose_fallback_allowed({})


@pytest.mark.parametrize("revision,active,expected", [
    (1, 1, True), (1, 2, False), (2, 1, False), (1, None, False),
    (None, None, False), (True, 1, False), ("1", 1, False), (-1, -1, False),
])
def test_delayed_video_pose_cannot_cross_tracking_epochs(revision, active, expected):
    import field_teammate_hololens_relay as relay
    assert relay._camera_pose_epoch_matches({"tracking_origin_revision": revision}, active) is expected
    assert relay._camera_pose_epoch_matches({}, active)


@pytest.mark.parametrize("head_z", [-.2, 0, .4, 1.61, 2.0])
def test_floor_clamp_never_moves_measured_camera_frame(head_z):
    import field_teammate_hololens_relay as relay
    base_z, camera_z = relay._pose_heights(head_z, 1.61, 0, "camera")
    assert base_z >= 0
    assert base_z + camera_z == pytest.approx(head_z)


def test_base_origin_retains_configured_eye_height():
    import field_teammate_hololens_relay as relay
    assert relay._pose_heights(.2, 1.61, 0, "base") == (.2, 1.61)


def measured_payload(**changes):
    return dict({"timestamp": 10., "tracking_valid": True, "tracking_origin_revision": 4,
        "position": {"x": 1., "y": -.2, "z": 2.},
        "forward": {"x": 0., "y": 0., "z": 1.},
        "up": {"x": 0., "y": 1., "z": 0.}}, **changes)


def test_atomic_head_packet_is_measured_pose_without_display_height():
    import field_teammate_hololens_relay as relay
    packet = relay._tracking_packet(measured_payload(), "map", "partner/head",
        SimpleNamespace(sec=100, nanosec=123), "relay-session", "unity_head")
    packet = json.loads(json.dumps(packet, allow_nan=False))
    assert packet["schema"] == "horus.field_tracking.v1"
    assert packet["position"] == [2., -1., -.2]
    assert packet["orientation_xyzw"] == pytest.approx([0., 0., 0., 1.])
    assert packet["header"] == {"frame_id": "map", "stamp": {"sec": 100, "nanosec": 123}}
    assert packet["source_timestamp_sec"] == 10.
    assert packet["tracking_origin_revision"] == 4 and packet["tracking_valid"] is True
    assert packet["child_frame_id"] == "partner/head" and packet["session_id"] == "relay-session"


@pytest.mark.parametrize("changes", [
    {"tracking_valid": False}, {"tracking_origin_revision": None},
    {"tracking_origin_revision": True}, {"confidence": 0.}, {"timestamp": math.nan},
    {"position": {"x": 0., "y": 0., "z": math.inf}},
    {"forward": {}}, {"forward": {"x": 0., "y": 1., "z": 0.}},
])
def test_atomic_invalid_packet_carries_invalidation_not_an_identity_or_fallback_pose(changes):
    import field_teammate_hololens_relay as relay
    packet = relay._tracking_packet(measured_payload(**changes), "map", "partner/head",
        SimpleNamespace(sec=100, nanosec=0), "relay", "unity_head")
    assert packet["tracking_valid"] is False
    assert packet["position"] is None and packet["orientation_xyzw"] is None
    json.dumps(packet, allow_nan=False)


def test_hand_mailbox_is_bounded_and_keeps_only_the_latest_actual_sample():
    import field_teammate_hololens_relay as relay
    mailbox = queue.Queue(maxsize=1)
    for index in range(1000):
        relay._queue_latest(mailbox, (index, index / 30))
    assert mailbox.qsize() == 1
    assert mailbox.get_nowait() == (999, 999 / 30)
    args = relay.build_parser().parse_args(["--hololens-host", "127.0.0.1", "--no-hand-tf"])
    assert args.no_hand_tf
