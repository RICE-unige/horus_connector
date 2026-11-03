#!/usr/bin/env python3
"""Local ROS/H.264 acceptance: calibration, pacing, silence and receiver reset."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rclpy
from rclpy.executors import SingleThreadedExecutor
from sensor_msgs.msg import CameraInfo, Image
from gst_webrtc_h264_machine import GLib, H264MachineReceiver, parse_args
from ros_image_io import qos_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--letterbox", action="store_true")
    parser.add_argument("--latched-info", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config = args.output / "streams.json"
    config.write_text(json.dumps({"session": {"mode": "bundled", "room": "ros-acceptance"},
        "streams": [{"id": "front", "video_source": "ros2", "input_topic": "/acceptance/input",
            "output_topic": "/acceptance/output", "input_camera_info_topic": "/acceptance/input_info",
            "output_camera_info_topic": "/acceptance/output_info", "preserve_source_header": True,
            "width": 80, "height": 80 if args.letterbox else 60, "fps": 15, "bitrate_kbit": 1000}]}))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    rclpy.init()
    node = rclpy.create_node("connector_ros_acceptance")
    images = node.create_publisher(Image, "/acceptance/input", qos_profile("auto"))
    info_qos = qos_profile("default" if args.latched_info else "auto")
    if args.latched_info:
        from rclpy.qos import DurabilityPolicy
        info_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
    calibration = node.create_publisher(CameraInfo, "/acceptance/input_info", info_qos)
    received, infos, sent = [], [], set()
    running = [True]
    count = [0]
    def publish():
        if not running[0]:
            return
        msg = Image()
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.header.frame_id = "source_optical_frame"
        msg.width, msg.height, msg.step, msg.encoding = 160, 120, 480, "rgb8"
        msg.data = bytes([220, 20, 40] if count[0] % 2 else [20, 40, 220]) * (160 * 120)
        info = CameraInfo()
        info.header = msg.header
        info.width, info.height = 160, 120
        info.distortion_model = "plumb_bob"
        info.k = [100., 0., 80., 0., 100., 60., 0., 0., 1.]
        info.r = [1., 0., 0., 0., 1., 0., 0., 0., 1.]
        info.p = [100., 0., 80., 0., 0., 100., 60., 0., 0., 0., 1., 0.]
        sent.add((msg.header.stamp.sec, msg.header.stamp.nanosec))
        if not args.latched_info or count[0] == 0:
            calibration.publish(info)
        images.publish(msg)
        count[0] += 1
    def collect(msg):
        received.append((time.monotonic(), msg.header.frame_id,
                         (msg.header.stamp.sec, msg.header.stamp.nanosec), msg.width, msg.height,
                         tuple(msg.data[(msg.height // 2) * msg.step + (msg.width // 2) * 3:
                                        (msg.height // 2) * msg.step + (msg.width // 2) * 3 + 3])))
        if args.letterbox:
            assert max(msg.data[:3]) < 10, "Letterbox padding is not black"
    node.create_subscription(Image, "/acceptance/output", collect, qos_profile("auto"))
    node.create_subscription(CameraInfo, "/acceptance/output_info", infos.append, qos_profile("auto"))
    node.create_timer(1 / 60, publish)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin)
    thread.start()
    evidence = {}
    log = (args.output / "sender.log").open("w")
    process = None
    receiver = None
    try:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/gst_webrtc_h264_robot.py"),
            "--streams-config", str(config), "--signaling-url", f"ws://127.0.0.1:{port}",
            "--adaptive-bitrate", "0", "--duration", "35", "--profile",
            str(ROOT / "tests/fixtures/webrtc_cpu_test.env")], cwd=ROOT, stdout=log,
            stderr=subprocess.STDOUT, env=os.environ)
        with patch.object(sys, "argv", ["receiver", "--streams-config", str(config), "--host", "127.0.0.1",
            "--port", str(port), "--video-output", "ros2", "--duration", "28",
            "--latency-json", str(args.output / "receiver.json"), "--clock-offset-ms", "0",
            "--profile", str(ROOT / "tests/fixtures/webrtc_cpu_test.env")]):
            receiver = H264MachineReceiver(parse_args())
        def pause():
            running[0] = False
            evidence["before_pause"] = len(received)
            evidence["generation_before_pause"] = receiver.generation
            return False
        def resume():
            evidence["at_resume"] = len(received)
            evidence["generation_after_silence"] = receiver.generation
            running[0] = True
            return False
        def reset():
            evidence["before_reset"] = len(received)
            receiver._recover_pipeline()
            return False
        GLib.timeout_add_seconds(7, pause)
        GLib.timeout_add_seconds(14, resume)
        GLib.timeout_add_seconds(19, reset)
        receiver.start()
    finally:
        if receiver is not None:
            receiver.stop()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=3)
        node.destroy_node()
        rclpy.shutdown()
        log.close()
    evidence.update(source_frames=count[0], received_frames=len(received), camera_info_frames=len(infos),
                    frames_after_reset=len(received) - evidence.get("before_reset", len(received)),
                    calibration_samples=[{"width": info.width, "height": info.height, "k": list(info.k)} for info in infos[:3]])
    (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2))
    assert evidence["before_pause"] >= 30, evidence
    assert evidence["at_resume"] - evidence["before_pause"] <= 15, evidence
    assert evidence["generation_before_pause"] == evidence["generation_after_silence"], evidence
    assert evidence["frames_after_reset"] >= 30, evidence
    target_height, cy = (80, 40.) if args.letterbox else (60, 30.)
    assert infos and all(msg.width == 80 and msg.height == target_height and list(msg.k) == [50., 0., 40., 0., 50., cy, 0., 0., 1.] for msg in infos)
    assert all(frame == "source_optical_frame" and stamp in sent and width == 80 and height == target_height
               for _time, frame, stamp, width, height, _pixel in received)
    assert any(pixel[0] > 150 for *_rest, pixel in received), "No decoded red source pixels"
    assert any(pixel[2] > 150 for *_rest, pixel in received), "No decoded blue source pixels"
    print("ROS_CAMERA_ACCEPTANCE " + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
