#!/usr/bin/env python3
"""Machine-side H.264 WebRTC receiver with cmd_vel DataChannel send."""

from __future__ import annotations

import argparse
import json
import statistics
import time

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
        answer = promise.get_reply().get_value("answer")
        self.webrtc.emit("set-local-description", answer, Gst.Promise.new())
        self.signaling.send({"type": "answer", "sdp": answer.sdp.as_text()})

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
            Gst.ElementFactory.make("videoconvert"),
            Gst.ElementFactory.make(self.args.video_sink),
        ]
        if any(element is None for element in elements):
            missing = [str(index) for index, element in enumerate(elements) if element is None]
            raise RuntimeError(f"failed to create decode elements: {missing}")
        queue, depay, parse, decoder, convert, sink = elements
        queue.set_property("max-size-buffers", 1)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        queue.set_property("leaky", 2)
        for assignment in [item for item in props.split() if "=" in item]:
            key, value = assignment.split("=", 1)
            decoder.set_property(key, value)
        if self.args.video_sink == "fakesink":
            sink.set_property("sync", False)
        else:
            if sink.find_property("sync"):
                sink.set_property("sync", False)
        for element in elements:
            self.pipeline.add(element)
            element.sync_state_with_parent()
        if not Gst.Element.link_many(queue, depay, parse, decoder, convert, sink):
            raise RuntimeError("failed to link H.264 receive pipeline")
        result = pad.link(queue.get_static_pad("sink"))
        if result != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link WebRTC pad: {result}")

    def _on_data_channel(self, _webrtc, channel):
        print(f"DataChannel received: {channel.props.label}", flush=True)
        if channel.props.label != "cmd-vel":
            return
        self.cmd_channel = channel
        channel.connect("on-open", self._on_cmd_channel_open)
        channel.connect("on-message-string", self._on_cmd_message)

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
    return parser.parse_args()


def main():
    H264MachineReceiver(parse_args()).start()


if __name__ == "__main__":
    main()
