#!/usr/bin/env python3
"""Robot-side H.264 WebRTC video sender."""

import argparse
import gc
import json
import logging
import math
import os
import signal
import time
import uuid
from pathlib import Path

import gi

from gst_webrtc_common import (
    ClientSignaling,
    Gst,
    ServerSignaling,
    configure_webrtcbin,
    data_channel_options,
    ensure_webrtc_runtime,
    ice_servers_from_registration,
    load_env_file,
    make_session_description,
)
from ros_image_io import RosImageAppSrc, ensure_rclpy
from webrtc_control import CommandGate, command_authorized
from webrtc_stats import BitrateController, extract_network_sample
from webrtc_streams import load_session, load_streams, ros_safe_name
from media_health import RecoveryBackoff, allocate_bitrates, write_status

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402
gi.require_version("GstRtp", "1.0")
from gi.repository import GstRtp

logger = logging.getLogger(__name__)


class RosCmdPublisher:
    def __init__(self, topic: str, max_linear_mps: float, max_angular_rps: float):
        self.topic = topic
        self.max_linear_mps = max(0.0, float(max_linear_mps))
        self.max_angular_rps = max(0.0, float(max_angular_rps))
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

    @staticmethod
    def _bounded_float(command: dict, key: str, limit: float) -> float:
        value = float(command.get(key, 0.0))
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if limit > 0.0:
            value = max(-limit, min(limit, value))
        return value

    def publish(self, command: dict) -> bool:
        if self.publisher is None:
            return False
        msg = self.twist_type()
        try:
            msg.linear.x = self._bounded_float(command, "linear_x", self.max_linear_mps)
            msg.linear.y = self._bounded_float(command, "linear_y", self.max_linear_mps)
            msg.linear.z = self._bounded_float(command, "linear_z", self.max_linear_mps)
            msg.angular.x = self._bounded_float(command, "angular_x", self.max_angular_rps)
            msg.angular.y = self._bounded_float(command, "angular_y", self.max_angular_rps)
            msg.angular.z = self._bounded_float(command, "angular_z", self.max_angular_rps)
        except (TypeError, ValueError) as exc:
            print(f"dropping invalid cmd_vel command: {exc}", flush=True)
            return False
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
        self.session, self.streams = self._load_stream_configuration()
        if self.args.streams_config:
            self.args.room = self.session["room"]
            self.args.video_bitrate_kbit = sum(stream["bitrate_kbit"] for stream in self.streams)
            self.args.min_bitrate_kbit = sum(stream["min_bitrate_kbit"] for stream in self.streams)
            self.args.max_bitrate_kbit = sum(stream["max_bitrate_kbit"] for stream in self.streams)
            budget = self.session.get("bitrate_budget_kbit", 0)
            if budget:
                self.args.max_bitrate_kbit = min(budget, self.args.max_bitrate_kbit)
                self.args.video_bitrate_kbit = min(self.args.video_bitrate_kbit, budget)
        if self.args.max_bitrate_kbit < self.args.min_bitrate_kbit:
            self.args.max_bitrate_kbit = self.args.min_bitrate_kbit
        self.profile = load_env_file(args.profile)
        if self.args.enable_control and not self.args.control_token and not self.args.allow_unauthenticated_control:
            raise SystemExit(
                "WEBRTC_CONTROL_ENABLED requires HORUS_WEBRTC_CONTROL_TOKEN. "
                "Set WEBRTC_ALLOW_UNAUTHENTICATED_CONTROL=1 only for trusted lab tests."
            )
        self.loop = GLib.MainLoop()
        self.webrtc = None
        self.pipeline = None
        self.encoder = None
        self.encoders = []
        self.control = RosCmdPublisher(
            args.ros_cmd_topic if args.enable_control else "",
            args.cmd_max_linear_mps,
            args.cmd_max_angular_rps,
        )
        self.ros_image_source = None
        self.ros_image_sources = []
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
        self.bitrate_controller = BitrateController(
            args.min_bitrate_kbit,
            args.max_bitrate_kbit,
            args.bitrate_step_kbit,
            bad_loss=args.bitrate_bad_loss,
            good_loss=args.bitrate_good_loss,
            bad_rtt_ms=args.bitrate_bad_rtt_ms,
            good_rtt_ms=args.bitrate_good_rtt_ms,
            good_samples_required=args.bitrate_good_samples,
        )
        self.stats_request_in_flight = False
        self.pipeline_ready = False
        self.relay_mode = bool(args.signaling_url)
        self.peer_ready = not self.relay_mode
        self.peer_session_id = ""
        self.command_gate = CommandGate(args.cmd_max_age_ms, args.cmd_max_future_ms)
        self.estop_latched = False
        self.recovering = False
        self.negotiation_in_progress = False
        self.offer_pending = False
        self.last_remote_answer_sdp = ""
        self.stopped = False
        self.generation = 0
        self.negotiation_id = ""
        self.negotiation_started = 0.0
        self.stats_started = 0.0
        self.bus = None
        self.bus_handler = None
        self.media_handlers = []
        self.media_probes = []
        self.request_pads = []
        self.recovery_timer = None
        self.recovery_backoff = RecoveryBackoff()
        self.duration_timer = None
        self.health_timer = None
        self.rtp_frames = {}
        self.rtp_last = {}
        self.last_rtp_timestamp = {}
        self.observation_start = time.monotonic()
        self.signaling = self._make_signaling()
        if self.args.enable_control:
            self._publish_zero_command("startup")

    def _load_stream_configuration(self):
        if not self.args.streams_config:
            stream = {
                "id": "primary",
                "label": "primary",
                "room": self.args.room,
                "input_topic": self.args.ros_image_topic,
                "output_topic": "",
                "width": self.args.width,
                "height": self.args.height,
                "fps": self.args.fps,
                "bitrate_kbit": self.args.video_bitrate_kbit,
                "min_bitrate_kbit": self.args.min_bitrate_kbit,
                "max_bitrate_kbit": self.args.max_bitrate_kbit,
                "video_source": self.args.video_source,
                "source_pipeline": self.args.source_pipeline,
                "ros_image_qos": self.args.ros_image_qos,
                "frame_id": "webrtc_camera",
            }
            return {"mode": "isolated", "room": self.args.room, "id": self.args.room}, [stream]
        os.environ["HORUS_STREAMS_CONFIG"] = self.args.streams_config
        root = Path(self.args.root).resolve()
        session = load_session(root, "robot")
        streams = load_streams(root, "robot")
        if getattr(self.args, "stream_id", ""):
            streams = [stream for stream in streams if stream["id"] == self.args.stream_id]
            if len(streams) != 1:
                raise SystemExit(f"Unknown or disabled camera stream: {self.args.stream_id}")
            return {**session, "mode": "isolated", "room": streams[0]["room"]}, streams
        if session["mode"] != "bundled":
            raise SystemExit("--streams-config requires session.mode=bundled (or HORUS_WEBRTC_SESSION_MODE=bundled)")
        if len(streams) > 32:
            raise SystemExit("A bundled WebRTC session supports at most 32 H.264 tracks")
        return session, streams

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

    def _video_source(self, stream: dict, index: int) -> str:
        if stream["video_source"] == "ros2":
            return (
                f"appsrc name=ros_image_src_{index} is-live=true format=time do-timestamp=true block=false "
                f"caps=video/x-raw,format=RGB,width={stream['width']},height={stream['height']},framerate={stream['fps']}/1"
            )
        if stream["source_pipeline"]:
            return stream["source_pipeline"]
        return (
            f"videotestsrc is-live=true pattern=ball ! "
            f"video/x-raw,width={stream['width']},height={stream['height']},framerate={stream['fps']}/1"
        )

    def _encoder(self, element_name: str = "video_encoder") -> str:
        name = self.profile.get("GST_H264_ENCODER_NAME", "")
        props = self.profile.get("GST_H264_ENCODER_PROPS", "")
        if not name:
            raise RuntimeError("No H.264 encoder selected. Run ./horus bootstrap robot.")
        return f"{name} name={element_name} {props}".strip()

    def _encoder_preprocess(self) -> str:
        return self.profile.get("GST_H264_ENCODER_PREPROCESS", "") or "videoconvert"

    def _source_transform(self, stream: dict) -> str:
        if stream["video_source"] == "ros2":
            suffix = ros_safe_name(stream["id"])
            return (
                f"videoconvert ! videoscale add-borders=false ! capsfilter name=scale_caps_{suffix} ! "
                f"videobox name=padding_{suffix} ! video/x-raw,width={stream['width']},height={stream['height']},"
                f"pixel-aspect-ratio=1/1,framerate={stream['fps']}/1 ! ")
        rate_filter = "" if stream["video_source"] == "ros2" else "videorate drop-only=true ! "
        borders = "true" if stream.get("resize_mode", "letterbox") == "letterbox" else "false"
        return (
            f"videoconvert ! videoscale add-borders={borders} ! {rate_filter}"
            f"video/x-raw,width={stream['width']},height={stream['height']},pixel-aspect-ratio=1/1,framerate={stream['fps']}/1 ! "
        )

    def _media_bin_description(self, stream: dict, index: int) -> str:
        encoder_name = f"video_encoder_{index}"
        return (
            f"{self._video_source(stream, index)} ! "
            "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
            f"{self._source_transform(stream)}"
            f"{self._encoder_preprocess()} ! "
            f"{self._encoder(encoder_name)} ! "
            "h264parse config-interval=1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            f"rtph264pay name=payloader_{index} pt={96 + index} config-interval=1 mtu=1200 aggregate-mode=zero-latency"
        )

    def _build_pipeline(self):
        pipeline = Gst.Pipeline.new("horus-h264-robot")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if self.webrtc is None:
            raise RuntimeError("failed to create webrtcbin")
        configure_webrtcbin(self.webrtc, self.args.ice_servers, self.args.ice_transport_policy)
        pipeline.add(self.webrtc)
        self.ros_image_sources = []
        self.sources_by_id = {}
        self.encoders = []
        for index, stream in enumerate(self.streams):
            media = Gst.parse_bin_from_description(self._media_bin_description(stream, index), True)
            if stream["video_source"] == "ros2":
                appsrc = media.get_by_name(f"ros_image_src_{index}")
                if appsrc is None:
                    raise RuntimeError(f"failed to create ROS image appsrc for {stream['id']}")
                source = RosImageAppSrc(
                    appsrc,
                    stream["input_topic"],
                    stream["fps"],
                    stream["ros_image_qos"],
                    node_name=f"horus_webrtc_source_{ros_safe_name(stream['id'])}",
                )
                self.ros_image_sources.append(source)
                source.camera_info_topic = stream.get("input_camera_info_topic", "")
                source.output_size = (stream["width"], stream["height"])
                source.resize_mode = stream.get("resize_mode", "letterbox")
                source.scale_caps = media.get_by_name(f"scale_caps_{ros_safe_name(stream['id'])}")
                source.padding = media.get_by_name(f"padding_{ros_safe_name(stream['id'])}")
                self.sources_by_id[stream["id"]] = source
            encoder = media.get_by_name(f"video_encoder_{index}")
            if encoder is not None:
                self.encoders.append((stream, encoder))
            probe_pad = media.get_by_name(f"payloader_{index}").get_static_pad("src")
            probe_id = probe_pad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                self._on_rtp_sent, (self.generation, stream["id"]))
            self.media_probes.append((probe_pad, probe_id))
            pipeline.add(media)
            srcpad = media.get_static_pad("src")
            request_pad_simple = getattr(self.webrtc, "request_pad_simple", None)
            sinkpad = request_pad_simple("sink_%u") if request_pad_simple is not None else None
            if sinkpad is None:
                sinkpad = self.webrtc.get_request_pad("sink_%u")
            if sinkpad is None:
                raise RuntimeError("failed to request webrtcbin sink pad; install gstreamer1.0-nice and rerun bootstrap")
            self.request_pads.append(sinkpad)
            if sinkpad.find_property("msid"):
                sinkpad.set_property("msid", stream["id"])
            elif len(self.streams) > 1:
                raise RuntimeError("Bundled cameras require GStreamer 1.22+ with negotiated MSID identity")
            result = srcpad.link(sinkpad)
            if result != Gst.PadLinkReturn.OK:
                raise RuntimeError(f"failed to link {stream['id']} to webrtcbin: {result}")
        self.encoder = self.encoders[0][1] if self.encoders else None
        self.ros_image_source = self.ros_image_sources[0] if self.ros_image_sources else None
        if self.encoders:
            self._set_encoder_bitrate(self.current_bitrate_kbit or self.args.video_bitrate_kbit, announce=False)
        return pipeline

    def start(self):
        Gst.init(None)
        ensure_webrtc_runtime()
        signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(self.stop))
        signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(self.stop))
        for index, stream in enumerate(self.streams):
            print(f"Robot media track {stream['id']}: {self._media_bin_description(stream, index)}", flush=True)
        if not self.relay_mode:
            self._start_pipeline()
        self._ensure_watchdog()
        if not self.relay_mode:
            self._ensure_adaptive_bitrate()
        self.signaling.start()
        self.health_timer = GLib.timeout_add_seconds(1, self._media_health)
        if self.args.duration > 0:
            self.duration_timer = GLib.timeout_add(max(1, int(self.args.duration * 1000)), self.stop)
        try:
            self.loop.run()
        finally:
            self.stop()

    def _start_pipeline(self):
        if self.stopped:
            return
        from media_health import recycle_media_process
        if self.generation and recycle_media_process(self):
            return
        self.generation += 1
        self.negotiation_id = uuid.uuid4().hex
        self.signaling.reset_negotiation(self.negotiation_id)
        self.data_channels_supported = True
        self.pipeline_ready = False
        self.pipeline = self._build_pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self.bus_handler = bus.connect("message", self._on_bus_message)
        self.bus = bus
        # Keep one bus watch/handler per pipeline generation.
        for name in ("ice-connection-state", "connection-state"):
            self._connect_media(self.webrtc, f"notify::{name}", self._on_connection_state, name)
        self._connect_media(self.webrtc, "on-negotiation-needed", self._on_negotiation_needed)
        self._connect_media(self.webrtc, "on-ice-candidate", self._on_ice_candidate)
        try:
            self._connect_media(self.webrtc, "on-data-channel", self._on_data_channel)
        except TypeError:
            self.data_channels_supported = False
            print("WebRTC DataChannel unavailable in this GStreamer runtime; cmd_vel over WebRTC disabled", flush=True)
        self.pipeline.set_state(Gst.State.PLAYING)
        for source in self.ros_image_sources:
            source.start()
        self._create_local_data_channels()

    def _create_local_data_channels(self):
        if (self.args.latency_probe or self.ros_image_sources) and self.data_channels_supported:
            self.frame_clock_channel = self.webrtc.emit("create-data-channel", "frame-clock", data_channel_options(ordered=False, max_retransmits=0))
            if self.frame_clock_channel is not None:
                self._connect_media(self.frame_clock_channel, "on-open", self._on_frame_clock_open)
            else:
                print("frame latency DataChannel unavailable; continuing without latency samples", flush=True)
        if self.args.enable_control and self.args.ros_cmd_topic and self.data_channels_supported:
            control_channel = self.webrtc.emit(
                "create-data-channel",
                "cmd-vel",
                data_channel_options(ordered=False, max_retransmits=0),
            )
            if control_channel is not None:
                self._attach_control_channel(control_channel)
            else:
                print("cmd-vel DataChannel unavailable; continuing with video only", flush=True)

    def _ensure_watchdog(self):
        if not self.args.enable_control or not self.args.ros_cmd_topic or self.watchdog_source_id is not None:
            return
        self.watchdog_source_id = GLib.timeout_add(100, self._watchdog_check)

    def stop(self):
        if self.stopped:
            return False
        self.stopped = True
        for name in ("watchdog_source_id", "adaptive_source_id", "recovery_timer", "duration_timer", "health_timer"):
            source = getattr(self, name)
            if source:
                GLib.source_remove(source)
                setattr(self, name, None)
        self.signaling.close()
        if self.args.enable_control:
            self._publish_zero_command("shutdown")
        self._stop_media_pipeline()
        write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"), {"role": "robot", "state": "stopped", "tracks": {}})
        self.control.close()
        if self.loop.is_running():
            self.loop.quit()
        return False

    def _connect_media(self, element, name, callback, *args):
        self.media_handlers.append((element, element.connect(name, callback, *args)))

    def _stop_media_pipeline(self):
        if self.recovery_timer is not None:
            source = GLib.MainContext.default().find_source_by_id(self.recovery_timer)
            if source is not None:
                source.destroy()
            self.recovery_timer = None
        self.generation += 1
        for element, handler in self.media_handlers:
            if element.handler_is_connected(handler):
                element.disconnect(handler)
        self.media_handlers.clear()
        for pad, probe in self.media_probes:
            pad.remove_probe(probe)
        self.media_probes.clear()
        if self.bus is not None:
            if self.bus_handler is not None:
                self.bus.disconnect(self.bus_handler)
            self.bus.remove_signal_watch()
            self.bus = self.bus_handler = None
        for source in self.ros_image_sources:
            source.close()
        self.ros_image_sources = []
        self.ros_image_source = None
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            for pad in self.request_pads:
                if self.webrtc is not None:
                    self.webrtc.release_request_pad(pad)
            self.pipeline = None
        self.request_pads.clear()
        self.sources_by_id.clear()
        self.pipeline_ready = False
        self.webrtc = None
        self.encoder = None
        self.encoders = []
        self.frame_clock_channel = None
        self.frame_clock_open = False
        self.rtp_last.clear()
        self.last_rtp_timestamp.clear()
        self.negotiation_in_progress = False
        self.offer_pending = False
        self.last_remote_answer_sdp = ""
        self.stats_request_in_flight = False
        gc.collect()

    def _on_negotiation_needed(self, _element):
        if self.stopped or _element is not self.webrtc:
            return
        if self.webrtc is None:
            self.offer_pending = True
            return
        if not self.pipeline_ready:
            self.offer_pending = True
            return
        if not self.signaling.connected.is_set():
            self.offer_pending = True
            return
        if self.negotiation_in_progress:
            return
        self.offer_pending = False
        self.negotiation_in_progress = True
        self.negotiation_started = time.monotonic()
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, (self.generation, self.webrtc))
        self.webrtc.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, _user_data):
        generation, element = _user_data
        if self.stopped or generation != self.generation or element is not self.webrtc:
            return
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            self.negotiation_in_progress = False
            detail = reply.to_string() if reply is not None else "no promise reply"
            print(f"failed to create WebRTC offer: {detail}", flush=True)
            GLib.idle_add(self._schedule_recovery)
            return
        self.webrtc.emit("set-local-description", offer, Gst.Promise.new())
        self.signaling.send({"type": "offer", "sdp": offer.sdp.as_text()})

    def _on_bus_message(self, _bus, message):
        if self.stopped or _bus is not self.bus:
            return
        if message.type == Gst.MessageType.STATE_CHANGED and message.src is self.pipeline:
            _old_state, new_state, _pending_state = message.parse_state_changed()
            if new_state == Gst.State.PLAYING:
                self.pipeline_ready = True
                if self.signaling.connected.is_set() and self.offer_pending:
                    GLib.idle_add(self._request_offer)
            elif new_state < Gst.State.PLAYING:
                self.pipeline_ready = False
        elif message.type == Gst.MessageType.ERROR:
            self.pipeline_error_count += 1
            error, debug = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {error}; {debug}", flush=True)
            self._publish_zero_command("GStreamer error")
            from media_runtime import invalidate_profile
            if invalidate_profile(self.args.profile, message.src):
                self.codec_exit_code = 75
                self.stop()
                return
            self._adaptive_downshift("GStreamer error")
            self._schedule_recovery()
        elif message.type == Gst.MessageType.EOS:
            self._schedule_recovery()
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
        self.recovery_timer = None
        if self.stopped:
            return False
        print("Recovering robot GStreamer pipeline", flush=True)
        try:
            self._stop_media_pipeline()
            if not self.relay_mode or self.peer_ready:
                self._start_pipeline()
                if self.stopped:
                    return False
                self.offer_pending = True
            self.recovering = False
            print(
                "Robot GStreamer pipeline recovered"
                if self.pipeline is not None
                else "Robot media stopped while waiting for a machine peer",
                flush=True,
            )
        except Exception as exc:
            print(f"Robot GStreamer recovery failed: {exc}", flush=True)
            self._schedule_recovery()
        return False

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        if self.stopped or _element is not self.webrtc:
            return
        if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
            print(f"Local ICE candidate: {candidate}", flush=True)
        self.signaling.send({"type": "ice", "sdpMLineIndex": int(mlineindex), "candidate": candidate})

    def _handle_signaling_message(self, message: dict):
        GLib.idle_add(self._handle_signaling_message_in_loop, message)

    def _handle_signaling_message_in_loop(self, message: dict):
        if self.stopped:
            return False
        kind = message.get("type")
        if kind == "registered":
            ice_servers = ice_servers_from_registration(self.args.ice_servers, message)
            if ice_servers != self.args.ice_servers:
                self.args.ice_servers = ice_servers
                if self.webrtc is not None:
                    configure_webrtcbin(self.webrtc, self.args.ice_servers, self.args.ice_transport_policy)
                print("Applied broker-issued TURN credentials to the robot session", flush=True)
            if self.relay_mode:
                self.peer_ready = bool(message.get("peer_ready", False))
                self.peer_session_id = str(message.get("peer_session_id") or "")
                if self.peer_ready:
                    self._start_relay_media_session(force_rebuild=self.pipeline is not None)
            return False
        if kind == "peer-ready" and message.get("role") == "machine":
            peer_session_id = str(message.get("peer_session_id") or "")
            if peer_session_id and peer_session_id == self.peer_session_id and self.peer_ready and self.pipeline is not None:
                return False
            self.peer_session_id = peer_session_id
            self.peer_ready = True
            print("Machine peer is ready; starting a fresh WebRTC media session", flush=True)
            self._start_relay_media_session(force_rebuild=self.pipeline is not None)
            return False
        if kind == "peer-left" and message.get("role") == "machine":
            self.peer_ready = False
            self.peer_session_id = ""
            self._publish_zero_command("machine peer disconnected")
            self._stop_media_pipeline()
            print("Machine peer left; robot media is idle", flush=True)
            return False
        if kind == "restart":
            self.peer_ready = True
            self._stop_media_pipeline()
            self._start_pipeline()
            self.offer_pending = True
            self._request_offer()
            return False
        if kind == "answer":
            if self.webrtc is None or message.get("negotiation_id", "") not in {"", self.negotiation_id}:
                return False
            sdp = message.get("sdp", "")
            if not sdp or sdp == self.last_remote_answer_sdp:
                return False
            self.last_remote_answer_sdp = sdp
            answer = make_session_description("answer", sdp)
            self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
            self.negotiation_in_progress = False
            self.offer_pending = False
        elif kind == "ice":
            if self.webrtc is None or message.get("negotiation_id", "") not in {"", self.negotiation_id}:
                return False
            if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
                print(f"Remote ICE candidate: {message.get('candidate', '')}", flush=True)
            self.webrtc.emit("add-ice-candidate", int(message["sdpMLineIndex"]), message["candidate"])
        return False

    def _on_signaling_connected(self):
        # Relay registration/peer-ready messages own startup; do not negotiate twice.
        if not self.relay_mode:
            GLib.idle_add(self._request_offer)

    def _request_offer(self):
        if self.stopped:
            return False
        if self.webrtc is None:
            self.offer_pending = True
            return False
        self._on_negotiation_needed(self.webrtc)
        return False

    def _start_relay_media_session(self, force_rebuild=False):
        if self.stopped:
            return False
        if not self.relay_mode or not self.peer_ready or not self.signaling.connected.is_set():
            return False
        if force_rebuild and self.pipeline is not None:
            self._stop_media_pipeline()
        if self.pipeline is None:
            self._start_pipeline()
            if self.stopped:
                return False
            self._ensure_adaptive_bitrate()
            print("Robot WebRTC media pipeline started for the active machine peer", flush=True)
        self.offer_pending = True
        self._request_offer()
        return False

    def _on_data_channel(self, _webrtc, channel):
        if self.stopped or _webrtc is not self.webrtc:
            return
        label = channel.props.label
        print(f"DataChannel received: {label}", flush=True)
        if label != "cmd-vel":
            return
        if not self.args.enable_control:
            print("Ignoring cmd-vel DataChannel because WebRTC control is disabled", flush=True)
            return
        self._attach_control_channel(channel)

    def _attach_control_channel(self, channel):
        if channel is None:
            return
        generation = self.generation
        for name, callback in (("on-open", self._on_control_channel_open),
                               ("on-close", self._on_control_channel_close),
                               ("on-error", self._on_control_channel_error),
                               ("on-message-string", self._on_control_message)):
            self._connect_media(channel, name, lambda *args, callback=callback: callback(*args)
                                if not self.stopped and generation == self.generation else None)

    def _on_control_channel_open(self, _channel):
        print("cmd-vel DataChannel open", flush=True)
        self._publish_zero_command("cmd_vel channel open")

    def _on_control_channel_close(self, _channel):
        print("cmd-vel DataChannel closed", flush=True)
        self._publish_zero_command("cmd_vel channel close")

    def _on_control_channel_error(self, _channel, error):
        print(f"cmd-vel DataChannel error: {error}", flush=True)
        self._publish_zero_command("cmd_vel channel error")

    def _on_control_message(self, channel, text):
        receive_ns = time.time_ns()
        try:
            command = json.loads(text)
        except Exception:
            logger.debug("Ignoring malformed cmd_vel command message: %s", text, exc_info=True)
            return
        if not command_authorized(
            command,
            expected_token=self.args.control_token,
            allow_unauthenticated=bool(self.args.allow_unauthenticated_control),
        ):
            print("dropping unauthorized cmd_vel command", flush=True)
            return
        decision = self.command_gate.validate(command, now_ns=receive_ns)
        if not decision.accepted:
            print(f"dropping WebRTC control command: {decision.reason}", flush=True)
            self._send_control_ack(channel, command, False, decision.reason, receive_ns)
            return
        command_type = command.get("type")
        if command_type == "estop":
            self.estop_latched = True
            self._publish_zero_command("WebRTC emergency stop")
            self._send_control_ack(channel, command, True, "estop latched", receive_ns)
            return
        if command_type == "estop_clear":
            self.estop_latched = False
            self._publish_zero_command("WebRTC emergency stop cleared")
            self._send_control_ack(channel, command, True, "estop cleared", receive_ns)
            return
        if self.estop_latched:
            self._send_control_ack(channel, command, False, "estop latched", receive_ns)
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
        self._send_control_ack(channel, command, published, "published" if published else "ROS publisher unavailable", receive_ns)

    @staticmethod
    def _send_control_ack(channel, command: dict, accepted: bool, reason: str, receive_ns: int):
        ack = {
            "type": "control_ack",
            "command_type": command.get("type"),
            "seq": command.get("seq"),
            "sent_ns": command.get("sent_ns"),
            "robot_receive_ns": receive_ns,
            "robot_ack_ns": time.time_ns(),
            "accepted": bool(accepted),
            "reason": reason,
            "published_to_ros": bool(accepted and command.get("type") == "cmd_vel"),
        }
        channel.emit("send-string", json.dumps(ack, separators=(",", ":")))

    def _on_frame_clock_open(self, _channel):
        if self.stopped or _channel is not self.frame_clock_channel:
            return
        self.frame_clock_open = True
        print("frame latency DataChannel open", flush=True)

    def _on_rtp_sent(self, _pad, info, context):
        generation, stream_id = context
        if self.stopped or generation != self.generation:
            return Gst.PadProbeReturn.OK
        buffers = info.get_buffer_list() if info.type & Gst.PadProbeType.BUFFER_LIST else None
        items = [buffers.get(index) for index in range(buffers.length())] if buffers is not None else [info.get_buffer()]
        for buffer in items:
            if buffer is None:
                continue
            ok, rtp = GstRtp.RTPBuffer.map(buffer, Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                timestamp = int(rtp.get_timestamp())
            finally:
                rtp.unmap()
            if self.last_rtp_timestamp.get(stream_id) == timestamp:
                continue
            self.last_rtp_timestamp[stream_id] = timestamp
            self.rtp_frames[stream_id] = self.rtp_frames.get(stream_id, 0) + 1
            self.rtp_last[stream_id] = time.monotonic()
            if self.frame_clock_open and self.frame_clock_channel is not None:
                payload = {"type": "frame_clock", "seq": self.rtp_frames[stream_id],
                           "sent_ns": time.time_ns(), "stage": "rtp_frame_sent",
                           "stream_id": stream_id, "rtp_timestamp": timestamp,
                           "negotiation_id": self.negotiation_id}
                source = self.sources_by_id.get(stream_id)
                segment_event = _pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
                if source is not None and segment_event is not None:
                    segment = segment_event.parse_segment()
                    running_time = segment.to_running_time(Gst.Format.TIME, buffer.pts)
                    metadata = source.metadata_for_running_time(int(running_time))
                    if metadata is not None:
                        payload["source"] = metadata
                self.frame_clock_channel.emit("send-string", json.dumps(payload, separators=(",", ":")))
        return Gst.PadProbeReturn.OK

    def _media_health(self):
        if self.stopped:
            return False
        now = time.monotonic()
        if self.webrtc is not None and not self.recovering and now - self.negotiation_started >= 30:
            if getattr(self.webrtc.get_property("connection-state"), "value_nick", "") == "connected":
                self.recovery_backoff.reset()
        if self.negotiation_in_progress and now - self.negotiation_started > 15:
            self._schedule_recovery()
        tracks = {}
        bitrates = {stream["id"]: value for (stream, _), value in zip(getattr(self, "encoders", []),
                    getattr(self, "applied_track_bitrates", []))}
        for stream in self.streams:
            key = stream["id"]
            last = self.rtp_last.get(key)
            tracks[key] = {"rtp_frames": self.rtp_frames.get(key, 0),
                           "applied_bitrate_kbit": bitrates.get(key),
                           "frame_age_sec": None if last is None else now - last,
                           "state": "waiting" if last is None else "flowing" if now - last < 3 else "stale"}
            source = getattr(self, "sources_by_id", {}).get(key)
            if source is not None:
                tracks[key]["source_frame_age_sec"] = None if not math.isfinite(source.last_push_sec) else now - source.last_push_sec
                tracks[key]["rate_dropped"] = source.rate_dropped
                tracks[key]["source_received_frames"] = source.total_received
                tracks[key]["source_pushed_frames"] = source.total_pushed
                # Active ROS input with a stalled encoder is different from a stopped publisher.
                waiting = now - self.negotiation_started > 15 if last is None else now - last > 5
                connected = self.webrtc is not None and getattr(self.webrtc.get_property("connection-state"), "value_nick", "") == "connected"
                if connected and waiting and now - source.last_push_sec < 2:
                    self._schedule_recovery()
        write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"),
                     {"role": "robot", "signaling_connected": self.signaling.connected.is_set(),
                      "state": "recovering" if self.recovery_timer is not None else "running",
                      "generation": self.generation, "tracks": tracks,
                      "recovery_attempts": self.recovery_backoff.attempts,
                      "applied_bitrate_kbit": self.current_bitrate_kbit})
        if self.signaling.connected.is_set() and self.negotiation_id:
            self.signaling.send({"type": "media-status", "negotiation_id": self.negotiation_id, "tracks": tracks})
        return True

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
        if not self.encoders:
            return False
        allocation = allocate_bitrates([stream for stream, _encoder in self.encoders], kbit)
        updated = 0
        applied = 0
        original = []
        try:
            for _stream, encoder in self.encoders:
                if encoder.find_property("bitrate") is None:
                    raise RuntimeError(f"{encoder.get_name()} has no runtime bitrate property")
                original.append(encoder.get_property("bitrate"))
            for (stream, encoder), share in zip(self.encoders, allocation):
                if encoder.find_property("bitrate") is None:
                    raise RuntimeError(f"{encoder.get_name()} has no runtime bitrate property")
                encoder.set_property("bitrate", self._encoder_bitrate_value(share))
                value = encoder.get_property("bitrate")
                applied += int(value / 1000) if self._encoder_bitrate_value(1) == 1000 else int(value)
                updated += 1
            self.current_bitrate_kbit = applied
            self.applied_track_bitrates = [int(encoder.get_property("bitrate") / 1000) if self._encoder_bitrate_value(1) == 1000
                                          else int(encoder.get_property("bitrate")) for _stream, encoder in self.encoders]
            if announce:
                print(
                    f"Adaptive WebRTC session bitrate set to {self.current_bitrate_kbit} kbit/s "
                    f"across {updated} track(s)",
                    flush=True,
                )
            return updated > 0
        except Exception as exc:
            for (_stream, encoder), value in zip(self.encoders, original):
                try:
                    encoder.set_property("bitrate", value)
                except Exception:
                    logger.warning("Could not restore encoder bitrate", exc_info=True)
            actual = []
            for _stream, encoder in self.encoders:
                try:
                    value = encoder.get_property("bitrate")
                    actual.append(int(value / 1000) if self._encoder_bitrate_value(1) == 1000 else int(value))
                except Exception:
                    actual.append(None)
            self.applied_track_bitrates = actual
            self.current_bitrate_kbit = sum(actual) if all(value is not None for value in actual) else None
            print(f"Adaptive bitrate update failed; actual per-track kbit/s={actual}: {exc}", flush=True)
            return False

    def _adaptive_downshift(self, reason: str):
        if not self.args.adaptive_bitrate or self.current_bitrate_kbit is None:
            return
        next_bitrate = max(self.args.min_bitrate_kbit, self.current_bitrate_kbit - self.args.bitrate_step_kbit)
        if next_bitrate < self.current_bitrate_kbit:
            print(f"{reason}: reducing WebRTC bitrate pressure", flush=True)
            self._set_encoder_bitrate(next_bitrate)

    def _adaptive_bitrate_check(self) -> bool:
        if self.current_bitrate_kbit is None:
            self._set_encoder_bitrate(self.args.video_bitrate_kbit)
            return True
        if self.stopped:
            return False
        now = time.monotonic()
        if self.negotiation_in_progress and now - self.negotiation_started > 15:
            self._schedule_recovery()
        if self.stats_request_in_flight and now - self.stats_started > 5:
            self.stats_request_in_flight = False
        if self.encoder is None:
            return True
        warning_changed = self.pipeline_warning_count != self.last_adaptive_warning_count
        error_changed = self.pipeline_error_count != self.last_adaptive_error_count
        self.last_adaptive_warning_count = self.pipeline_warning_count
        self.last_adaptive_error_count = self.pipeline_error_count
        if warning_changed or error_changed or self.recovering:
            self._adaptive_downshift("WebRTC health check")
            return True
        if not self.signaling.connected.is_set() or self.webrtc is None or self.stats_request_in_flight:
            return True
        self.stats_request_in_flight = True
        self.stats_started = now
        promise = Gst.Promise.new_with_change_func(self._on_stats_ready, (self.generation, self.stats_started))
        self.webrtc.emit("get-stats", None, promise)
        return True

    def _on_stats_ready(self, promise, _user_data):
        if self.stopped or _user_data != (self.generation, self.stats_started):
            return
        try:
            promise.wait()
            reply = promise.get_reply()
            sample = extract_network_sample(reply)
        except Exception as exc:
            logger.debug("WebRTC get-stats failed", exc_info=True)
            print(f"WebRTC stats unavailable: {exc}", flush=True)
            sample = None
        GLib.idle_add(self._apply_network_sample, sample, _user_data)

    def _apply_network_sample(self, sample, generation=None):
        if self.stopped or (generation is not None and generation != (self.generation, self.stats_started)):
            return False
        self.stats_request_in_flight = False
        if sample is None:
            return False
        target, reason = self.bitrate_controller.target(self.current_bitrate_kbit, sample)
        if target != self.current_bitrate_kbit:
            self._set_encoder_bitrate(target)
        if sample.known:
            loss = "unknown" if sample.packet_loss_fraction is None else f"{sample.packet_loss_fraction * 100.0:.2f}%"
            rtt = "unknown" if sample.round_trip_time_ms is None else f"{sample.round_trip_time_ms:.1f}ms"
            print(
                f"WebRTC network stats: loss={loss} rtt={rtt} nack={sample.nack_count} "
                f"pli={sample.pli_count} decision={reason}",
                flush=True,
            )
        return False

    def _schedule_recovery(self):
        if not self.stopped and self.recovery_timer is None:
            self.recovering = True
            delay = self.recovery_backoff.next_delay()
            print(f"Robot media recovery scheduled in {delay} seconds", flush=True)
            self.recovery_timer = GLib.timeout_add_seconds(delay, self._recover_pipeline)

    def _on_connection_state(self, element, _pspec, name):
        if element is self.webrtc and getattr(element.get_property(name), "value_nick", "") == "failed":
            GLib.idle_add(self._schedule_recovery)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--streams-config", default="")
    parser.add_argument("--stream-id", default="")
    parser.add_argument("--signaling-url", default="")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--room", default="default")
    parser.add_argument("--profile", default=".webrtc_profile.env")
    parser.add_argument("--ice-servers", default="stun:stun.l.google.com:19302")
    parser.add_argument("--ice-transport-policy", choices=["all", "relay"], default=os.environ.get("WEBRTC_ICE_TRANSPORT_POLICY", "all"))
    parser.add_argument(
        "--enable-control",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("WEBRTC_CONTROL_ENABLED", os.environ.get("WEBRTC_ENABLE_CONTROL", "0"))),
    )
    parser.add_argument("--control-token", default=os.environ.get("HORUS_WEBRTC_CONTROL_TOKEN", ""))
    parser.add_argument(
        "--allow-unauthenticated-control",
        type=int,
        choices=[0, 1],
        default=int(os.environ.get("WEBRTC_ALLOW_UNAUTHENTICATED_CONTROL", "0")),
    )
    parser.add_argument("--ros-cmd-topic", default="/cmd_vel")
    parser.add_argument("--cmd-max-linear-mps", type=float, default=float(os.environ.get("WEBRTC_CMD_MAX_LINEAR_MPS", "0.5")))
    parser.add_argument("--cmd-max-angular-rps", type=float, default=float(os.environ.get("WEBRTC_CMD_MAX_ANGULAR_RPS", "1.0")))
    parser.add_argument("--cmd-watchdog-timeout", type=float, default=float(os.environ.get("WEBRTC_CMD_WATCHDOG_TIMEOUT", "0.3")))
    parser.add_argument("--cmd-rate-limit-hz", type=float, default=float(os.environ.get("WEBRTC_CMD_RATE_LIMIT_HZ", "100.0")))
    parser.add_argument("--cmd-max-age-ms", type=float, default=float(os.environ.get("WEBRTC_CMD_MAX_AGE_MS", "2000.0")))
    parser.add_argument("--cmd-max-future-ms", type=float, default=float(os.environ.get("WEBRTC_CMD_MAX_FUTURE_MS", "5000.0")))
    parser.add_argument("--video-bitrate-kbit", type=int, default=int(os.environ.get("VIDEO_BITRATE_KBIT", "6000")))
    parser.add_argument("--adaptive-bitrate", type=int, choices=[0, 1], default=int(os.environ.get("WEBRTC_ADAPTIVE_BITRATE", "1")))
    parser.add_argument("--min-bitrate-kbit", type=int, default=int(os.environ.get("WEBRTC_MIN_BITRATE_KBIT", "1000")))
    parser.add_argument("--max-bitrate-kbit", type=int, default=int(os.environ.get("WEBRTC_MAX_BITRATE_KBIT", os.environ.get("VIDEO_BITRATE_KBIT", "6000"))))
    parser.add_argument("--bitrate-step-kbit", type=int, default=int(os.environ.get("WEBRTC_BITRATE_STEP_KBIT", "500")))
    parser.add_argument("--bitrate-check-sec", type=float, default=float(os.environ.get("WEBRTC_BITRATE_CHECK_SEC", "2.0")))
    parser.add_argument("--bitrate-bad-loss", type=float, default=float(os.environ.get("WEBRTC_BITRATE_BAD_LOSS", "0.08")))
    parser.add_argument("--bitrate-good-loss", type=float, default=float(os.environ.get("WEBRTC_BITRATE_GOOD_LOSS", "0.02")))
    parser.add_argument("--bitrate-bad-rtt-ms", type=float, default=float(os.environ.get("WEBRTC_BITRATE_BAD_RTT_MS", "350.0")))
    parser.add_argument("--bitrate-good-rtt-ms", type=float, default=float(os.environ.get("WEBRTC_BITRATE_GOOD_RTT_MS", "150.0")))
    parser.add_argument("--bitrate-good-samples", type=int, default=int(os.environ.get("WEBRTC_BITRATE_GOOD_SAMPLES", "3")))
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
    from media_runtime import invalidate_profile, recover_profile
    args = parse_args()
    recover_profile(args, "robot")
    sender = H264RobotSender(args)
    try:
        sender.start()
    except (RuntimeError, GLib.Error):
        invalidate_profile(args.profile)
        sender.stop()
        raise
    raise SystemExit(getattr(sender, "codec_exit_code", 0))


if __name__ == "__main__":
    main()
