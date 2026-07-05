#!/usr/bin/env python3
"""ROS 2 command-path RTT benchmark.

The machine role publishes timestamped velocity-command probes. The robot role
acks each probe immediately on a companion topic. RTT is measured on the machine
clock, so this benchmark does not require cross-host clock synchronization.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


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


def write_json(path: str | None, payload: dict):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class RobotAckNode(Node):
    def __init__(self, args):
        super().__init__("horus_cmd_vel_benchmark_robot")
        self.args = args
        self.started = time.monotonic()
        self.received = 0
        self.invalid = 0
        self.publisher = self.create_publisher(String, args.ack_topic, qos(args.qos_depth))
        self.create_subscription(String, args.cmd_topic, self.on_command, qos(args.qos_depth))

    def on_command(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            self.invalid += 1
            return
        self.received += 1
        ack = {
            "type": "cmd_vel_ack",
            "seq": payload.get("seq"),
            "send_wall_ns": payload.get("send_wall_ns"),
            "robot_receive_wall_ns": time.time_ns(),
            "robot_receive_monotonic_sec": time.monotonic() - self.started,
        }
        out = String()
        out.data = json.dumps(ack, separators=(",", ":"))
        self.publisher.publish(out)

    def summary(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 0.001)
        return {
            "role": "robot_ack",
            "cmd_topic": self.args.cmd_topic,
            "ack_topic": self.args.ack_topic,
            "received_commands": self.received,
            "invalid_commands": self.invalid,
            "commands_per_sec": self.received / elapsed,
            "elapsed_sec": elapsed,
            "qos": f"keep_last_{self.args.qos_depth}_best_effort",
        }


class MachineProbeNode(Node):
    def __init__(self, args):
        super().__init__("horus_cmd_vel_benchmark_machine")
        self.args = args
        self.started = time.monotonic()
        self.seq = 0
        self.sent = 0
        self.acks = 0
        self.invalid_acks = 0
        self.pending: dict[int, int] = {}
        self.rtts_ms: list[float] = []
        self.samples: list[dict] = []
        self.publisher = self.create_publisher(String, args.cmd_topic, qos(args.qos_depth))
        self.create_subscription(String, args.ack_topic, self.on_ack, qos(args.qos_depth))
        self.timer = None

    def start_publishing(self):
        self.started = time.monotonic()
        self.timer = self.create_timer(1.0 / self.args.rate_hz, self.publish_command)

    def publish_command(self):
        seq = self.seq
        self.seq += 1
        send_wall_ns = time.time_ns()
        payload = {
            "type": "cmd_vel",
            "seq": seq,
            "send_wall_ns": send_wall_ns,
            "linear_x": self.args.linear_x,
            "angular_z": self.args.angular_z,
        }
        msg = String()
        msg.data = json.dumps(payload, separators=(",", ":"))
        self.pending[seq] = send_wall_ns
        self.sent += 1
        self.publisher.publish(msg)

    def on_ack(self, msg: String):
        receive_wall_ns = time.time_ns()
        try:
            payload = json.loads(msg.data)
            seq = int(payload["seq"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self.invalid_acks += 1
            return
        send_wall_ns = self.pending.pop(seq, payload.get("send_wall_ns", receive_wall_ns))
        rtt_ms = (receive_wall_ns - int(send_wall_ns)) / 1_000_000.0
        if 0.0 <= rtt_ms < 10_000.0:
            self.acks += 1
            self.rtts_ms.append(rtt_ms)
            self.samples.append(
                {
                    "seq": seq,
                    "send_wall_ns": int(send_wall_ns),
                    "ack_receive_wall_ns": receive_wall_ns,
                    "rtt_ms": rtt_ms,
                    "receive_t_sec": time.monotonic() - self.started,
                }
            )
        else:
            self.invalid_acks += 1

    def summary(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 0.001)
        received = self.acks
        lost = max(0, self.sent - received)
        values = sorted(self.rtts_ms)
        return {
            "role": "machine_probe",
            "cmd_topic": self.args.cmd_topic,
            "ack_topic": self.args.ack_topic,
            "target_rate_hz": self.args.rate_hz,
            "sent_commands": self.sent,
            "acked_commands": received,
            "lost_commands": lost,
            "invalid_acks": self.invalid_acks,
            "delivery_ratio": received / self.sent if self.sent else 0.0,
            "ack_rate_hz": received / elapsed,
            "rtt_ms_median": statistics.median(values) if values else None,
            "rtt_ms_p50": percentile(values, 0.50),
            "rtt_ms_p95": percentile(values, 0.95),
            "rtt_ms_p99": percentile(values, 0.99),
            "rtt_ms_max": max(values) if values else None,
            "elapsed_sec": elapsed,
            "qos": f"keep_last_{self.args.qos_depth}_best_effort",
            "latency_method": "machine send wall-clock to machine ack receive wall-clock; no cross-host clock correction required",
        }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["robot", "machine"])
    parser.add_argument("--cmd-topic", default="/benchmark/cmd_vel")
    parser.add_argument("--ack-topic", default="/benchmark/cmd_vel_ack")
    parser.add_argument("--rate-hz", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--qos-depth", type=int, default=1)
    parser.add_argument("--linear-x", type=float, default=0.05)
    parser.add_argument("--angular-z", type=float, default=0.02)
    parser.add_argument("--warmup-sec", type=float, default=2.0)
    parser.add_argument("--json", default=None)
    parser.add_argument("--samples-json", default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    if not math.isfinite(args.rate_hz) or args.rate_hz <= 0:
        raise SystemExit("--rate-hz must be positive")
    rclpy.init()
    node = RobotAckNode(args) if args.role == "robot" else MachineProbeNode(args)
    if args.role == "machine" and args.warmup_sec > 0:
        warmup_deadline = time.monotonic() + args.warmup_sec
        while rclpy.ok() and time.monotonic() < warmup_deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
        node.start_publishing()
    elif args.role == "machine":
        node.start_publishing()
    deadline = time.monotonic() + args.duration
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
    finally:
        payload = node.summary()
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        write_json(args.json, payload)
        if args.role == "machine":
            write_json(
                args.samples_json,
                {
                    "cmd_topic": args.cmd_topic,
                    "ack_topic": args.ack_topic,
                    "target_rate_hz": args.rate_hz,
                    "qos": f"keep_last_{args.qos_depth}_best_effort",
                    "samples": node.samples,
                },
            )
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
