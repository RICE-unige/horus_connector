"""Check the robot's configured cameras in the same ROS environment as its sender."""

import argparse
import os
from pathlib import Path
import time

from media_health import write_status
from webrtc_streams import load_streams

IMAGE = "sensor_msgs/msg/Image"


def missing_frame_state(endpoints, compatible):
    if not endpoints:
        return "no_publisher"
    raw = [endpoint for endpoint in endpoints if endpoint.topic_type == IMAGE]
    if not raw:
        return "wrong_type"
    return "waiting_for_frames" if any(compatible(endpoint) for endpoint in raw) else "qos_mismatch"


def sample_cameras(streams, duration):
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSCompatibility, qos_check_compatible
    from sensor_msgs.msg import Image
    from ros_image_io import validate_raw_image, qos_profile

    ctx = Context()
    rclpy.init(context=ctx)
    node = executor = None
    tracks = {s["id"]: {"topic": s["input_topic"], "state": "waiting_for_frames"} for s in streams}
    try:
        node = rclpy.create_node("horus_camera_preflight", context=ctx, enable_rosout=False)
        executor = SingleThreadedExecutor(context=ctx)
        executor.add_node(node)
        qos = {}

        def receive(message, track):
            if track["state"] == "sample_received":
                return
            try:
                validate_raw_image(message)
            except ValueError as exc:
                track.update(state="invalid_frame", detail=str(exc))
            else:
                track.update(state="sample_received", detail="")

        for stream in streams:
            key = stream["id"]
            qos[key] = qos_profile(stream["ros_image_qos"])
            track = tracks[key]
            node.create_subscription(Image, track["topic"], lambda msg, t=track: receive(msg, t), qos[key])
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and any(t["state"] != "sample_received" for t in tracks.values()):
            executor.spin_once(timeout_sec=min(.1, max(0, deadline - time.monotonic())))

        for key, track in tracks.items():
            endpoints = node.get_publishers_info_by_topic(track["topic"])
            track["publisher_types"] = sorted({endpoint.topic_type for endpoint in endpoints})
            if track["state"] == "waiting_for_frames":
                track["state"] = missing_frame_state(
                    endpoints, lambda endpoint: qos_check_compatible(endpoint.qos_profile, qos[key])[0]
                    != QoSCompatibility.ERROR,
                )
        alternatives = sorted(
            topic for topic, types in node.get_topic_names_and_types()
            if IMAGE in types and any(endpoint.topic_type == IMAGE for endpoint in node.get_publishers_info_by_topic(topic))
        )
        return {"tracks": tracks, "visible_raw_topics": alternatives}
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        ctx.try_shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=4)
    args = parser.parse_args()
    if not 0 < args.timeout <= 15:
        parser.error("camera probe timeout must be in (0, 15] seconds")
    streams = [stream for stream in load_streams(args.root, "robot") if stream["video_source"] == "ros2"]
    if not streams:
        write_status(args.status, {"scope": "local_camera_only", "tracks": {}})
        return
    domain = os.environ.get("ROS_DOMAIN_ID", "0")
    try:
        result = sample_cameras(streams, args.timeout)
    except Exception as exc:
        result = {"tracks": {s["id"]: {"topic": s["input_topic"], "state": "ros_unavailable",
                                      "detail": str(exc)} for s in streams}}
    write_status(args.status, {"scope": "local_camera_only", "domain": domain, **result})
    for track in result["tracks"].values():
        topic, state = track["topic"], track["state"]
        if state == "sample_received":
            print(f"Camera {topic}: received a ROS frame (domain {domain}); remote delivery not checked.")
        else:
            detail = track.get("detail") or ", ".join(track.get("publisher_types", ()))
            suffix = f" ({detail})" if detail else ""
            print(f"Warning Camera {topic}: {state.replace('_', ' ')}{suffix} in domain {domain}. "
                  "Video will wait/retry; other services can still start.")
    visible = result.get("visible_raw_topics", [])
    if visible and any(t["state"] != "sample_received" for t in result["tracks"].values()):
        print("Visible raw image topics (not auto-selected): " + ", ".join(visible[:8]))


if __name__ == "__main__":
    main()
