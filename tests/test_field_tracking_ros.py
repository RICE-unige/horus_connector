"""Opt-in real ROS node regression; loopback only, never a physical headset."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_live_relay_paused_mapping_poses_but_kept_video_and_recovered(monkeypatch):
    if os.environ.get("HORUS_FIELD_ROS_TEST") != "1":
        pytest.skip("Opt in on an isolated ROS domain with HORUS_FIELD_ROS_TEST=1")
    rclpy = pytest.importorskip("rclpy")
    import field_teammate_hololens_relay as relay

    args = relay.build_parser().parse_args([
        "--hololens-host", "127.0.0.1", "--no-raw-image", "--connect-timeout", "0.25"])
    endpoint = relay.HoloLensRelayEndpoint("127.0.0.1", 1, 1, 1)

    def exercise(node):
        # Stop loopback retry workers; exercise the real ROS publishers/methods.
        node.stop()
        node._stop.clear()
        monkeypatch.setattr(relay, "_hands_from_pose", lambda _payload: pytest.fail("Hand parsing blocked the head path"))
        head = {"timestamp": 10.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                "confidence": 1.0, "tracking_valid": True, "tracking_origin_revision": 1}
        node._publish_pose(head)
        assert node._pose_count == 1 and len(node._pose_history) == 1
        assert node._hand_samples.qsize() == 1
        assert node._head_pose_pub.qos_profile.depth == 1
        node._publish_pose({**head, "tracking_valid": False, "confidence": 0.0})
        assert node._pose_count == 1 and not node._pose_history
        assert node._last_confidence == 0.0
        video = {"timestamp": 10.0, "tracking_valid": False, "tracking_origin_revision": 1}
        node._publish_video(video, b"protocol-test-frame-not-a-real-jpeg")
        assert node._video_count == 1 and node._video_pose_count == 0
        assert node._last_confidence == 0.0
        node._publish_pose({**head, "timestamp": 11.0, "tracking_origin_revision": 2})
        assert node._pose_count == 2 and node._last_confidence == 1.0
        node._publish_video({**video, "tracking_valid": True, "camera_pose": head}, b"retired-epoch")
        assert node._video_pose_count == 0
        node._publish_video({**video, "timestamp": 11.0, "tracking_origin_revision": 2,
                             "tracking_valid": True, "camera_pose": head}, b"protocol-test-frame")
        assert node._video_count == 3 and node._video_native_pose_count == 1
        assert node._video_pose_count == 1 and node._video_fallback_pose_count == 0

    monkeypatch.setattr(rclpy, "spin", exercise)
    assert relay._run_live(args, endpoint) == 0
