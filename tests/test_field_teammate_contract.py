import sys
from pathlib import Path


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


def test_video_pose_fallback_uses_nearest_head_pose_on_capture_clock():
    import field_teammate_hololens_relay as relay

    samples = [
        relay.TimedCameraPose(10.00, 20.00, (1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0)),
        relay.TimedCameraPose(10.04, 20.04, (4.0, 5.0, 6.0), (0.0, 0.0, 0.1, 0.995)),
    ]

    assert relay._nearest_camera_pose(samples, 10.03) == samples[1]
    assert relay._nearest_camera_pose(samples, 10.20) is None
    assert relay._nearest_camera_pose(samples, float("nan")) is None
