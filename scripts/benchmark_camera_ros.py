#!/usr/bin/env python3
"""ROS 2 camera transport benchmark for raw and JPEG-compressed frames."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image

from experiment_metrics import CsvMetricWriter, default_metrics_path


def qos(depth: int) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


def make_frame(width: int, height: int, scene: str) -> np.ndarray:
    if scene == "noise":
        return np.random.default_rng(7).integers(0, 256, (height, width, 3), dtype=np.uint8)
    if scene == "textured":
        rng = np.random.default_rng(7)
        low_h = max(1, height // 24)
        low_w = max(1, width // 24)
        texture = rng.integers(0, 256, (low_h, low_w, 3), dtype=np.uint8)
        texture = cv2.resize(texture, (width, height), interpolation=cv2.INTER_CUBIC)
        x = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
        y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
        red = np.broadcast_to(x, (height, width))
        green = np.broadcast_to(y, (height, width))
        blue = ((red.astype(np.uint16) + green.astype(np.uint16)) // 2).astype(np.uint8)
        gradient = np.dstack((blue, green, red))
        frame = cv2.addWeighted(texture, 0.72, gradient, 0.28, 0)
        for offset in range(0, width, max(80, width // 18)):
            color = (int(offset * 3) % 255, int(offset * 5) % 255, int(offset * 7) % 255)
            cv2.line(frame, (offset, 0), (width - offset // 2, height - 1), color, 3)
        return frame

    x = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    red = np.broadcast_to(x, (height, width))
    green = np.broadcast_to(y, (height, width))
    blue = ((red.astype(np.uint16) + green.astype(np.uint16)) // 2).astype(np.uint8)
    return np.dstack((blue, green, red))


class CameraPublisher(Node):
    def __init__(self, args):
        super().__init__("horus_camera_benchmark_publisher")
        self.args = args
        self.started = time.monotonic()
        self.count = 0
        self.bytes_sent = 0
        self.frame = make_frame(args.width, args.height, args.scene)
        self.raw_bytes = self.frame.tobytes()
        self.jpeg_bytes = self._encode_jpeg()
        msg_type = Image if args.profile == "raw" else CompressedImage
        self.publisher = self.create_publisher(msg_type, args.topic, qos(args.qos_depth))
        self.create_timer(1.0 / args.fps, self.publish_frame)

    def _encode_jpeg(self) -> bytes:
        ok, encoded = cv2.imencode(".jpg", self.frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.args.jpeg_quality])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        return encoded.tobytes()

    def publish_frame(self):
        stamp = self.get_clock().now().to_msg()
        if self.args.profile == "raw":
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = "benchmark_camera"
            msg.height = self.args.height
            msg.width = self.args.width
            msg.encoding = "bgr8"
            msg.is_bigendian = 0
            msg.step = self.args.width * 3
            msg.data = self.raw_bytes
            byte_count = len(self.raw_bytes)
        else:
            msg = CompressedImage()
            msg.header.stamp = stamp
            msg.header.frame_id = "benchmark_camera"
            msg.format = "bgr8; jpeg compressed"
            msg.data = self.jpeg_bytes
            byte_count = len(self.jpeg_bytes)
        self.publisher.publish(msg)
        self.count += 1
        self.bytes_sent += byte_count

    def summary(self):
        elapsed = max(time.monotonic() - self.started, 0.001)
        return {
            "role": "pub",
            "profile": self.args.profile,
            "target_fps": self.args.fps,
            "width": self.args.width,
            "height": self.args.height,
            "scene": self.args.scene,
            "messages": self.count,
            "fps": self.count / elapsed,
            "mbps": self.bytes_sent * 8.0 / elapsed / 1_000_000.0,
            "payload_bytes": len(self.raw_bytes if self.args.profile == "raw" else self.jpeg_bytes),
            "elapsed_sec": elapsed,
        }


class CameraSubscriber(Node):
    def __init__(self, args):
        super().__init__("horus_camera_benchmark_subscriber")
        self.args = args
        self.started = time.monotonic()
        self.first = None
        self.last = None
        self.count = 0
        self.bytes_received = 0
        self.latencies_ms = []
        self.samples = []
        msg_type = Image if args.profile == "raw" else CompressedImage
        self.create_subscription(msg_type, args.topic, self.on_message, qos(args.qos_depth))

    def on_message(self, msg):
        now_mono = time.monotonic()
        now_ros = self.get_clock().now().nanoseconds
        sent_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        raw_latency_ms = (now_ros - sent_ns) / 1_000_000.0
        latency_ms = raw_latency_ms - self.args.clock_offset_ms
        if -1000.0 < latency_ms < 10_000.0:
            self.latencies_ms.append(latency_ms)
            self.samples.append(
                {
                    "seq": self.count,
                    "t_sec": now_mono - self.started,
                    "latency_ms": latency_ms,
                    "raw_latency_ms": raw_latency_ms,
                }
            )
        self.first = now_mono if self.first is None else self.first
        self.last = now_mono
        self.count += 1
        self.bytes_received += len(msg.data)

    def summary(self):
        observed = max((self.last or self.started) - (self.first or self.started), 0.001)
        values = sorted(self.latencies_ms)
        p95_index = min(len(values) - 1, int(round((len(values) - 1) * 0.95))) if values else 0
        p99_index = min(len(values) - 1, int(round((len(values) - 1) * 0.99))) if values else 0
        fresh_count = sum(1 for value in self.latencies_ms if value <= self.args.fresh_deadline_ms)
        return {
            "role": "sub",
            "profile": self.args.profile,
            "target_fps": self.args.fps,
            "width": self.args.width,
            "height": self.args.height,
            "scene": self.args.scene,
            "clock_offset_ms": self.args.clock_offset_ms,
            "fresh_deadline_ms": self.args.fresh_deadline_ms,
            "messages": self.count,
            "fresh_messages": fresh_count,
            "stale_messages": max(0, len(self.latencies_ms) - fresh_count),
            "fps": self.count / observed,
            "fresh_fps": fresh_count / observed,
            "mbps": self.bytes_received * 8.0 / observed / 1_000_000.0,
            "latency_ms_median": statistics.median(values) if values else None,
            "latency_ms_p95": values[p95_index] if values else None,
            "latency_ms_p99": values[p99_index] if values else None,
            "observed_sec": observed,
            "elapsed_sec": time.monotonic() - self.started,
        }


def write_json(path: str | None, payload: dict):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def write_samples(path: str | None, args, samples: list[dict]):
    if not path:
        return
    write_json(
        path,
        {
            "profile": args.profile,
            "target_fps": args.fps,
            "width": args.width,
            "height": args.height,
            "scene": args.scene,
            "topic": args.topic,
            "clock_offset_ms": args.clock_offset_ms,
            "fresh_deadline_ms": args.fresh_deadline_ms,
            "samples": samples,
        },
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["pub", "sub"])
    parser.add_argument("--profile", choices=["raw", "compressed"], required=True)
    parser.add_argument("--topic", default="/benchmark/camera")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=60.0)
    parser.add_argument("--duration", type=float, default=12.0)
    parser.add_argument("--qos-depth", type=int, default=4)
    parser.add_argument("--jpeg-quality", type=int, default=72)
    parser.add_argument("--clock-offset-ms", type=float, default=0.0)
    parser.add_argument("--fresh-deadline-ms", type=float, default=150.0)
    parser.add_argument("--scene", choices=["gradient", "textured", "noise"], default="textured")
    parser.add_argument("--json", default=None)
    parser.add_argument("--samples-json", default=None)
    parser.add_argument("--metrics-csv", default=None)
    return parser.parse_args()


def write_standard_metrics(path: str | None, role: str, payload: dict):
    target = Path(path) if path else default_metrics_path("source_metrics.csv")
    if target is None:
        return

    fields = (
        "role",
        "profile",
        "topic",
        "target_fps",
        "width",
        "height",
        "scene",
        "messages",
        "fps",
        "mbps",
        "payload_bytes",
        "latency_ms_median",
        "latency_ms_p95",
        "latency_ms_p99",
    )
    row = {
        "role": role,
        "profile": payload.get("profile"),
        "topic": payload.get("topic", ""),
        "target_fps": payload.get("target_fps"),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "scene": payload.get("scene"),
        "messages": payload.get("messages"),
        "fps": payload.get("fps"),
        "mbps": payload.get("mbps"),
        "payload_bytes": payload.get("payload_bytes", ""),
        "latency_ms_median": payload.get("latency_ms_median", ""),
        "latency_ms_p95": payload.get("latency_ms_p95", ""),
        "latency_ms_p99": payload.get("latency_ms_p99", ""),
    }
    with CsvMetricWriter(target, fieldnames=fields) as writer:
        writer.write(row)


def main():
    args = parse_args()
    rclpy.init()
    node = CameraPublisher(args) if args.role == "pub" else CameraSubscriber(args)
    deadline = time.monotonic() + args.duration
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
    finally:
        payload = node.summary()
        payload["topic"] = args.topic
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        write_json(args.json, payload)
        write_standard_metrics(args.metrics_csv, args.role, payload)
        if args.role == "sub":
            write_samples(args.samples_json, args, node.samples)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
