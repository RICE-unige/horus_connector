import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_per_client_credentials_enforce_role_and_room(tmp_path):
    from webrtc_session import CredentialStore

    source = tmp_path / "credentials.json"
    source.write_text(
        json.dumps(
            {
                "clients": {
                    "robot:alpha": {
                        "token": "robot-secret",
                        "roles": ["robot"],
                        "rooms": ["alpha", "alpha-*"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore.from_file(str(source))

    assert store.authorized("robot:alpha", "robot-secret", "robot", "alpha")
    assert store.authorized("robot:alpha", "robot-secret", "robot", "alpha-front")
    assert not store.authorized("robot:alpha", "robot-secret", "machine", "alpha")
    assert not store.authorized("robot:alpha", "wrong", "robot", "alpha")
    assert not store.authorized("robot:alpha", "robot-secret", "robot", "beta")


def test_voice_credentials_fail_closed_without_communicative_capability(tmp_path):
    from webrtc_session import CredentialStore

    source = tmp_path / "credentials.json"
    source.write_text(
        json.dumps(
            {
                "clients": {
                    "field_teammate:alpha": {
                        "token": "field-secret",
                        "roles": ["field_teammate"],
                        "rooms": ["alpha"],
                    },
                    "field_teammate:bravo": {
                        "token": "field-secret-2",
                        "roles": ["field_teammate"],
                        "rooms": ["bravo"],
                        "capabilities": ["communicative"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    store = CredentialStore.from_file(str(source))

    assert not store.authorized(
        "field_teammate:alpha",
        "field-secret",
        "field_teammate",
        "alpha",
        required_capability="communicative",
    )
    assert store.authorized(
        "field_teammate:bravo",
        "field-secret-2",
        "field_teammate",
        "bravo",
        required_capability="communicative",
    )


def test_voice_rejects_legacy_shared_token_without_identity_credentials():
    from webrtc_signal_relay import Relay

    relay = Relay(auth_token="legacy-camera-token")

    assert not relay._is_authorized(
        {
            "role": "operator",
            "room": "study-a",
            "token": "legacy-camera-token",
        }
    )
    assert relay._is_authorized(
        {
            "role": "machine",
            "room": "study-a",
            "token": "legacy-camera-token",
        }
    )


def test_voice_trusted_development_accepts_tokenless_clients():
    from webrtc_signal_relay import Relay

    locked_relay = Relay()
    trusted_relay = Relay(allow_unauthenticated_voice=True)
    registration = {
        "role": "operator",
        "room": "field_teammate_1",
        "client_id": "operator:field_teammate_1",
        "token": "",
    }

    assert not locked_relay._is_authorized(registration)
    assert trusted_relay._is_authorized(registration)


def test_non_numeric_voice_protocol_version_is_rejected():
    from horus_voice_session import VoiceCallRegistry

    registry = VoiceCallRegistry()
    decision = registry.validate_and_apply(
        "study-a",
        "operator",
        {
            "type": "call_invite",
            "protocol_version": "not-a-number",
            "room": "study-a",
            "call_id": "call-12345678",
            "session_id": "session-12345678",
        },
    )

    assert not decision.accepted
    assert decision.reason == "voice protocol version mismatch"


def test_turn_credentials_match_coturn_rest_api():
    from webrtc_session import TurnCredentialIssuer

    issuer = TurnCredentialIssuer("turn.example.org", "shared-secret", ttl_sec=600)
    credential = issuer.issue("robot:alpha", now=1000.0)

    assert credential is not None
    assert credential.username == "1600:robot_alpha"
    expected = base64.b64encode(
        hmac.new(b"shared-secret", credential.username.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    assert credential.password == expected
    assert len(credential.urls) == 2
    assert all("turn.example.org:3478" in url for url in credential.urls)


def test_command_gate_rejects_stale_future_and_out_of_order_commands():
    from webrtc_control import CommandGate

    now = 10_000_000_000
    gate = CommandGate(max_age_ms=100, max_future_ms=100)
    base = {"type": "cmd_vel", "session_id": "session-a", "linear_x": 0.1}

    assert not gate.validate({**base, "seq": 0, "sent_ns": now - 200_000_000}, now).accepted
    assert not gate.validate({**base, "seq": 0, "sent_ns": now + 200_000_000}, now).accepted
    assert gate.validate({**base, "seq": 1, "sent_ns": now}, now).accepted
    assert not gate.validate({**base, "seq": 1, "sent_ns": now}, now).accepted
    assert not gate.validate({**base, "seq": 0, "sent_ns": now}, now).accepted
    assert gate.validate({**base, "seq": 2, "sent_ns": now}, now).accepted


def test_stats_controller_downshifts_immediately_and_upshifts_with_hysteresis():
    from webrtc_stats import BitrateController, NetworkSample

    controller = BitrateController(1000, 6000, 500, good_samples_required=3)
    target, reason = controller.target(4000, NetworkSample(packet_loss_fraction=0.10, round_trip_time_ms=100))
    assert (target, reason) == (3500, "network-congested")

    healthy = NetworkSample(packet_loss_fraction=0.0, round_trip_time_ms=50)
    assert controller.target(3500, healthy)[0] == 3500
    assert controller.target(3500, healthy)[0] == 3500
    assert controller.target(3500, healthy) == (4000, "network-healthy")
    assert controller.target(4000, NetworkSample()) == (4000, "hold")


def test_stats_extractor_reads_remote_inbound_video_metrics():
    from webrtc_stats import extract_network_sample

    report = {
        "remote-video": {
            "type": "remote-inbound-rtp",
            "kind": "video",
            "fraction-lost": 0.04,
            "round-trip-time": 0.125,
        },
        "outbound-video": {
            "type": "outbound-rtp",
            "kind": "video",
            "nack-count": 3,
            "pli-count": 1,
        },
    }
    sample = extract_network_sample(report)

    assert sample.packet_loss_fraction == 0.04
    assert sample.round_trip_time_ms == 125.0
    assert sample.nack_count == 3
    assert sample.pli_count == 1


def test_version_two_streams_share_one_bundled_room(monkeypatch, tmp_path):
    import webrtc_streams

    source = tmp_path / "streams.json"
    source.write_text(
        json.dumps(
            {
                "version": 2,
                "session": {"id": "alpha", "room": "alpha", "mode": "bundled"},
                "streams": [
                    {"id": "front", "room": "alpha", "output_topic": "/alpha/front"},
                    {"id": "rear", "room": "alpha", "output_topic": "/alpha/rear"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(source))

    session = webrtc_streams.load_session(tmp_path, "machine")
    streams = webrtc_streams.load_streams(tmp_path, "machine")

    assert session == {"version": 2, "mode": "bundled", "room": "alpha", "id": "alpha", "role": "machine"}
    assert [stream["room"] for stream in streams] == ["alpha", "alpha"]


def test_stream_config_rejects_invalid_video_budget(monkeypatch, tmp_path):
    import pytest
    import webrtc_streams

    source = tmp_path / "streams.json"
    source.write_text(
        json.dumps(
            {
                "version": 2,
                "streams": [
                    {
                        "id": "front",
                        "width": 640,
                        "height": 360,
                        "fps": 30,
                        "bitrate_kbit": 4000,
                        "min_bitrate_kbit": 5000,
                        "max_bitrate_kbit": 6000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(source))

    with pytest.raises(ValueError, match="bitrate_kbit must be within"):
        webrtc_streams.load_streams(tmp_path, "robot")


def test_signaling_ready_timer_cannot_activate_replaced_connection():
    from gst_webrtc_common import JsonSignaling

    callbacks = []
    signaling = JsonSignaling(lambda _message: None, lambda: callbacks.append("ready"))
    generation_one = signaling._begin_connection()
    generation_two = signaling._begin_connection()

    signaling._notify_ready(generation_one)
    assert callbacks == []
    signaling._notify_ready(generation_two)
    assert callbacks == ["ready"]


def test_relay_offer_coalesces_stale_session_signaling():
    from webrtc_signal_relay import Relay

    relay = Relay()
    target = ("alpha", "machine")
    relay._queue_pending_locked(target, json.dumps({"type": "ice", "session_id": "one"}))
    relay._queue_pending_locked(target, json.dumps({"type": "offer", "session_id": "one"}))

    queued = [json.loads(text) for _created_at, text in relay.pending[target]]
    assert queued == [{"type": "offer", "session_id": "one"}]


def test_voice_call_registry_enforces_invite_accept_negotiate_order():
    from horus_voice_session import VoiceCallRegistry

    registry = VoiceCallRegistry()
    base = {
        "protocol_version": 1,
        "room": "study-a",
        "call_id": "call-12345678",
        "session_id": "session-12345678",
    }
    field_base = {**base, "session_id": "session-field-12345678"}

    assert not registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "type": "call_accept"},
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "operator",
        {**base, "type": "call_invite"},
    ).accepted
    assert not registry.validate_and_apply(
        "study-a",
        "operator",
        {
            **base,
            "type": "session_description",
            "description_type": "offer",
            "sdp": "v=0",
        },
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "type": "call_accept"},
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "operator",
        {
            **base,
            "type": "session_description",
            "description_type": "offer",
            "sdp": "v=0",
        },
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "type": "call_connected"},
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "operator",
        {**base, "type": "mute_changed", "muted": True},
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "operator",
        {**base, "type": "call_end"},
    ).accepted
    assert "study-a" not in registry.calls


def test_voice_call_registry_rejects_stale_call_and_invalid_payload():
    from horus_voice_session import VoiceCallRegistry

    registry = VoiceCallRegistry()
    base = {
        "protocol_version": 1,
        "room": "study-a",
        "call_id": "call-12345678",
        "session_id": "session-12345678",
    }
    field_base = {**base, "session_id": "session-field-12345678"}
    assert registry.validate_and_apply(
        "study-a",
        "operator",
        {**base, "type": "call_invite"},
    ).accepted
    assert not registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "call_id": "call-87654321", "type": "call_accept"},
    ).accepted
    assert registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "type": "call_accept"},
    ).accepted
    assert not registry.validate_and_apply(
        "study-a",
        "field_teammate",
        {**field_base, "session_id": "session-stale-12345678", "type": "call_connected"},
    ).accepted
    assert not registry.validate_and_apply(
        "study-a",
        "operator",
        {**base, "type": "mute_changed", "muted": "yes"},
    ).accepted


def test_relay_pairs_operator_with_field_teammate():
    from webrtc_signal_relay import Relay

    assert Relay.target_role("operator") == "field_teammate"
    assert Relay.target_role("field_teammate") == "operator"
    assert Relay.target_role("robot") == "machine"
