import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_bulk_governor_requires_capable_runtime_and_keeps_errors_out_of_rate(tmp_path):
    import json
    text = (ROOT / "horus").read_text()
    function = "zenoh_bulk_rate() {" + text.split("zenoh_bulk_rate() {", 1)[1].split("\n}\n", 1)[0] + "\n}"
    script = ('set -e\nlog_error() { printf "%s\\n" "$*"; }\n'
              'zenoh_uses_docker() { return 1; }\n' + function + '\nrate=$(zenoh_bulk_rate)\nprintf "%s" "$rate"')
    env = dict(os.environ, ROOT=str(tmp_path), ZENOH_BRIDGE_BINARY=str(tmp_path / "bridge"),
               HORUS_ZENOH_BULK_BYTES_PER_SECOND="0", HORUS_ZENOH_BULK_BUDGET_FILE=str(tmp_path / "budget"))
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and not result.stdout and "Bulk pacing requires" in result.stderr
    receipt = tmp_path / "bridge.source.json"
    receipt.write_text(json.dumps({"new_files": []}))
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "no shared bulk-budget support" in result.stderr
    receipt.write_text(json.dumps({"new_files": ["zenoh-plugin-ros2dds/src/bulk_budget.rs"]}))
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == "0", result.stderr


def test_native_media_readiness_requires_current_process_health(tmp_path):
    import json
    import time
    from service_readiness import check
    from service_runner import process_start_time
    for name in ("source-media", "media-relay"):
        state = {"phase": "running", "child_pid": os.getpid(),
                 "child_start": process_start_time(os.getpid())}
        (tmp_path / f"{name}.service.json").write_text(json.dumps(state))
        assert not check(tmp_path, name)["ready"]
        health = {"pid": os.getpid(), "updated_wall_sec": time.time(),
                  "engine": "native_rtp_no_dds", "tracks": {}}
        (tmp_path / f"{name}.media.json").write_text(json.dumps(health))
        result = check(tmp_path, name)
        assert result["ready"] and not result["data_ready"]
        health["updated_wall_sec"] -= 10
        (tmp_path / f"{name}.media.json").write_text(json.dumps(health))
        assert not check(tmp_path, name)["ready"]


def test_source_media_status_does_not_claim_received_video(tmp_path):
    config = tmp_path / "streams.json"
    config.write_text('{"version":2,"streams":[]}')
    env_file = tmp_path / "robot.env"
    env_file.write_text(f"HORUS_ROLE=robot\nHORUS_SOURCE_MEDIA_ENABLED=1\n"
                        f"HORUS_STREAMS_CONFIG={config}\nHORUS_CLOUD_RUN_TURN=0\n"
                        "HORUS_TOPOLOGY=direct\nHORUS_MACHINE_IP=127.0.0.1\n")
    result = subprocess.run([str(ROOT / "horus"), "status"], cwd=ROOT, text=True,
                            env=dict(os.environ, HORUS_ENV=str(env_file), HORUS_RUN_DIR=str(tmp_path / "run")),
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "source-media" in result.stdout
    assert "not frame-delivery proof" in result.stdout
    assert "native H.264 / WebRTC" in result.stdout
    assert "1280x720" not in result.stdout


def test_priority_patch_is_pinned_and_keeps_original_history():
    import hashlib
    import json
    manifest = json.loads((ROOT / "config/zenoh-source.json").read_text())
    patch = (ROOT / manifest["patch"]).read_bytes()
    assert hashlib.sha256(patch).hexdigest() == manifest["patch_sha256"]
    assert len(manifest["commit"]) == 40
    assert b"+            None if independent_worker" in patch
    assert b"+                qos.history" not in patch
    assert b"+        Some((Priority::DataLow | Priority::Background, _))" in patch
    assert b"ros2_type == \"std_msgs/msg/UInt8MultiArray\"" in patch
    assert b"mod hmsh_queue" in patch
    assert b"+            if independent_worker {\n+                pace_bulk_sample(sample.len(), bulk_rate);\n+            }" in patch


def test_direct_camera_payloads_are_not_duplicated_over_zenoh():
    import re
    from transport_policy import resolve
    topics = ["/front/image_raw", "/front/image_raw/compressed", "/rear/image_raw"]
    result = resolve(ROOT / "config/zenoh_robot.json5", {"webrtc_only_topics": topics}, role="robot")
    patterns = result["plugins"]["ros2dds"]["allow"]["publishers"]
    for topic in topics:
        assert not any(re.fullmatch(pattern, topic) for pattern in patterns)
    for topic in ("/tf", "/tf_static", "/front/camera_info", "/front/image_raw_extra", "/map", "/mesh/indexed"):
        assert any(re.fullmatch(pattern, topic) for pattern in patterns)
    base = resolve(ROOT / "config/zenoh_machine.json5", {}, role="machine")
    machine = resolve(ROOT / "config/zenoh_machine.json5", {"webrtc_only_topics": topics}, role="machine")
    assert machine == base


def test_gstreamer_expands_inherited_transport_without_overriding_media():
    pytest.importorskip("gi")
    from gst_webrtc_common import Gst, make_session_description
    Gst.init(None)
    text = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
            "a=setup:active\r\na=ice-ufrag:parent\r\na=ice-pwd:parent-password\r\n"
            "a=fingerprint:sha-256 AB:CD\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            "a=mid:video0\r\na=setup:passive\r\na=rtpmap:96 H264/90000\r\n")
    description = make_session_description("answer", text)
    sdp = description.sdp
    media = sdp.get_media(0)
    assert media.get_attribute_val("setup") == "passive"
    assert media.get_attribute_val("ice-ufrag") == "parent"
    assert media.get_attribute_val("ice-pwd") == "parent-password"
    assert media.get_attribute_val("fingerprint") is None
    assert sdp.get_attribute_val("fingerprint") == "sha-256 AB:CD"
    assert sdp.get_attribute_val("setup") == "active"


def test_gstreamer_does_not_invent_missing_transport_attributes():
    pytest.importorskip("gi")
    from gst_webrtc_common import Gst, make_session_description
    Gst.init(None)
    description = make_session_description("offer", "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"
        "s=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:0\r\n")
    sdp = description.sdp
    media = sdp.get_media(0)
    assert media.get_attribute_val("setup") is None


def test_closed_relay_does_not_report_a_stale_negotiation():
    pytest.importorskip("rclpy")
    pytest.importorskip("gi")
    from source_media_receiver import Camera
    camera = Camera.__new__(Camera)
    camera.generation = 0
    camera.session_id = ""
    camera.offer_sdp = camera.answer_sdp = "old-session"
    camera.candidates = [{"candidate": "old-candidate"}]
    camera.connected = True
    camera.delivery = camera.bus = camera.pipeline = None
    camera.handlers = []
    camera.close()
    assert not camera.connected
    assert not camera.offer_sdp and not camera.answer_sdp and not camera.candidates
