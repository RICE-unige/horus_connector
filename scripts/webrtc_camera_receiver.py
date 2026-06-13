#!/usr/bin/env python3
"""Receive WebRTC camera frames and measure delivery latency."""

import argparse
import asyncio
import csv
import json
import statistics
import time
from pathlib import Path

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from experiment_metrics import CsvMetricWriter, default_metrics_path
from webrtc_common import candidate_summary, unpack_frame, wait_for_ice_gathering_complete


def percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


class Recorder:
    def __init__(self, duration):
        self.duration = duration
        self.started_ns = None
        self.rows = []
        self.control_rows = []
        self.stop = asyncio.Event()

    def record(self, header, payload):
        receive_ns = time.time_ns()
        if self.started_ns is None:
            self.started_ns = receive_ns
        sent_ns = int(header["stamp_ns"])
        latency_ms = (receive_ns - sent_ns) / 1_000_000.0
        row = {
            "camera": header["camera"],
            "seq": int(header["seq"]),
            "receive_ns": receive_ns,
            "source_stamp_ns": sent_ns,
            "latency_ms": latency_ms,
            "bytes": len(payload),
            "width": int(header.get("width", 0)),
            "height": int(header.get("height", 0)),
            "quality": int(header.get("quality", 0)),
        }
        self.rows.append(row)
        if (receive_ns - self.started_ns) / 1e9 >= self.duration:
            self.stop.set()

    def summary(self):
        by_camera = {}
        for row in self.rows:
            by_camera.setdefault(row["camera"], []).append(row)
        cameras = {}
        total_bytes = sum(row["bytes"] for row in self.rows)
        if self.rows:
            start_ns = min(row["receive_ns"] for row in self.rows)
            end_ns = max(row["receive_ns"] for row in self.rows)
            observed_sec = max((end_ns - start_ns) / 1e9, 0.001)
        else:
            observed_sec = 0.0
        for camera, rows in sorted(by_camera.items()):
            values = [row["latency_ms"] for row in rows]
            byte_count = sum(row["bytes"] for row in rows)
            span = max((max(row["receive_ns"] for row in rows) - min(row["receive_ns"] for row in rows)) / 1e9, 0.001)
            cameras[camera] = {
                "count": len(rows),
                "observed_hz": len(rows) / span,
                "mb_received": byte_count / 1_000_000.0,
                "mbps_received": byte_count * 8.0 / 1_000_000.0 / max(observed_sec, 0.001),
                "latency_ms_min": min(values),
                "latency_ms_median": statistics.median(values),
                "latency_ms_mean": statistics.fmean(values),
                "latency_ms_p95": percentile(values, 95.0),
                "latency_ms_p99": percentile(values, 99.0),
                "latency_ms_max": max(values),
            }
        values = [row["latency_ms"] for row in self.rows]
        control_values = [row["rtt_ms"] for row in self.control_rows]
        return {
            "duration_sec": self.duration,
            "observed_sec": observed_sec,
            "count": len(self.rows),
            "mb_received": total_bytes / 1_000_000.0,
            "mbps_received": total_bytes * 8.0 / 1_000_000.0 / max(observed_sec, 0.001),
            "latency_ms_median": statistics.median(values) if values else None,
            "latency_ms_p95": percentile(values, 95.0),
            "latency_ms_p99": percentile(values, 99.0),
            "cameras": cameras,
            "control": {
                "count": len(self.control_rows),
                "rtt_ms_median": statistics.median(control_values) if control_values else None,
                "rtt_ms_p95": percentile(control_values, 95.0),
                "rtt_ms_p99": percentile(control_values, 99.0),
                "acked_hz": (len(self.control_rows) / max(self.duration, 0.001)),
                "published_to_ros_count": sum(1 for row in self.control_rows if row.get("published_to_ros")),
            },
        }

    def write_csv(self, path):
        if not path:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "camera",
                    "seq",
                    "receive_ns",
                    "source_stamp_ns",
                    "latency_ms",
                    "bytes",
                    "width",
                    "height",
                    "quality",
                ],
            )
            writer.writeheader()
            writer.writerows(self.rows)

    def write_json(self, path):
        if not path:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.summary(), indent=2, sort_keys=True), encoding="utf-8")

    def write_standard_metrics(self, path):
        target = Path(path) if path else default_metrics_path("webrtc_metrics.csv")
        if target is None:
            return
        summary = self.summary()
        fields = (
            "role",
            "stream",
            "count",
            "observed_sec",
            "mbps_received",
            "latency_ms_median",
            "latency_ms_p95",
            "latency_ms_p99",
            "control_count",
            "control_rtt_ms_p95",
        )
        with CsvMetricWriter(target, fieldnames=fields) as writer:
            writer.write(
                {
                    "role": "receiver",
                    "stream": "webrtc_camera",
                    "count": summary.get("count"),
                    "observed_sec": summary.get("observed_sec"),
                    "mbps_received": summary.get("mbps_received"),
                    "latency_ms_median": summary.get("latency_ms_median"),
                    "latency_ms_p95": summary.get("latency_ms_p95"),
                    "latency_ms_p99": summary.get("latency_ms_p99"),
                    "control_count": summary.get("control", {}).get("count"),
                    "control_rtt_ms_p95": summary.get("control", {}).get("rtt_ms_p95"),
                }
            )

    def record_control_ack(self, ack):
        receive_ns = time.time_ns()
        sent_ns = int(ack.get("sent_ns", 0))
        if sent_ns <= 0:
            return
        self.control_rows.append(
            {
                "seq": ack.get("seq"),
                "receive_ns": receive_ns,
                "sent_ns": sent_ns,
                "robot_receive_ns": ack.get("robot_receive_ns"),
                "robot_ack_ns": ack.get("robot_ack_ns"),
                "rtt_ms": (receive_ns - sent_ns) / 1_000_000.0,
                "published_to_ros": bool(ack.get("published_to_ros")),
            }
        )


async def send_cmd_vel_loop(channel, args):
    while channel.readyState != "open":
        await asyncio.sleep(0.01)
    if args.cmd_rate <= 0.0:
        return
    period = 1.0 / args.cmd_rate
    deadline = time.monotonic() + args.duration
    seq = 0
    while time.monotonic() < deadline:
        command = {
            "type": "cmd_vel",
            "seq": seq,
            "sent_ns": time.time_ns(),
            "linear_x": args.cmd_linear_x,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": args.cmd_angular_z,
        }
        if channel.bufferedAmount < args.cmd_max_buffer_bytes:
            channel.send(json.dumps(command, separators=(",", ":")))
        seq += 1
        await asyncio.sleep(period)


async def run(args):
    recorder = Recorder(args.duration)
    pcs = set()
    ice_servers = [RTCIceServer(urls=url) for url in args.ice_server]

    async def handle(websocket):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        pcs.add(pc)

        @pc.on("iceconnectionstatechange")
        def on_ice_connection_state_change():
            print(f"ICE state: {pc.iceConnectionState}", flush=True)
            if pc.iceConnectionState in {"failed", "closed", "disconnected"}:
                recorder.stop.set()

        @pc.on("datachannel")
        def on_datachannel(channel):
            print(f"DataChannel received: {channel.label}", flush=True)

            @channel.on("message")
            def on_message(message):
                if channel.label == "camera-jpeg":
                    header, payload = unpack_frame(message)
                    if header is not None:
                        recorder.record(header, payload)
                elif channel.label == "cmd-vel":
                    try:
                        ack = json.loads(message if isinstance(message, str) else message.decode("utf-8"))
                    except Exception:
                        return
                    if ack.get("type") == "cmd_vel_ack":
                        recorder.record_control_ack(ack)

            if channel.label == "cmd-vel":
                asyncio.create_task(send_cmd_vel_loop(channel, args))

        offer = json.loads(await websocket.recv())
        print(f"Remote ICE candidates: {candidate_summary(offer['sdp'])}", flush=True)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type=offer["type"]))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await wait_for_ice_gathering_complete(pc, timeout=args.ice_timeout)
        print(f"Local ICE candidates: {candidate_summary(pc.localDescription.sdp)}", flush=True)
        await websocket.send(json.dumps({"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}))
        await recorder.stop.wait()

    async with websockets.serve(handle, args.host, args.port, max_size=32 * 1024 * 1024):
        print(f"WebRTC camera receiver listening on ws://{args.host}:{args.port}", flush=True)
        try:
            await asyncio.wait_for(recorder.stop.wait(), timeout=args.wait_timeout)
        except asyncio.TimeoutError:
            print("Timed out waiting for WebRTC camera frames", flush=True)
        finally:
            for pc in list(pcs):
                await pc.close()
            recorder.write_csv(args.csv)
            recorder.write_json(args.json)
            recorder.write_standard_metrics(args.metrics_csv)
            print(json.dumps(recorder.summary(), indent=2, sort_keys=True), flush=True)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--wait-timeout", type=float, default=90.0)
    parser.add_argument("--ice-timeout", type=float, default=30.0)
    parser.add_argument("--ice-server", action="append", default=["stun:stun.l.google.com:19302"])
    parser.add_argument("--cmd-rate", type=float, default=0.0, help="Send test cmd_vel messages back to the robot over WebRTC at this rate.")
    parser.add_argument("--cmd-linear-x", type=float, default=0.15)
    parser.add_argument("--cmd-angular-z", type=float, default=0.2)
    parser.add_argument("--cmd-max-buffer-bytes", type=int, default=64_000)
    parser.add_argument("--json", default="webrtc_camera_latency.json")
    parser.add_argument("--csv", default="webrtc_camera_latency.csv")
    parser.add_argument("--metrics-csv", default=None)
    return parser.parse_args()


def main():
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
