#!/usr/bin/env python3
"""ROS 2 camera transport benchmark for raw and JPEG-compressed frames.

The benchmark models camera transport as a freshest-frame workload: late frames
and dropped frames are both counted against the usable visual feedback budget.
"""

from __future__ import annotations

import argparse
import json
import math
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


FRAME_ID_PREFIX = "benchmark_camera"


def qos(depth: int) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def frame_id(seq: int, capture_wall_ns: int) -> str:
    return f"{FRAME_ID_PREFIX};seq={seq};capture_wall_ns={capture_wall_ns}"


def parse_frame_id(value: str) -> dict[str, int]:
    metadata: dict[str, int] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        try:
            metadata[key.strip()] = int(raw.strip())
        except ValueError:
            continue
    return metadata


def load_clock_offset(path: str | None) -> tuple[float, str]:
    if not path:
        return 0.0, "none"
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "offset_ms" not in payload:
        raise ValueError(f"{path} does not contain offset_ms")
    return float(payload["offset_ms"]), f"clock_offset_probe:{Path(path).name}"


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
        seq = self.count
        capture_wall_ns = time.time_ns()
        stamp = self.get_clock().now().to_msg()
        if self.args.profile == "raw":
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = frame_id(seq, capture_wall_ns)
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
            msg.header.frame_id = frame_id(seq, capture_wall_ns)
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
            "freshest_frame_policy": f"keep_last_{self.args.qos_depth}_best_effort",
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
        self.fresh_count = 0
        self.stale_count = 0
        self.invalid_latency_count = 0
        self.last_seq = None
        self.max_seq = None
        self.dropped_before_receive = 0
        self.out_of_order_count = 0
        msg_type = Image if args.profile == "raw" else CompressedImage
        self.create_subscription(msg_type, args.topic, self.on_message, qos(args.qos_depth))

    def on_message(self, msg):
        now_mono = time.monotonic()
        receive_wall_ns = time.time_ns()
        receive_ros_ns = self.get_clock().now().nanoseconds
        sent_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
        metadata = parse_frame_id(msg.header.frame_id)
        seq = metadata.get("seq", self.count)
        sequence_gap = 0
        if self.last_seq is not None:
            if seq > self.last_seq + 1:
                sequence_gap = seq - self.last_seq - 1
                self.dropped_before_receive += sequence_gap
            elif seq <= self.last_seq:
                self.out_of_order_count += 1
        self.last_seq = seq if self.last_seq is None else max(self.last_seq, seq)
        self.max_seq = seq if self.max_seq is None else max(self.max_seq, seq)

        raw_latency_ms = (receive_ros_ns - sent_ns) / 1_000_000.0
        latency_ms = raw_latency_ms - self.args.clock_offset_ms
        raw_wall_latency_ms = None
        wall_latency_ms = None
        capture_wall_ns = metadata.get("capture_wall_ns")
        if capture_wall_ns is not None:
            raw_wall_latency_ms = (receive_wall_ns - capture_wall_ns) / 1_000_000.0
            wall_latency_ms = raw_wall_latency_ms - self.args.clock_offset_ms
        if -1000.0 < latency_ms < 10_000.0:
            is_fresh = latency_ms <= self.args.fresh_deadline_ms
            self.latencies_ms.append(latency_ms)
            if is_fresh:
                self.fresh_count += 1
            else:
                self.stale_count += 1
            self.samples.append(
                {
                    "seq": seq,
                    "receive_t_sec": now_mono - self.started,
                    "latency_ms": latency_ms,
                    "raw_latency_ms": raw_latency_ms,
                    "raw_wall_latency_ms": raw_wall_latency_ms,
                    "wall_latency_ms": wall_latency_ms,
                    "capture_ros_ns": sent_ns,
                    "capture_wall_ns": capture_wall_ns,
                    "receive_ros_ns": receive_ros_ns,
                    "receive_wall_ns": receive_wall_ns,
                    "sequence_gap": sequence_gap,
                    "fresh": is_fresh,
                    "stale": not is_fresh,
                }
            )
        else:
            self.invalid_latency_count += 1
        self.first = now_mono if self.first is None else self.first
        self.last = now_mono
        self.count += 1
        self.bytes_received += len(msg.data)

    def estimated_published_frames(self):
        if self.max_seq is not None:
            return max(int(self.max_seq) + 1, self.count + self.dropped_before_receive)
        elapsed = max(time.monotonic() - self.started, 0.001)
        return max(self.count, int(round(elapsed * self.args.fps)))

    def summary(self):
        observed = max((self.last or self.started) - (self.first or self.started), 0.001)
        values = sorted(self.latencies_ms)
        estimated_published = self.estimated_published_frames()
        dropped_or_skipped = max(0, estimated_published - self.count)
        fresh_sla = self.fresh_count / estimated_published if estimated_published else 0.0
        return {
            "role": "sub",
            "profile": self.args.profile,
            "target_fps": self.args.fps,
            "width": self.args.width,
            "height": self.args.height,
            "scene": self.args.scene,
            "clock_offset_ms": self.args.clock_offset_ms,
            "clock_offset_source": self.args.clock_offset_source,
            "fresh_deadline_ms": self.args.fresh_deadline_ms,
            "messages": self.count,
            "estimated_published_frames": estimated_published,
            "received_frames": self.count,
            "dropped_or_skipped_frames": dropped_or_skipped,
            "sequence_gap_frames": self.dropped_before_receive,
            "out_of_order_frames": self.out_of_order_count,
            "invalid_latency_samples": self.invalid_latency_count,
            "fresh_messages": self.fresh_count,
            "stale_messages": self.stale_count,
            "fresh_frame_sla": fresh_sla,
            "fresh_frame_sla_percent": fresh_sla * 100.0,
            "delivery_ratio": self.count / estimated_published if estimated_published else 0.0,
            "fps": self.count / observed,
            "fresh_fps": self.fresh_count / observed,
            "usable_fps": self.fresh_count / observed,
            "mbps": self.bytes_received * 8.0 / observed / 1_000_000.0,
            "latency_ms_median": statistics.median(values) if values else None,
            "latency_ms_p50": percentile(values, 0.50),
            "latency_ms_p95": percentile(values, 0.95),
            "latency_ms_p99": percentile(values, 0.99),
            "observed_sec": observed,
            "elapsed_sec": time.monotonic() - self.started,
            "freshest_frame_policy": f"keep_last_{self.args.qos_depth}_best_effort",
            "latency_method": "ROS header stamp corrected by measured clock offset; no percentile-baseline subtraction",
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
            "clock_offset_source": args.clock_offset_source,
            "fresh_deadline_ms": args.fresh_deadline_ms,
            "freshest_frame_policy": f"keep_last_{args.qos_depth}_best_effort",
            "latency_method": "ROS header stamp corrected by measured clock offset; no percentile-baseline subtraction",
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
    parser.add_argument("--qos-depth", type=int, default=1)
    parser.add_argument("--jpeg-quality", type=int, default=72)
    parser.add_argument("--clock-offset-ms", type=float, default=None)
    parser.add_argument("--clock-offset-json", default=None)
    parser.add_argument("--fresh-deadline-ms", type=float, default=150.0)
    parser.add_argument("--scene", choices=["gradient", "textured", "noise"], default="textured")
    parser.add_argument("--json", default=None)
    parser.add_argument("--samples-json", default=None)
    parser.add_argument("--metrics-csv", default=None)
    args = parser.parse_args()
    if args.clock_offset_ms is None:
        args.clock_offset_ms, args.clock_offset_source = load_clock_offset(args.clock_offset_json)
    else:
        args.clock_offset_source = "manual"
    return args


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
        "estimated_published_frames",
        "delivery_ratio",
        "fresh_frame_sla_percent",
        "usable_fps",
        "dropped_or_skipped_frames",
        "stale_messages",
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
        "estimated_published_frames": payload.get("estimated_published_frames", ""),
        "delivery_ratio": payload.get("delivery_ratio", ""),
        "fresh_frame_sla_percent": payload.get("fresh_frame_sla_percent", ""),
        "usable_fps": payload.get("usable_fps", ""),
        "dropped_or_skipped_frames": payload.get("dropped_or_skipped_frames", ""),
        "stale_messages": payload.get("stale_messages", ""),
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
