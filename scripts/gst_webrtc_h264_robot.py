#!/usr/bin/env python3
"""Robot-side H.264 WebRTC sender with cmd_vel DataChannel receive."""

import argparse
import json
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
        self.profile = load_env_file(args.profile)
        self.loop = GLib.MainLoop()
        self.webrtc = None
        self.pipeline = None
        self.control = RosCmdPublisher(args.ros_cmd_topic)
        self.ros_image_source = None
        self.frame_clock_channel = None
        self.frame_clock_open = False
        self.frame_clock_seq = 0
        self.frame_clock_last_sec = 0.0
        self.data_channels_supported = True
        self.signaling = self._make_signaling()

    def _make_signaling(self):
        if self.args.signaling_url:
            return ClientSignaling(self.args.signaling_url, "robot", self.args.room, self._handle_signaling_message)
        return ServerSignaling(self.args.host, self.args.port, self._handle_signaling_message)

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
        return f"{name} {props}".strip()

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
        configure_webrtcbin(self.webrtc, self.args.ice_servers)
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
        self.signaling.start()
        desc = self._media_bin_description()
        print(f"Robot media pipeline: {desc}", flush=True)
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
        if self.args.duration > 0:
            GLib.timeout_add_seconds(int(self.args.duration), self.stop)
        try:
            self.loop.run()
        finally:
            self.stop()

    def stop(self):
        if self.ros_image_source is not None:
            self.ros_image_source.close()
            self.ros_image_source = None
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.control.close()
        if self.loop.is_running():
            self.loop.quit()
        return False

    def _on_negotiation_needed(self, _element):
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, None)
        self.webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _user_data):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            detail = reply.to_string() if reply is not None else "no promise reply"
            print(f"failed to create WebRTC offer: {detail}", flush=True)
            self.stop()
            return
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        self.signaling.send({"type": "offer", "sdp": offer.sdp.as_text()})

    def _on_bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {error}; {debug}", flush=True)
            self.stop()
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"GStreamer warning from {message.src.get_name()}: {warning}; {debug}", flush=True)

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        self.signaling.send({"type": "ice", "sdpMLineIndex": int(mlineindex), "candidate": candidate})

    def _handle_signaling_message(self, message: dict):
        GLib.idle_add(self._handle_signaling_message_in_loop, message)

    def _handle_signaling_message_in_loop(self, message: dict):
        kind = message.get("type")
        if kind == "answer":
            answer = make_session_description("answer", message["sdp"])
            self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        elif kind == "ice":
            self.webrtc.emit("add-ice-candidate", int(message["sdpMLineIndex"]), message["candidate"])
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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signaling-url", default="")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--room", default="default")
    parser.add_argument("--profile", default=".webrtc_profile.env")
    parser.add_argument("--ice-servers", default="stun:stun.l.google.com:19302")
    parser.add_argument("--ros-cmd-topic", default="/cmd_vel")
    parser.add_argument("--video-source", choices=["ros2", "gst", "testsrc"], default="testsrc")
    parser.add_argument("--ros-image-topic", default="/camera/image_raw")
    parser.add_argument("--ros-image-qos", choices=["sensor_data", "default"], default="sensor_data")
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
