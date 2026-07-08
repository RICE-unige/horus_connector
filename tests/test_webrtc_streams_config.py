import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_webrtc_streams_fallback_keeps_single_stream_service(monkeypatch, tmp_path):
    import webrtc_streams

    monkeypatch.delenv("HORUS_STREAMS_CONFIG", raising=False)
    monkeypatch.setenv("HORUS_ROOM", "robot-a")
    monkeypatch.setenv("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "/camera/image_raw")
    monkeypatch.setenv("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC", "/robot_a/camera/webrtc/image_raw")

    streams = webrtc_streams.load_streams(tmp_path, "machine")

    assert len(streams) == 1
    assert streams[0]["service"] == "webrtc"
    assert streams[0]["room"] == "robot-a"
    assert streams[0]["output_topic"] == "/robot_a/camera/webrtc/image_raw"


def test_webrtc_streams_config_fans_out_named_services(monkeypatch, tmp_path):
    import webrtc_streams

    config = tmp_path / "streams.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "streams": [
                    {
                        "id": "front",
                        "room": "robot-a-front",
                        "input_topic": "/front/image_raw",
                        "output_topic": "/robot_a/camera/front/webrtc/image_raw",
                    },
                    {
                        "id": "rear",
                        "room": "robot-a-rear",
                        "input_topic": "/rear/image_raw",
                        "output_topic": "/robot_a/camera/rear/webrtc/image_raw",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))

    streams = webrtc_streams.load_streams(tmp_path, "robot")

    assert [stream["service"] for stream in streams] == ["webrtc-front", "webrtc-rear"]
    assert [stream["room"] for stream in streams] == ["robot-a-front", "robot-a-rear"]
    assert [stream["input_topic"] for stream in streams] == ["/front/image_raw", "/rear/image_raw"]


def test_webrtc_streams_rejects_duplicate_stream_ids(monkeypatch, tmp_path):
    import pytest
    import webrtc_streams

    config = tmp_path / "streams.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "streams": [
                    {"id": "primary", "label": "front", "room": "robot-a-front", "output_topic": "/robot_a/camera/front/webrtc/image_raw"},
                    {"id": "primary", "label": "rear", "room": "robot-a-rear", "output_topic": "/robot_a/camera/rear/webrtc/image_raw"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))

    with pytest.raises(ValueError, match="Duplicate stream id"):
        webrtc_streams.load_streams(tmp_path, "machine")


def test_webrtc_streams_rejects_duplicate_rooms(monkeypatch, tmp_path):
    import pytest
    import webrtc_streams

    config = tmp_path / "streams.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "streams": [
                    {"id": "front", "room": "robot-a", "output_topic": "/robot_a/camera/front/webrtc/image_raw"},
                    {"id": "rear", "room": "robot-a", "output_topic": "/robot_a/camera/rear/webrtc/image_raw"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))

    with pytest.raises(ValueError, match="Duplicate WebRTC room"):
        webrtc_streams.load_streams(tmp_path, "machine")


def test_webrtc_streams_rejects_duplicate_output_topics(monkeypatch, tmp_path):
    import pytest
    import webrtc_streams

    config = tmp_path / "streams.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "streams": [
                    {"id": "front", "room": "robot-a-front", "output_topic": "/robot_a/camera/webrtc/image_raw"},
                    {"id": "rear", "room": "robot-a-rear", "output_topic": "/robot_a/camera/webrtc/image_raw"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HORUS_STREAMS_CONFIG", str(config))

    with pytest.raises(ValueError, match="Duplicate output topic"):
        webrtc_streams.load_streams(tmp_path, "machine")
