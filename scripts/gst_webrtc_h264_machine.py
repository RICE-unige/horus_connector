#!/usr/bin/env python3
"""Machine-side H.264 WebRTC receiver with cmd_vel DataChannel send."""

from __future__ import annotations

import argparse
from collections import deque
import json
import statistics
import time
from pathlib import Path

import gi

from gst_webrtc_common import (
    ClientSignaling,
    Gst,
    ServerSignaling,
    configure_webrtcbin,
    ensure_webrtc_runtime,
    load_env_file,
    make_session_description,
)

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402


class H264MachineReceiver:
    def __init__(self, args):
        self.args = args
        self.profile = load_env_file(args.profile)
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self.webrtc = None
        self.cmd_channel = None
        self.cmd_seq = 0
        self.cmd_rtts = []
        self.cmd_acks = 0
        self.video_frames = 0
        self.video_first_sec = None
        self.video_last_sec = None
        self.frame_clock = deque()
        self.frame_latency_samples = []
        self.signaling = self._make_signaling()

    def _make_signaling(self):
        if self.args.signaling_url:
            return ClientSignaling(self.args.signaling_url, "machine", self.args.room, self._handle_signaling_message)
        return ServerSignaling(self.args.host, self.args.port, self._handle_signaling_message)

    def _decoder(self):
        name = self.profile.get("GST_H264_DECODER_NAME", "") or "avdec_h264"
        props = self.profile.get("GST_H264_DECODER_PROPS", "")
        return name, props

    def _build_pipeline(self):
        pipeline = Gst.Pipeline.new("horus-h264-machine")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if self.webrtc is None:
            raise RuntimeError("failed to create webrtcbin")
        configure_webrtcbin(self.webrtc, self.args.ice_servers)
        pipeline.add(self.webrtc)
        return pipeline

    def start(self):
        Gst.init(None)
        ensure_webrtc_runtime()
        self.signaling.start()
        print("Machine media pipeline: webrtcbin receiver", flush=True)
        self.pipeline = self._build_pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("pad-added", self._on_incoming_stream)
        self.webrtc.connect("on-data-channel", self._on_data_channel)
        self.pipeline.set_state(Gst.State.PLAYING)
        if self.args.duration > 0:
            GLib.timeout_add_seconds(int(self.args.duration), self.stop)
        try:
            self.loop.run()
        finally:
            self.stop()

    def stop(self):
        if self.video_frames:
            observed_sec = max((self.video_last_sec or 0.0) - (self.video_first_sec or 0.0), 0.001)
            payload = {
                "video_frames": self.video_frames,
                "video_observed_sec": observed_sec,
                "video_decoded_fps": self.video_frames / observed_sec,
            }
            if self.frame_latency_samples:
                latencies = [sample["latency_ms"] for sample in self.frame_latency_samples]
                fresh_count = sum(1 for sample in self.frame_latency_samples if sample["latency_ms"] <= self.args.fresh_deadline_ms)
                payload.update(
                    {
                        "video_latency_samples": len(self.frame_latency_samples),
                        "video_latency_ms_median": statistics.median(latencies),
                        "video_latency_ms_p95": sorted(latencies)[
                            min(len(latencies) - 1, int(round((len(latencies) - 1) * 0.95)))
                        ],
                        "video_latency_ms_p99": sorted(latencies)[
                            min(len(latencies) - 1, int(round((len(latencies) - 1) * 0.99)))
                        ],
                        "fresh_deadline_ms": self.args.fresh_deadline_ms,
                        "fresh_latency_samples": fresh_count,
                        "fresh_sample_sla": fresh_count / len(self.frame_latency_samples),
                        "fresh_fps_estimate": (self.video_frames / observed_sec)
                        * (fresh_count / len(self.frame_latency_samples)),
                        "clock_offset_ms": self.args.clock_offset_ms,
                    }
                )
            print(
                json.dumps(payload, indent=2, sort_keys=True),
                flush=True,
            )
            if self.args.latency_json:
                Path(self.args.latency_json).parent.mkdir(parents=True, exist_ok=True)
                Path(self.args.latency_json).write_text(
                    json.dumps(
                        {
                            **payload,
                            "samples": self.frame_latency_samples,
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
        if self.cmd_rtts:
            print(
                json.dumps(
                    {
                        "cmd_vel_acks": self.cmd_acks,
                        "cmd_vel_rtt_ms_median": statistics.median(self.cmd_rtts),
                        "cmd_vel_rtt_ms_max": max(self.cmd_rtts),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                flush=True,
            )
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self.loop.is_running():
            self.loop.quit()
        return False

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self.signaling.send({"type": "ice", "sdpMLineIndex": int(mlineindex), "candidate": candidate})

    def _handle_signaling_message(self, message):
        GLib.idle_add(self._handle_signaling_message_in_loop, message)

    def _handle_signaling_message_in_loop(self, message):
        kind = message.get("type")
        if kind == "offer":
            offer = make_session_description("offer", message["sdp"])
            self.webrtc.emit("set-remote-description", offer, Gst.Promise.new())
            promise = Gst.Promise.new_with_change_func(self._on_answer_created, None)
            self.webrtc.emit("create-answer", None, promise)
        elif kind == "ice":
            self.webrtc.emit("add-ice-candidate", int(message["sdpMLineIndex"]), message["candidate"])
        return False

    def _on_answer_created(self, promise, _user_data):
        promise.wait()
        reply = promise.get_reply()
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None:
            detail = reply.to_string() if reply is not None else "no promise reply"
            print(f"failed to create WebRTC answer: {detail}", flush=True)
            self.stop()
            return
        self.webrtc.emit("set-local-description", answer, Gst.Promise.new())
        self.signaling.send({"type": "answer", "sdp": answer.sdp.as_text()})

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {error}; {debug}", flush=True)
            self.stop()
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"GStreamer warning from {message.src.get_name()}: {warning}; {debug}", flush=True)

    def _on_incoming_stream(self, _webrtc, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or not caps.to_string().startswith("application/x-rtp"):
            return
        name, props = self._decoder()
        print(f"Incoming RTP video; decoder={name}", flush=True)
        elements = [
            Gst.ElementFactory.make("queue"),
            Gst.ElementFactory.make("rtph264depay"),
            Gst.ElementFactory.make("h264parse"),
            Gst.ElementFactory.make(name),
            Gst.ElementFactory.make("capsfilter"),
            Gst.ElementFactory.make("videoconvert"),
            Gst.ElementFactory.make(self.args.video_sink),
        ]
        if any(element is None for element in elements):
            missing = [str(index) for index, element in enumerate(elements) if element is None]
            raise RuntimeError(f"failed to create decode elements: {missing}")
        queue, depay, parse, decoder, raw_caps, convert, sink = elements
        queue.set_property("max-size-buffers", 1)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        queue.set_property("leaky", 2)
        raw_caps.set_property("caps", Gst.Caps.from_string("video/x-raw"))
        for assignment in [item for item in props.split() if "=" in item]:
            key, value = assignment.split("=", 1)
            decoder.set_property(key, value)
        if self.args.video_sink == "fakesink":
            sink.set_property("sync", False)
            sink.set_property("signal-handoffs", True)
            sink.connect("handoff", self._on_video_frame)
        else:
            if sink.find_property("sync"):
                sink.set_property("sync", False)
        for element in elements:
            self.pipeline.add(element)
            element.sync_state_with_parent()
        for left, right in [
            (queue, depay),
            (depay, parse),
            (parse, decoder),
            (decoder, raw_caps),
            (raw_caps, convert),
            (convert, sink),
        ]:
            if not left.link(right):
                raise RuntimeError(f"failed to link {left.get_name()} -> {right.get_name()}")
        result = pad.link(queue.get_static_pad("sink"))
        if result != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link WebRTC pad: {result}")

    def _on_video_frame(self, _sink, _buffer, _pad):
        now = time.monotonic()
        if self.video_first_sec is None:
            self.video_first_sec = now
        self.video_last_sec = now
        self.video_frames += 1
        if self.frame_clock:
            sent = self.frame_clock.pop()
            self.frame_clock.clear()
            raw_latency_ms = (time.time_ns() - int(sent["sent_ns"])) / 1_000_000.0
            self.frame_latency_samples.append(
                {
                    "seq": sent.get("seq"),
                    "t_sec": now - (self.video_first_sec or now),
                    "latency_ms": raw_latency_ms - self.args.clock_offset_ms,
                    "raw_latency_ms": raw_latency_ms,
                }
            )

    def _on_data_channel(self, _webrtc, channel):
        print(f"DataChannel received: {channel.props.label}", flush=True)
        if channel.props.label == "frame-clock":
            channel.connect("on-message-string", self._on_frame_clock_message)
            return
        if channel.props.label != "cmd-vel":
            return
        self.cmd_channel = channel
        channel.connect("on-open", self._on_cmd_channel_open)
        channel.connect("on-message-string", self._on_cmd_message)

    def _on_frame_clock_message(self, _channel, text):
        try:
            message = json.loads(text)
        except Exception:
            return
        if message.get("type") != "frame_clock" or "sent_ns" not in message:
            return
        self.frame_clock.append(message)
        while len(self.frame_clock) > 600:
            self.frame_clock.popleft()

    def _on_cmd_channel_open(self, _channel):
        print("cmd-vel DataChannel open", flush=True)
        if self.args.cmd_rate > 0:
            interval_ms = max(1, int(1000.0 / self.args.cmd_rate))
            GLib.timeout_add(interval_ms, self._send_cmd_vel)

    def _send_cmd_vel(self):
        if self.cmd_channel is None:
            return True
        command = {
            "type": "cmd_vel",
            "seq": self.cmd_seq,
            "sent_ns": time.time_ns(),
            "linear_x": self.args.cmd_linear_x,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": self.args.cmd_angular_z,
        }
        self.cmd_channel.emit("send-string", json.dumps(command, separators=(",", ":")))
        self.cmd_seq += 1
        return True

    def _on_cmd_message(self, _channel, text):
        try:
            ack = json.loads(text)
        except Exception:
            return
        if ack.get("type") != "cmd_vel_ack":
            return
        sent_ns = int(ack.get("sent_ns") or 0)
        if sent_ns > 0:
            self.cmd_rtts.append((time.time_ns() - sent_ns) / 1_000_000.0)
        self.cmd_acks += 1


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signaling-url", default="")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--room", default="default")
    parser.add_argument("--profile", default=".webrtc_profile.env")
    parser.add_argument("--ice-servers", default="stun:stun.l.google.com:19302")
    parser.add_argument("--video-sink", default="fakesink")
    parser.add_argument("--cmd-rate", type=float, default=0.0)
    parser.add_argument("--cmd-linear-x", type=float, default=0.0)
    parser.add_argument("--cmd-angular-z", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--clock-offset-ms", type=float, default=0.0)
    parser.add_argument("--fresh-deadline-ms", type=float, default=150.0)
    parser.add_argument("--latency-json", default="")
    return parser.parse_args()


def main():
    H264MachineReceiver(parse_args()).start()


if __name__ == "__main__":
    main()
