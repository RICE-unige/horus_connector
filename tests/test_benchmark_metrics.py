import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_corrected_stats_do_not_subtract_percentile_baseline():
    import render_transport_benchmark as renderer

    stats = renderer.corrected_stats(
        [
            {"latency_ms": 1000.0, "fresh": False},
            {"latency_ms": 1010.0, "fresh": False},
            {"latency_ms": 1020.0, "fresh": False},
        ]
    )

    assert stats["fresh_samples"] == 0
    assert stats["latency_ms_p50"] == 1010.0
    assert stats["latency_ms_p95"] > 1010.0


def test_ros_result_uses_captured_frames_as_fresh_sla_denominator(tmp_path, monkeypatch):
    import render_transport_benchmark as renderer

    monkeypatch.setattr(renderer, "RUN_DIR", tmp_path)
    name = "modeb_720p30_lan_zenoh"
    (tmp_path / f"{name}_pub.json").write_text(json.dumps({"messages": 10, "payload_bytes": 1234}), encoding="utf-8")
    (tmp_path / f"{name}_sub.json").write_text(
        json.dumps(
            {
                "messages": 4,
                "estimated_published_frames": 10,
                "fresh_messages": 2,
                "fresh_frame_sla": 0.2,
                "usable_fps": 6.0,
                "observed_sec": 1.0,
                "fps": 4.0,
                "mbps": 1.2,
                "stale_messages": 2,
                "dropped_or_skipped_frames": 6,
                "clock_offset_ms": 3.5,
                "clock_offset_source": "clock_offset_probe:test.json",
                "latency_method": "ROS header stamp corrected by measured clock offset; no percentile-baseline subtraction",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"{name}_sub.samples.json").write_text(
        json.dumps(
            {
                "samples": [
                    {"latency_ms": 80.0, "fresh": True},
                    {"latency_ms": 90.0, "fresh": True},
                    {"latency_ms": 190.0, "fresh": False},
                    {"latency_ms": 240.0, "fresh": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = renderer.ros_result("720p30", "lan", "zenoh")

    assert result["fresh_frame_sla_percent"] == 20.0
    assert result["delivery_ratio"] == 0.4
    assert result["dropped_or_skipped_frames"] == 6
    assert result["stale_frames"] == 2


def test_webrtc_result_counts_unreceived_frames_against_sla(tmp_path, monkeypatch):
    import render_transport_benchmark as renderer

    monkeypatch.setattr(renderer, "RUN_DIR", tmp_path)
    monkeypatch.setattr(renderer, "DURATION_SEC", 10.0)
    monkeypatch.setattr(renderer, "TARGET_FPS", 10.0)
    name = "modeb_720p30_lan_webrtc"
    (tmp_path / f"{name}_latency.json").write_text(
        json.dumps(
            {
                "video_frames": 8,
                "video_decoded_fps": 8.0,
                "fresh_latency_samples": 4,
                "stale_latency_samples": 4,
                "frame_clock_dropped": 2,
                "latency_method": "WebRTC sender frame-clock timestamp corrected by measured clock offset; no percentile-baseline subtraction",
                "clock_offset_source": "mean_pre_post_clock_offset_probe",
                "samples": [
                    {"latency_ms": 80.0, "fresh": True},
                    {"latency_ms": 90.0, "fresh": True},
                    {"latency_ms": 100.0, "fresh": True},
                    {"latency_ms": 110.0, "fresh": True},
                    {"latency_ms": 180.0, "fresh": False},
                    {"latency_ms": 190.0, "fresh": False},
                    {"latency_ms": 210.0, "fresh": False},
                    {"latency_ms": 230.0, "fresh": False},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = renderer.webrtc_result("720p30", "lan")

    assert result["published_frames"] == 100
    assert result["received_frames"] == 8
    assert result["fresh_frame_sla_percent"] == 4.0
    assert result["delivery_ratio"] == 0.08
