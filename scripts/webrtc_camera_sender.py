#!/usr/bin/env python3
"""Send camera frames over a low-latency WebRTC DataChannel."""

import argparse
import asyncio
import io
import json
import statistics
import time
from pathlib import Path

import numpy as np
import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from PIL import Image

from webrtc_common import camera_specs, candidate_summary, pack_frame, wait_for_ice_gathering_complete


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def make_frame(rng, width, height, phase):
    x = np.linspace(0, 255, width, dtype=np.uint16)[None, :]
    y = np.linspace(0, 255, height, dtype=np.uint16)[:, None]
    red = np.broadcast_to((x + phase) % 256, (height, width)).astype(np.uint8)
    green = np.broadcast_to((y + 2 * phase) % 256, (height, width)).astype(np.uint8)
    blue = ((red.astype(np.uint16) // 2 + green.astype(np.uint16) // 2 + phase) % 256).astype(np.uint8)
    frame = np.dstack((red, green, blue))
    block = 32
    noise = rng.integers(0, 64, size=(height // block + 1, width // block + 1, 3), dtype=np.uint8)
    noise = np.repeat(np.repeat(noise, block, axis=0), block, axis=1)[:height, :width, :]
    mixed = (frame.astype(np.uint16) * 82 + noise.astype(np.uint16) * 18) // 100
    return mixed.astype(np.uint8)


def encode_jpeg(frame, quality):
    buffer = io.BytesIO()
    Image.fromarray(frame, mode="RGB").save(buffer, format="JPEG", quality=quality, optimize=False)
    return buffer.getvalue()


class SenderStats:
    def __init__(self):
        self.started = time.time()
        self.generated = {}
        self.sent = {}
        self.dropped = {}
        self.bytes = {}
        self.encode_ms = {}

    def record_generated(self, name):
        self.generated[name] = self.generated.get(name, 0) + 1

    def record_sent(self, name, byte_count, encode_ms):
        self.sent[name] = self.sent.get(name, 0) + 1
        self.bytes[name] = self.bytes.get(name, 0) + int(byte_count)
        self.encode_ms.setdefault(name, []).append(encode_ms)

    def record_dropped(self, name):
        self.dropped[name] = self.dropped.get(name, 0) + 1

    def summary(self):
        elapsed = max(time.time() - self.started, 0.001)
        cameras = {}
        for name in sorted(set(self.generated) | set(self.sent) | set(self.dropped)):
            enc = self.encode_ms.get(name, [])
            cameras[name] = {
                "generated": self.generated.get(name, 0),
                "sent": self.sent.get(name, 0),
                "dropped": self.dropped.get(name, 0),
                "mb_sent": self.bytes.get(name, 0) / 1_000_000.0,
                "mbps_sent": self.bytes.get(name, 0) * 8.0 / 1_000_000.0 / elapsed,
                "encode_ms_median": statistics.median(enc) if enc else None,
                "encode_ms_p95": percentile(enc, 95.0),
            }
        return {"elapsed_sec": elapsed, "cameras": cameras}


def write_summary(path, payload):
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class RosCmdPublisher:
    def __init__(self, topic):
        self.topic = topic
        self.node = None
        self.publisher = None
        self.rclpy = None
        if not topic:
            return
        try:
            import rclpy
            from geometry_msgs.msg import Twist
        except Exception as exc:
            print(f"ROS cmd_vel publishing disabled: {exc}", flush=True)
            return
        try:
            rclpy.init(args=None)
            self.node = rclpy.create_node("webrtc_cmd_vel_bridge")
            self.publisher = self.node.create_publisher(Twist, topic, 10)
            self.twist_type = Twist
            self.rclpy = rclpy
            print(f"Publishing WebRTC control messages to ROS topic {topic}", flush=True)
        except Exception as exc:
            print(f"ROS cmd_vel publishing disabled: {exc}", flush=True)
            self.close()

    def publish(self, command):
        if self.publisher is None:
            return False
        msg = self.twist_type()
        msg.linear.x = float(command.get("linear_x", 0.0))
        msg.linear.y = float(command.get("linear_y", 0.0))
        msg.linear.z = float(command.get("linear_z", 0.0))
        msg.angular.x = float(command.get("angular_x", 0.0))
        msg.angular.y = float(command.get("angular_y", 0.0))
        msg.angular.z = float(command.get("angular_z", 0.0))
        self.publisher.publish(msg)
        return True

    def close(self):
        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        if self.rclpy is not None:
            try:
                self.rclpy.shutdown()
            except Exception:
                pass
            self.rclpy = None


def attach_control_channel(channel, ros_publisher):
    @channel.on("open")
    def on_open():
        print("Control DataChannel open; waiting for cmd_vel messages", flush=True)

    @channel.on("message")
    def on_message(message):
        receive_ns = time.time_ns()
        try:
            command = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
        except Exception as exc:
            print(f"Ignoring invalid control message: {exc}", flush=True)
            return
        if command.get("type") != "cmd_vel":
            return
        published = ros_publisher.publish(command)
        ack = {
            "type": "cmd_vel_ack",
            "seq": command.get("seq"),
            "sent_ns": command.get("sent_ns"),
            "robot_receive_ns": receive_ns,
            "robot_ack_ns": time.time_ns(),
            "published_to_ros": published,
        }
        channel.send(json.dumps(ack, separators=(",", ":")))


async def run_camera(channel, spec, duration, args, stats):
    rng = np.random.default_rng(args.seed + sum(ord(ch) for ch in spec.name))
    if spec.fps <= 0:
        return
    period = 1.0 / spec.fps
    next_send = time.monotonic()
    deadline = time.monotonic() + duration
    seq = 0
    while time.monotonic() < deadline:
        sleep_for = next_send - time.monotonic()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        next_send += period
        stats.record_generated(spec.name)
        if channel.bufferedAmount > args.max_buffer_bytes:
            stats.record_dropped(spec.name)
            continue
        phase = (seq * 7) % 255
        encode_start = time.perf_counter()
        frame = make_frame(rng, spec.width, spec.height, phase)
        payload = encode_jpeg(frame, spec.jpeg_quality)
        encode_ms = (time.perf_counter() - encode_start) * 1000.0
        sent_ns = time.time_ns()
        header = {
            "camera": spec.name,
            "seq": seq,
            "stamp_ns": sent_ns,
            "width": spec.width,
            "height": spec.height,
            "encoding": "jpeg",
            "quality": spec.jpeg_quality,
        }
        if channel.bufferedAmount > args.max_buffer_bytes:
            stats.record_dropped(spec.name)
        else:
            channel.send(pack_frame(header, payload))
            stats.record_sent(spec.name, len(payload), encode_ms)
        seq += 1


async def run(args):
    ice_servers = [RTCIceServer(urls=url) for url in args.ice_server]
    pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
    channel = pc.createDataChannel("camera-jpeg", ordered=False, maxRetransmits=0)
    control_channel = pc.createDataChannel("cmd-vel", ordered=False, maxRetransmits=0)
    ros_publisher = RosCmdPublisher(args.ros_cmd_topic)
    attach_control_channel(control_channel, ros_publisher)
    opened = asyncio.Event()
    closed = asyncio.Event()

    @channel.on("open")
    def on_open():
        opened.set()

    @channel.on("close")
    def on_close():
        closed.set()

    @pc.on("iceconnectionstatechange")
    def on_ice_connection_state_change():
        print(f"ICE state: {pc.iceConnectionState}", flush=True)
        if pc.iceConnectionState in {"failed", "closed", "disconnected"}:
            closed.set()

    async with websockets.connect(args.signaling_url, max_size=32 * 1024 * 1024) as websocket:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await wait_for_ice_gathering_complete(pc, timeout=args.ice_timeout)
        print(f"Local ICE candidates: {candidate_summary(pc.localDescription.sdp)}", flush=True)
        await websocket.send(json.dumps({"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}))
        answer = json.loads(await websocket.recv())
        print(f"Remote ICE candidates: {candidate_summary(answer['sdp'])}", flush=True)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

        await asyncio.wait_for(opened.wait(), timeout=args.ice_timeout)
        print("DataChannel open; streaming cameras", flush=True)
        stats = SenderStats()
        tasks = [
            asyncio.create_task(run_camera(channel, spec, args.duration, args, stats))
            for spec in camera_specs(args.camera_fps_scale, args.quality_scale)
        ]
        await asyncio.gather(*tasks)
        await asyncio.sleep(args.drain_sec)
        summary = stats.summary()
        print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
        write_summary(args.json, summary)
        channel.close()
        await asyncio.sleep(0.5)
    ros_publisher.close()
    await pc.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signaling-url", required=True, help="Receiver WebSocket URL, e.g. ws://cloud.example.com:8765")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--camera-fps-scale", type=float, default=1.0)
    parser.add_argument("--quality-scale", type=float, default=1.0)
    parser.add_argument("--max-buffer-bytes", type=int, default=2_000_000)
    parser.add_argument("--drain-sec", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--ice-timeout", type=float, default=30.0)
    parser.add_argument("--ice-server", action="append", default=["stun:stun.l.google.com:19302"])
    parser.add_argument("--ros-cmd-topic", default="", help="If set, publish incoming WebRTC cmd_vel commands to this ROS Twist topic.")
    parser.add_argument("--json", default=None)
    return parser.parse_args()


def main():
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
