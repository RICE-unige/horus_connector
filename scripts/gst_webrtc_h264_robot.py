#!/usr/bin/env python3
"""Robot-side H.264 WebRTC sender with cmd_vel DataChannel receive."""

import argparse
import json
import os
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
from ros_image_io import RosImageAppSrc, ensure_rclpy

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402


class RosCmdPublisher:
    def __init__(self, topic: str):
        self.topic = topic
        self.node = None
        self.publisher = None
        self.rclpy = None
        if not topic:
            return
        try:
            from geometry_msgs.msg import Twist
        except Exception as exc:
            print(f"ROS cmd_vel publishing disabled: {exc}", flush=True)
            return
        try:
            rclpy = ensure_rclpy()
            self.node = rclpy.create_node("horus_webrtc_cmd_vel")
            self.publisher = self.node.create_publisher(Twist, topic, 10)
            self.twist_type = Twist
            self.rclpy = rclpy
            print(f"Publishing WebRTC control messages to {topic}", flush=True)
        except Exception as exc:
            print(f"ROS cmd_vel publishing disabled: {exc}", flush=True)
            self.close()

    def publish(self, command: dict) -> bool:
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
            self.rclpy = None


class H264RobotSender:
    def __init__(self, args):
        self.args = args
        if self.args.max_bitrate_kbit < self.args.min_bitrate_kbit:
            self.args.max_bitrate_kbit = self.args.min_bitrate_kbit
        self.profile = load_env_file(args.profile)
        self.loop = GLib.MainLoop()
        self.webrtc = None
        self.pipeline = None
        self.encoder = None
        self.control = RosCmdPublisher(args.ros_cmd_topic)
        self.ros_image_source = None
        self.frame_clock_channel = None
        self.frame_clock_open = False
        self.frame_clock_seq = 0
        self.frame_clock_last_sec = 0.0
        self.data_channels_supported = True
        self.last_control_message_time = 0.0
        self.last_control_publish_time = 0.0
        self.watchdog_tripped = False
        self.watchdog_source_id = None
        self.adaptive_source_id = None
        self.pipeline_warning_count = 0
        self.pipeline_error_count = 0
        self.last_adaptive_warning_count = 0
        self.last_adaptive_error_count = 0
        self.current_bitrate_kbit = max(1, args.video_bitrate_kbit)
        self.recovering = False
        self.negotiation_in_progress = False
        self.offer_pending = False
        self.last_remote_answer_sdp = ""
        self.last_peer_ready_recovery = 0.0
        self.signaling = self._make_signaling()

    def _make_signaling(self):
        if self.args.signaling_url:
            return ClientSignaling(
                self.args.signaling_url,
                "robot",
                self.args.room,
                self._handle_signaling_message,
                self._on_signaling_connected,
            )
        return ServerSignaling(self.args.host, self.args.port, self._handle_signaling_message, self._on_signaling_connected)

    def _video_source(self) -> str:
        if self.args.video_source == "ros2":
            return (
                "appsrc name=ros_image_src is-live=true format=time do-timestamp=true block=false "
                f"caps=video/x-raw,format=RGB,width={self.args.width},height={self.args.height},framerate={self.args.fps}/1"
            )
        if self.args.source_pipeline:
            return self.args.source_pipeline
        return (
            f"videotestsrc is-live=true pattern=ball ! "
            f"video/x-raw,width={self.args.width},height={self.args.height},framerate={self.args.fps}/1"
        )

    def _encoder(self) -> str:
        name = self.profile.get("GST_H264_ENCODER_NAME", "")
        props = self.profile.get("GST_H264_ENCODER_PROPS", "")
        if not name:
            raise RuntimeError("No H.264 encoder selected. Run ./horus bootstrap robot.")
        return f"{name} name=video_encoder {props}".strip()

    def _encoder_preprocess(self) -> str:
        return self.profile.get("GST_H264_ENCODER_PREPROCESS", "") or "videoconvert"

    def _source_transform(self) -> str:
        if self.args.video_source != "ros2":
            return ""
        return (
            "videoconvert ! videoscale ! "
            f"video/x-raw,width={self.args.width},height={self.args.height} ! "
        )

    def _media_bin_description(self) -> str:
        return (
            f"{self._video_source()} ! "
            "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
            f"{self._source_transform()}"
            f"{self._encoder_preprocess()} ! "
            f"{'identity name=frameclock signal-handoffs=true silent=true ! ' if self.args.latency_probe else ''}"
            f"{self._encoder()} ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "rtph264pay pt=96 config-interval=1 mtu=1200 aggregate-mode=zero-latency"
        )

    def _build_pipeline(self):
        pipeline = Gst.Pipeline.new("horus-h264-robot")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if self.webrtc is None:
            raise RuntimeError("failed to create webrtcbin")
        configure_webrtcbin(self.webrtc, self.args.ice_servers, self.args.ice_transport_policy)
        media = Gst.parse_bin_from_description(self._media_bin_description(), True)
        if self.args.video_source == "ros2":
            appsrc = media.get_by_name("ros_image_src")
            if appsrc is None:
                raise RuntimeError("failed to create ROS image appsrc")
            self.ros_image_source = RosImageAppSrc(
                appsrc,
                self.args.ros_image_topic,
                self.args.fps,
                self.args.ros_image_qos,
            )
        self.encoder = media.get_by_name("video_encoder")
        if self.encoder is not None:
            self._set_encoder_bitrate(self.current_bitrate_kbit, announce=False)
        if self.args.latency_probe:
            frameclock = media.get_by_name("frameclock")
            if frameclock is None:
                raise RuntimeError("failed to create WebRTC frame latency probe")
            frameclock.connect("handoff", self._on_frame_clock_handoff)
        pipeline.add(media)
        pipeline.add(self.webrtc)
        srcpad = media.get_static_pad("src")
        request_pad_simple = getattr(self.webrtc, "request_pad_simple", None)
        sinkpad = request_pad_simple("sink_%u") if request_pad_simple is not None else None
        if sinkpad is None:
            sinkpad = self.webrtc.get_request_pad("sink_%u")
        if sinkpad is None:
            raise RuntimeError("failed to request webrtcbin sink pad; install gstreamer1.0-nice and rerun bootstrap")
        result = srcpad.link(sinkpad)
        if result != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link media to webrtcbin: {result}")
        return pipeline

    def start(self):
        Gst.init(None)
        ensure_webrtc_runtime()
        desc = self._media_bin_description()
        print(f"Robot media pipeline: {desc}", flush=True)
        self._start_pipeline()
        self._ensure_watchdog()
        self._ensure_adaptive_bitrate()
        self.signaling.start()
        if self.args.duration > 0:
            GLib.timeout_add_seconds(int(self.args.duration), self.stop)
        try:
            self.loop.run()
        finally:
            self.stop()

    def _start_pipeline(self):
        self.data_channels_supported = True
        self.pipeline = self._build_pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        try:
            self.webrtc.connect("on-data-channel", self._on_data_channel)
        except TypeError:
            self.data_channels_supported = False
            print("WebRTC DataChannel unavailable in this GStreamer runtime; cmd_vel over WebRTC disabled", flush=True)
        self.pipeline.set_state(Gst.State.PLAYING)
        if self.ros_image_source is not None:
            self.ros_image_source.start()
        self._create_local_data_channels()

    def _create_local_data_channels(self):
        if self.args.latency_probe and self.data_channels_supported:
            self.frame_clock_channel = self.webrtc.emit("create-data-channel", "frame-clock", None)
            if self.frame_clock_channel is not None:
                self.frame_clock_channel.connect("on-open", self._on_frame_clock_open)
            else:
                print("frame latency DataChannel unavailable; continuing without latency samples", flush=True)
        if self.args.ros_cmd_topic and self.data_channels_supported:
            control_channel = self.webrtc.emit("create-data-channel", "cmd-vel", None)
            if control_channel is not None:
                self._attach_control_channel(control_channel)
            else:
                print("cmd-vel DataChannel unavailable; continuing with video only", flush=True)

    def _ensure_watchdog(self):
        if not self.args.ros_cmd_topic or self.watchdog_source_id is not None:
            return
        self.watchdog_source_id = GLib.timeout_add(100, self._watchdog_check)

    def stop(self):
        if self.ros_image_source is not None:
            self.ros_image_source.close()
            self.ros_image_source = None
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.encoder = None
        self.control.close()
        if self.loop.is_running():
            self.loop.quit()
        return False

    def _on_negotiation_needed(self, _element):
        if self.webrtc is None:
            self.offer_pending = True
            return
        if not self.signaling.connected.is_set():
            self.offer_pending = True
            return
        if self.negotiation_in_progress:
            self.offer_pending = True
            return
        self.offer_pending = False
        self.negotiation_in_progress = True
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        self.webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _user_data):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            self.negotiation_in_progress = False
            detail = reply.to_string() if reply is not None else "no promise reply"
            print(f"failed to create WebRTC offer: {detail}", flush=True)
            self.stop()
            return
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        self.signaling.send({"type": "offer", "sdp": offer.sdp.as_text()})

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            self.pipeline_error_count += 1
            error, debug = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {error}; {debug}", flush=True)
            self._publish_zero_command("GStreamer error")
            self._adaptive_downshift("GStreamer error")
            if not self.recovering:
                self.recovering = True
                print("GStreamer recovery scheduled in 2 seconds", flush=True)
                GLib.timeout_add_seconds(2, self._recover_pipeline)
        elif message.type == Gst.MessageType.WARNING:
            self.pipeline_warning_count += 1
            warning, debug = message.parse_warning()
            print(f"GStreamer warning from {message.src.get_name()}: {warning}; {debug}", flush=True)
            self._adaptive_downshift("GStreamer warning")

    def _watchdog_check(self) -> bool:
        if self.last_control_message_time > 0.0:
            if time.monotonic() - self.last_control_message_time > self.args.cmd_watchdog_timeout:
                if not self.watchdog_tripped:
                    print(
                        f"cmd_vel watchdog timeout after {self.args.cmd_watchdog_timeout:.3f}s; publishing zero command",
                        flush=True,
                    )
                    self._publish_zero_command("cmd_vel watchdog")
                    self.watchdog_tripped = True
        return True

    def _publish_zero_command(self, reason: str):
        if self.control.publish(
            {
                "linear_x": 0.0,
                "linear_y": 0.0,
                "linear_z": 0.0,
                "angular_x": 0.0,
                "angular_y": 0.0,
                "angular_z": 0.0,
            }
        ):
            print(f"{reason}: zero /cmd_vel published", flush=True)

    def _recover_pipeline(self):
        print("Recovering robot GStreamer pipeline", flush=True)
        try:
            if self.ros_image_source is not None:
                self.ros_image_source.close()
                self.ros_image_source = None
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self.webrtc = None
            self.encoder = None
            self.frame_clock_channel = None
            self.frame_clock_open = False
            self.negotiation_in_progress = False
            self.last_remote_answer_sdp = ""
            self._start_pipeline()
            self.recovering = False
            print("Robot GStreamer pipeline recovered", flush=True)
        except Exception as exc:
            print(f"Robot GStreamer recovery failed: {exc}; retrying in 5 seconds", flush=True)
            GLib.timeout_add_seconds(5, self._recover_pipeline)
        return False

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
            print(f"Local ICE candidate: {candidate}", flush=True)
        self.signaling.send({"type": "ice", "sdpMLineIndex": int(mlineindex), "candidate": candidate})

    def _handle_signaling_message(self, message: dict):
        GLib.idle_add(self._handle_signaling_message_in_loop, message)

    def _handle_signaling_message_in_loop(self, message: dict):
        kind = message.get("type")
        if kind == "peer-ready" and message.get("role") == "machine":
            print("Machine peer is ready; refreshing WebRTC session", flush=True)
            self._recover_for_peer_ready()
            return False
        if kind == "answer":
            sdp = message.get("sdp", "")
            if not sdp or sdp == self.last_remote_answer_sdp:
                return False
            self.last_remote_answer_sdp = sdp
            answer = make_session_description("answer", sdp)
            self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
            self.negotiation_in_progress = False
            self.offer_pending = False
        elif kind == "ice":
            if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
                print(f"Remote ICE candidate: {message.get('candidate', '')}", flush=True)
            self.webrtc.emit("add-ice-candidate", int(message["sdpMLineIndex"]), message["candidate"])
        return False

    def _on_signaling_connected(self):
        GLib.idle_add(self._request_offer)

    def _request_offer(self):
        if self.webrtc is None:
            self.offer_pending = True
            return False
        self._on_negotiation_needed(self.webrtc)
        return False

    def _recover_for_peer_ready(self):
        now = time.monotonic()
        if self.recovering or now - self.last_peer_ready_recovery < 2.0:
            return False
        self.last_peer_ready_recovery = now
        self.recovering = True
        self._recover_pipeline()
        return False

    def _on_data_channel(self, _webrtc, channel):
        label = channel.props.label
        print(f"DataChannel received: {label}", flush=True)
        if label != "cmd-vel":
            return
        self._attach_control_channel(channel)

    def _attach_control_channel(self, channel):
        if channel is None:
            return
        channel.connect("on-open", lambda _channel: print("cmd-vel DataChannel open", flush=True))
        channel.connect("on-message-string", self._on_control_message)

    def _on_control_message(self, channel, text):
        receive_ns = time.time_ns()
        try:
            command = json.loads(text)
        except Exception:
            return
        if command.get("type") != "cmd_vel":
            return
        now = time.monotonic()
        if self.args.cmd_rate_limit_hz > 0:
            min_interval = 1.0 / self.args.cmd_rate_limit_hz
            if now - self.last_control_publish_time < min_interval:
                return
        self.last_control_publish_time = now
        self.last_control_message_time = now
        self.watchdog_tripped = False
        published = self.control.publish(command)
        ack = {
            "type": "cmd_vel_ack",
            "seq": command.get("seq"),
            "sent_ns": command.get("sent_ns"),
            "robot_receive_ns": receive_ns,
            "robot_ack_ns": time.time_ns(),
            "published_to_ros": published,
        }
        channel.emit("send-string", json.dumps(ack, separators=(",", ":")))

    def _on_frame_clock_open(self, _channel):
        self.frame_clock_open = True
        print("frame latency DataChannel open", flush=True)

    def _on_frame_clock_handoff(self, _identity, _buffer):
        if not self.frame_clock_open or self.frame_clock_channel is None:
            return
        now = time.monotonic()
        if self.args.latency_probe_rate > 0 and now - self.frame_clock_last_sec < 1.0 / self.args.latency_probe_rate:
            return
        self.frame_clock_last_sec = now
        payload = {
            "type": "frame_clock",
            "seq": self.frame_clock_seq,
            "sent_ns": time.time_ns(),
        }
        self.frame_clock_channel.emit("send-string", json.dumps(payload, separators=(",", ":")))
        self.frame_clock_seq += 1

    def _ensure_adaptive_bitrate(self):
        if not self.args.adaptive_bitrate or self.adaptive_source_id is not None:
            return
        interval_ms = max(250, int(self.args.bitrate_check_sec * 1000))
        self.adaptive_source_id = GLib.timeout_add(interval_ms, self._adaptive_bitrate_check)
        print(
            "Adaptive WebRTC bitrate enabled: "
            f"{self.args.min_bitrate_kbit}-{self.args.max_bitrate_kbit} kbit/s, "
            f"step {self.args.bitrate_step_kbit} kbit/s",
            flush=True,
        )

    def _encoder_bitrate_value(self, kbit: int) -> int:
        name = self.profile.get("GST_H264_ENCODER_NAME", "")
        if name in {"nvv4l2h264enc", "openh264enc"}:
            return int(kbit) * 1000
        return int(kbit)

    def _set_encoder_bitrate(self, kbit: int, announce: bool = True) -> bool:
        self.current_bitrate_kbit = max(self.args.min_bitrate_kbit, min(kbit, self.args.max_bitrate_kbit))
        if self.encoder is None or self.encoder.find_property("bitrate") is None:
            return False
        try:
            self.encoder.set_property("bitrate", self._encoder_bitrate_value(self.current_bitrate_kbit))
            if announce:
                print(f"Adaptive WebRTC bitrate set to {self.current_bitrate_kbit} kbit/s", flush=True)
            return True
        except Exception as exc:
            print(f"Adaptive WebRTC bitrate update skipped: {exc}", flush=True)
            return False

    def _adaptive_downshift(self, reason: str):
        if not self.args.adaptive_bitrate:
            return
        next_bitrate = max(self.args.min_bitrate_kbit, self.current_bitrate_kbit - self.args.bitrate_step_kbit)
        if next_bitrate < self.current_bitrate_kbit:
            print(f"{reason}: reducing WebRTC bitrate pressure", flush=True)
            self._set_encoder_bitrate(next_bitrate)

    def _adaptive_bitrate_check(self) -> bool:
        if self.encoder is None:
            return True
        warning_changed = self.pipeline_warning_count != self.last_adaptive_warning_count
        error_changed = self.pipeline_error_count != self.last_adaptive_error_count
        self.last_adaptive_warning_count = self.pipeline_warning_count
        self.last_adaptive_error_count = self.pipeline_error_count
        if warning_changed or error_changed or self.recovering:
            self._adaptive_downshift("WebRTC health check")
            return True
        if not self.signaling.connected.is_set():
            return True
        next_bitrate = min(self.args.max_bitrate_kbit, self.current_bitrate_kbit + self.args.bitrate_step_kbit)
        if next_bitrate > self.current_bitrate_kbit:
            self._set_encoder_bitrate(next_bitrate)
        return True


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signaling-url", default="")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--room", default="default")
    parser.add_argument("--profile", default=".webrtc_profile.env")
    parser.add_argument("--ice-servers", default="stun:stun.l.google.com:19302")
    parser.add_argument("--ice-transport-policy", choices=["all", "relay"], default=os.environ.get("WEBRTC_ICE_TRANSPORT_POLICY", "all"))
    parser.add_argument("--ros-cmd-topic", default="/cmd_vel")
    parser.add_argument("--cmd-watchdog-timeout", type=float, default=float(os.environ.get("WEBRTC_CMD_WATCHDOG_TIMEOUT", "0.3")))
    parser.add_argument("--cmd-rate-limit-hz", type=float, default=float(os.environ.get("WEBRTC_CMD_RATE_LIMIT_HZ", "100.0")))
    parser.add_argument("--video-bitrate-kbit", type=int, default=int(os.environ.get("VIDEO_BITRATE_KBIT", "6000")))
    parser.add_argument("--adaptive-bitrate", type=int, choices=[0, 1], default=int(os.environ.get("WEBRTC_ADAPTIVE_BITRATE", "1")))
    parser.add_argument("--min-bitrate-kbit", type=int, default=int(os.environ.get("WEBRTC_MIN_BITRATE_KBIT", "1000")))
    parser.add_argument("--max-bitrate-kbit", type=int, default=int(os.environ.get("WEBRTC_MAX_BITRATE_KBIT", os.environ.get("VIDEO_BITRATE_KBIT", "6000"))))
    parser.add_argument("--bitrate-step-kbit", type=int, default=int(os.environ.get("WEBRTC_BITRATE_STEP_KBIT", "500")))
    parser.add_argument("--bitrate-check-sec", type=float, default=float(os.environ.get("WEBRTC_BITRATE_CHECK_SEC", "2.0")))
    parser.add_argument("--video-source", choices=["ros2", "gst", "testsrc"], default="testsrc")
    parser.add_argument("--ros-image-topic", default="/camera/image_raw")
    parser.add_argument("--ros-image-qos", choices=["auto", "sensor_data", "default"], default="auto")
    parser.add_argument("--source-pipeline", default="")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--latency-probe", action="store_true")
    parser.add_argument("--latency-probe-rate", type=float, default=60.0)
    return parser.parse_args()


def main():
    H264RobotSender(parse_args()).start()


if __name__ == "__main__":
    main()
