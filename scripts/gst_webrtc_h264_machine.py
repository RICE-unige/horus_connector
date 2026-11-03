#!/usr/bin/env python3
"""Machine-side H.264 WebRTC video receiver."""

import argparse
import gc
from collections import defaultdict, deque
import json
import logging
import math
import os
import signal
import statistics
import time
import uuid
from pathlib import Path

import gi

from gst_webrtc_common import (
    ClientSignaling,
    Gst,
    ServerSignaling,
    configure_webrtcbin,
    ensure_webrtc_runtime,
    env_int,
    ice_servers_from_registration,
    load_env_file,
    make_session_description,
)
from ros_image_io import RosImagePublisher
from media_health import FrameMatcher, RecoveryBackoff, RollingRate, write_status
from frame_delivery import FrameDelivery
from webrtc_streams import load_session, load_streams, ros_safe_name

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402
gi.require_version("GstRtp", "1.0")
from gi.repository import GstRtp

logger = logging.getLogger(__name__)


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


def load_clock_offset(path: str | None) -> tuple[float, str]:
    if not path:
        return 0.0, "none"
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "offset_ms" not in payload:
        raise ValueError(f"{path} does not contain offset_ms")
    return float(payload["offset_ms"]), f"clock_offset_probe:{Path(path).name}"


class H264MachineReceiver:
    def __init__(self, args):
        self.args = args
        self.session, self.streams = self._load_stream_configuration()
        if len(self.streams) > 1:
            self.args.room = self.session["room"]
        self.profile = load_env_file(args.profile)
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self.webrtc = None
        self.cmd_channel = None
        self.cmd_seq = 0
        self.control_session_id = uuid.uuid4().hex
        self.cmd_rtts = deque(maxlen=10000)
        self.cmd_acks = 0
        self.video_frames = 0
        self.video_frames_by_stream = defaultdict(int)
        self.video_first_sec_by_stream = {}
        self.video_last_sec_by_stream = {}
        self.video_first_sec = None
        self.video_last_sec = None
        self.frame_clock = deque()
        self.frame_clock_by_stream = defaultdict(deque)
        self.frame_latency_samples = deque(maxlen=10000)
        self.frame_clock_dropped = 0
        self.frame_clock_overflow_dropped = 0
        self.ros_image_publisher = None
        self.ros_image_publishers = {}
        self.deliveries = {}
        self.decoder_bins = {}
        self.track_repair_times = {}
        self.track_repairs = defaultdict(int)
        self.decoded_rates = defaultdict(RollingRate)
        self.attached_streams = set()
        self.recovering = False
        self.answer_in_progress = False
        self.last_remote_offer_sdp = ""
        self.stopped = False
        self.generation = 0
        self.negotiation_id = ""
        self.peer_session_id = ""
        self.bus = None
        self.bus_handler = None
        self.media_handlers = []
        self.media_probes = []
        self.recovery_timer = None
        self.recovery_backoff = RecoveryBackoff()
        self.sources = []
        self.observation_start = time.monotonic()
        self.negotiation_started = self.observation_start
        self.peer_present = False
        self.last_restart = 0.0
        self.recovery_count = 0
        self.matchers = {stream["id"]: FrameMatcher() for stream in self.streams}
        if self.args.video_output in {"ros2", "both"}:
            for stream in self.streams:
                publisher = RosImagePublisher(
                    stream["output_topic"],
                    self.args.ros_image_encoding,
                    stream["frame_id"],
                    stream["ros_image_qos"],
                    node_name=f"horus_webrtc_publisher_{ros_safe_name(stream['id'])}",
                )
                publisher.camera_info_topic = stream.get("output_camera_info_topic", "")
                publisher.preserve_source_header = stream.get("preserve_source_header", False)
                publisher.start()
                self.ros_image_publishers[stream["id"]] = publisher
                self.deliveries[stream["id"]] = FrameDelivery(
                    publisher, lambda pts, key=stream["id"]: self.matchers[key].metadata_for_pts(pts),
                    wait_sec=stream.get("metadata_wait_ms", 80) / 1000)
            self.ros_image_publisher = next(iter(self.ros_image_publishers.values()), None)
        self.signaling = self._make_signaling()

    def _load_stream_configuration(self):
        if not self.args.streams_config:
            stream = {
                "id": "primary",
                "label": "primary",
                "room": self.args.room,
                "input_topic": "",
                "output_topic": self.args.ros_image_topic,
                "width": 0,
                "height": 0,
                "fps": 0,
                "bitrate_kbit": 0,
                "min_bitrate_kbit": 0,
                "max_bitrate_kbit": 0,
                "video_source": "ros2",
                "source_pipeline": "",
                "ros_image_qos": self.args.ros_image_qos,
                "frame_id": self.args.ros_image_frame_id,
            }
            return {"mode": "isolated", "room": self.args.room, "id": self.args.room}, [stream]
        os.environ["HORUS_STREAMS_CONFIG"] = self.args.streams_config
        root = Path(self.args.root).resolve()
        session = load_session(root, "machine")
        streams = load_streams(root, "machine")
        if getattr(self.args, "stream_id", ""):
            streams = [stream for stream in streams if stream["id"] == self.args.stream_id]
            if len(streams) != 1:
                raise SystemExit(f"Unknown or disabled camera stream: {self.args.stream_id}")
            return {**session, "mode": "isolated", "room": streams[0]["room"]}, streams
        if session["mode"] != "bundled":
            raise SystemExit("--streams-config requires session.mode=bundled (or HORUS_WEBRTC_SESSION_MODE=bundled)")
        return session, streams

    def _make_signaling(self):
        if self.args.signaling_url:
            return ClientSignaling(self.args.signaling_url, "machine", self.args.room, self._handle_signaling_message)
        return ServerSignaling(self.args.host, self.args.port, self._handle_signaling_message)

    def _decoder(self):
        name = self.profile.get("GST_H264_DECODER_NAME", "") or "avdec_h264"
        props = self.profile.get("GST_H264_DECODER_PROPS", "")
        return name, props

    def _decoder_postprocess(self) -> str:
        return self.profile.get("GST_H264_DECODER_POSTPROCESS", "")

    def _build_pipeline(self):
        self.generation += 1
        pipeline = Gst.Pipeline.new("horus-h264-machine")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if self.webrtc is None:
            raise RuntimeError("failed to create webrtcbin")
        configure_webrtcbin(self.webrtc, self.args.ice_servers, self.args.ice_transport_policy)
        self._connect_webrtc_diagnostics()
        pipeline.add(self.webrtc)
        return pipeline

    def _connect_webrtc_diagnostics(self):
        for name in ("signaling-state", "ice-gathering-state", "ice-connection-state", "connection-state"):
            try:
                self._connect_media(self.webrtc, f"notify::{name}", self._on_webrtc_state_change, name)
            except TypeError:
                logger.debug("WebRTC state property is unavailable: %s", name, exc_info=True)

    def _connect_media(self, element, signal, callback, *args):
        handler = element.connect(signal, callback, *args)
        self.media_handlers.append((element, handler))

    def _disconnect_media(self):
        for element, handler in self.media_handlers:
            if element.handler_is_connected(handler):
                element.disconnect(handler)
        self.media_handlers.clear()
        for pad, probe in self.media_probes:
            pad.remove_probe(probe)
        self.media_probes.clear()

    def _probe_media(self, pad, callback, context):
        probe = pad.add_probe(Gst.PadProbeType.BUFFER, callback, context)
        self.media_probes.append((pad, probe))

    def _on_webrtc_state_change(self, element, _pspec, name: str):
        try:
            value = element.get_property(name)
        except Exception:
            logger.debug("Failed to read WebRTC state property: %s", name, exc_info=True)
            return
        label = getattr(value, "value_nick", str(value))
        print(f"WebRTC {name}: {label}", flush=True)
        if element is self.webrtc and label == "failed" and not self.stopped:
            self._schedule_recovery()

    def start(self):
        Gst.init(None)
        ensure_webrtc_runtime()
        print("Machine media pipeline: webrtcbin receiver", flush=True)
        self.pipeline = self._build_pipeline()
        self._watch_bus()
        self._connect_media(self.webrtc, "on-ice-candidate", self._on_ice_candidate)
        self._connect_media(self.webrtc, "pad-added", self._on_incoming_stream)
        try:
            self._connect_media(self.webrtc, "on-data-channel", self._on_data_channel)
        except TypeError:
            print("WebRTC DataChannel unavailable in this GStreamer runtime; cmd_vel over WebRTC disabled", flush=True)
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        signal.signal(signal.SIGINT, self._handle_stop_signal)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.signaling.start()
        self.sources.append(GLib.timeout_add_seconds(1, self._health_check))
        if self.args.duration > 0:
            self.sources.append(GLib.timeout_add(max(1, int(self.args.duration * 1000)), self.stop))
        try:
            self.loop.run()
        finally:
            self.stop()

    def _handle_stop_signal(self, _signum, _frame):
        GLib.idle_add(self.stop)

    def stop(self):
        if self.stopped:
            return False
        self.stopped = True
        self.generation += 1
        self._disconnect_media()
        self._remove_bus_watch()
        for source in self.sources:
            registered = GLib.MainContext.default().find_source_by_id(source)
            if registered is not None:
                registered.destroy()
        self.sources.clear()
        cmd_timer = GLib.MainContext.default().find_source_by_id(getattr(self, "cmd_timer", 0)) if getattr(self, "cmd_timer", None) else None
        if cmd_timer is not None:
            cmd_timer.destroy()
        if self.recovery_timer:
            GLib.source_remove(self.recovery_timer)
            self.recovery_timer = None
        self.signaling.close()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.decoder_bins.clear()
        self.pipeline = self.webrtc = self.cmd_channel = None
        gc.collect()
        write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"), {"role": "machine", "state": "stopped", "tracks": {}})
        if self.video_frames or self.args.latency_json:
            observed_sec = max(time.monotonic() - self.observation_start, 0.001)
            payload = {
                "video_frames": self.video_frames,
                "measurement_stage": "sender_rtp_frame_to_decoded_output",
                "latency_scope": "packetization_to_decode_not_capture_or_display",
                "video_observed_sec": observed_sec,
                "video_decoded_fps": self.video_frames / observed_sec,
                "video_tracks": {
                    stream_id: {
                        "frames": frames,
                        "decoded_fps": frames
                        / max(
                            time.monotonic() - self.observation_start,
                            0.001,
                        ),
                    }
                    for stream_id, frames in sorted(self.video_frames_by_stream.items())
                },
            }
            if self.frame_latency_samples:
                latencies = [sample["latency_ms"] for sample in self.frame_latency_samples if sample["timing_valid"]]
                fresh_count = sum(1 for sample in self.frame_latency_samples if sample["fresh"])
                stale_count = sum(sample["timing_valid"] and sample["stale"] for sample in self.frame_latency_samples)
                payload.update(
                    {
                        "video_latency_samples": len(self.frame_latency_samples),
                        "video_latency_ms_median": statistics.median(latencies) if latencies else None,
                        "video_latency_ms_p50": percentile(latencies, 0.50),
                        "video_latency_ms_p95": percentile(latencies, 0.95),
                        "video_latency_ms_p99": percentile(latencies, 0.99),
                        "fresh_deadline_ms": self.args.fresh_deadline_ms,
                        "fresh_latency_samples": fresh_count,
                        "stale_latency_samples": stale_count,
                        "fresh_sample_sla": fresh_count / len(latencies) if latencies else None,
                        "fresh_fps_estimate": None,
                        "timing_invalid_samples": sum(not sample["timing_valid"] for sample in self.frame_latency_samples),
                        "latency_sample_retention": "last_10000_matched_samples_not_all_frames",
                        "clock_offset_ms": self.args.clock_offset_ms,
                        "clock_offset_source": self.args.clock_offset_source,
                        "frame_clock_dropped": self.frame_clock_dropped,
                        "frame_clock_overflow_dropped": self.frame_clock_overflow_dropped,
                        "freshest_frame_policy": "latest_decoded_frame_only",
                        "latency_method": "exact_RTP_timestamp_and_decoded_PTS_match; sender_packetization_to_decode_only_not_glass_to_glass",
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
                            "samples": list(self.frame_latency_samples),
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
        for delivery in self.deliveries.values():
            delivery.close()
        self.deliveries.clear()
        self.ros_image_publishers = {}
        self.ros_image_publisher = None
        if self.loop.is_running():
            self.loop.quit()
        return False

    def _on_ice_candidate(self, _element, mlineindex, candidate):
        if self.stopped or _element is not self.webrtc:
            return
        if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
            print(f"Local ICE candidate: {candidate}", flush=True)
        self.signaling.send({"type": "ice", "sdpMLineIndex": int(mlineindex), "candidate": candidate})

    def _handle_signaling_message(self, message):
        GLib.idle_add(self._handle_signaling_message_in_loop, message)

    def _handle_signaling_message_in_loop(self, message):
        if self.stopped:
            return False
        kind = message.get("type")
        if kind == "registered":
            ice_servers = ice_servers_from_registration(self.args.ice_servers, message)
            if ice_servers != self.args.ice_servers:
                self.args.ice_servers = ice_servers
                if self.webrtc is not None:
                    configure_webrtcbin(self.webrtc, self.args.ice_servers, self.args.ice_transport_policy)
                print("Applied broker-issued TURN credentials to the machine session", flush=True)
            return False
        if kind == "media-status" and message.get("negotiation_id") == self.negotiation_id:
            self.sender_health = (time.monotonic(), message.get("tracks", {}))
            return False
        if kind == "peer-ready" and message.get("role") == "robot":
            self.peer_present = True
            peer_session_id = str(message.get("peer_session_id") or "")
            if peer_session_id and peer_session_id == self.peer_session_id:
                return False
            self.peer_session_id = peer_session_id
            if self.last_remote_offer_sdp:
                print("Robot peer was replaced; resetting the machine WebRTC session", flush=True)
                self._recover_pipeline(request_offer=False)
            return False
        if kind == "peer-left" and message.get("role") == "robot":
            self.peer_present = False
            if self.last_remote_offer_sdp:
                print("Robot peer left; resetting the machine WebRTC session", flush=True)
                self._recover_pipeline(request_offer=False)
            return False
        if kind == "offer":
            sdp = message.get("sdp", "")
            if not sdp or sdp == self.last_remote_offer_sdp:
                return False
            new_id = str(message.get("negotiation_id") or "")
            if self.last_remote_offer_sdp and new_id != self.negotiation_id:
                self._recover_pipeline(request_offer=False)
            self.negotiation_id = new_id
            self.peer_session_id = str(message.get("session_id") or "")
            self.signaling.reset_negotiation(new_id)
            self.peer_present = True
            self.negotiation_started = time.monotonic()
            self.last_remote_offer_sdp = sdp
            self.answer_in_progress = True
            offer = make_session_description("offer", sdp)
            self.webrtc.emit("set-remote-description", offer, Gst.Promise.new())
            promise = Gst.Promise.new_with_change_func(self._on_answer_created, (self.generation, self.webrtc))
            self.webrtc.emit("create-answer", None, promise)
        elif kind == "ice":
            if message.get("negotiation_id", "") != self.negotiation_id:
                return False
            if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
                print(f"Remote ICE candidate: {message.get('candidate', '')}", flush=True)
            self.webrtc.emit("add-ice-candidate", int(message["sdpMLineIndex"]), message["candidate"])
        return False

    def _on_answer_created(self, promise, _user_data):
        generation, element = _user_data
        if self.stopped or generation != self.generation or element is not self.webrtc:
            return
        promise.wait()
        reply = promise.get_reply()
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None:
            self.answer_in_progress = False
            detail = reply.to_string() if reply is not None else "no promise reply"
            print(f"failed to create WebRTC answer: {detail}", flush=True)
            self._schedule_recovery()
            return
        element.emit("set-local-description", answer, Gst.Promise.new())
        self.signaling.send({"type": "answer", "sdp": answer.sdp.as_text()})
        self.answer_in_progress = False

    def _on_bus_message(self, _bus, message):
        if self.stopped or _bus is not self.bus:
            return
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"GStreamer error from {message.src.get_name()}: {error}; {debug}", flush=True)
            from media_runtime import invalidate_profile
            if invalidate_profile(self.args.profile, message.src):
                self.codec_exit_code = 75
                self.stop()
                return
            for key, media in self.decoder_bins.items():
                if message.src == media or message.src.has_as_ancestor(media):
                    self._repair_track(key)
                    return
            self._schedule_recovery()
        elif message.type == Gst.MessageType.EOS:
            self._schedule_recovery()
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"GStreamer warning from {message.src.get_name()}: {warning}; {debug}", flush=True)

    def _watch_bus(self):
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus_handler = self.bus.connect("message", self._on_bus_message)

    def _remove_bus_watch(self):
        if self.bus is not None:
            if self.bus_handler is not None:
                self.bus.disconnect(self.bus_handler)
            self.bus.remove_signal_watch()
        self.bus = self.bus_handler = None

    def _schedule_recovery(self):
        if self.stopped or self.recovery_timer is not None:
            return
        self.recovering = True
        delay = self.recovery_backoff.next_delay()
        print(f"Machine media recovery scheduled in {delay} seconds", flush=True)
        self.recovery_timer = GLib.timeout_add_seconds(delay, self._recover_pipeline)

    def _health_check(self):
        if self.stopped:
            return False
        now = time.monotonic()
        tracks = {}
        for stream in self.streams:
            stream_id = stream["id"]
            last = self.video_last_sec_by_stream.get(stream_id)
            tracks[stream_id] = {"decoded_frames": self.video_frames_by_stream[stream_id],
                                 "frame_age_sec": None if last is None else now - last,
                                 "decoded_fps": self.decoded_rates[stream_id].rate(now),
                                 "state": "waiting" if last is None else "flowing" if now - last < 3 else "stale"}
            publisher = self.ros_image_publishers.get(stream_id)
            if publisher is not None:
                tracks[stream_id].update(ros_published_frames=publisher.published_frames,
                                        camera_info_frames=publisher.camera_info_frames,
                                        metadata_missing_frames=publisher.metadata_missing_frames,
                                        published_fps=publisher.published_rate.rate(now),
                                        header_clock="ros_source" if publisher.preserve_source_header else "receiver_ros_receipt",
                                        published_age_sec=None if publisher.last_published_sec is None or
                                            publisher.last_published_epoch != publisher.active_delivery_epoch else now - publisher.last_published_sec)
                age = tracks[stream_id]["published_age_sec"]
                tracks[stream_id]["decoded_state"] = tracks[stream_id]["state"]
                tracks[stream_id]["state"] = "waiting" if age is None else "flowing" if age < 3 else "stale"
                delivery = self.deliveries[stream_id]
                tracks[stream_id].update(delivery_replaced=delivery.replaced, metadata_expired=delivery.expired,
                                        publication_failures=delivery.failures, publication_rejected=delivery.rejected,
                                        delivery_queue=len(delivery.pending))
        write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"),
                     {"role": "machine", "signaling_connected": self.signaling.connected.is_set(),
                      "state": "recovering" if self.recovering else "running", "tracks": tracks,
                      "recovery_attempts": self.recovery_backoff.attempts,
                      "generation": self.generation, "recoveries": self.recovery_count})
        # A silent publisher is reported stale, not used to restart a healthy peer.
        if self.peer_present and self.signaling.connected.is_set() and self.webrtc is not None:
            state = getattr(self.webrtc.get_property("connection-state"), "value_nick", "")
            if state == "connected" and not self.recovering and now - self.negotiation_started >= 30:
                self.recovery_backoff.reset()
            deadline = float(os.environ.get("WEBRTC_NEGOTIATION_TIMEOUT_SEC", "15"))
            if state in {"new", "connecting", "failed", "disconnected"} and now - self.negotiation_started > deadline:
                if now - self.last_restart > deadline:
                    self._schedule_recovery()
            sender_time, sender_tracks = getattr(self, "sender_health", (0, {}))
            if state == "connected" and now - sender_time < 3 and now - self.last_restart > 15:
                for stream_id, details in sender_tracks.items():
                    local_age = tracks.get(stream_id, {}).get("frame_age_sec")
                    if details.get("state") == "flowing" and (local_age is not None and local_age > 5
                                                               or local_age is None and now - self.negotiation_started > 15):
                        self._repair_track(stream_id)
                        break
        return True

    def _repair_track(self, stream_id):
        now = time.monotonic()
        if now - self.track_repair_times.get(stream_id, -float("inf")) < env_int("WEBRTC_TRACK_REPAIR_RETRY_SEC", 3):
            return
        self.track_repair_times[stream_id] = now
        media = self.decoder_bins.get(stream_id)
        if media is None or self.track_repairs[stream_id] >= env_int("WEBRTC_TRACK_REPAIR_LIMIT", 1, 0):
            self._schedule_recovery()
            return
        self.track_repairs[stream_id] += 1
        self.matchers[stream_id] = FrameMatcher()
        self.video_last_sec_by_stream.pop(stream_id, None)
        if stream_id in self.deliveries:
            self.deliveries[stream_id].reset()
        print(f"Recovering decoder track={stream_id}, leaving other tracks connected", flush=True)
        media.set_state(Gst.State.READY)
        media.sync_state_with_parent()

    def _recover_pipeline(self, request_offer=True):
        if self.recovery_timer is not None:
            source = GLib.MainContext.default().find_source_by_id(self.recovery_timer)
            if source is not None:
                source.destroy()
        self.recovery_timer = None
        if self.stopped:
            return False
        from media_health import recycle_media_process
        if recycle_media_process(self):
            return False
        self.generation += 1
        self.last_restart = time.monotonic()
        for delivery in self.deliveries.values():
            delivery.reset()
        self.decoder_bins.clear()
        self.track_repairs.clear()
        self.track_repair_times.clear()
        self.video_last_sec_by_stream.clear()
        self.sender_health = (0, {})
        if getattr(self, "cmd_timer", None):
            source = GLib.MainContext.default().find_source_by_id(self.cmd_timer)
            if source is not None:
                source.destroy()
            self.cmd_timer = None
        self.recovery_count += 1
        print("Recovering Machine GStreamer pipeline...", flush=True)
        try:
            self._disconnect_media()
            self._remove_bus_watch()
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
            self.webrtc = None
            self.cmd_channel = None
            # GI signal closures can retain an otherwise stopped ICE graph.
            # Collect only at teardown, never on the frame-delivery path.
            gc.collect()
            self.frame_clock.clear()
            self.frame_clock_by_stream.clear()
            self.matchers = {stream["id"]: FrameMatcher() for stream in self.streams}
            self.attached_streams.clear()
            self.answer_in_progress = False
            self.last_remote_offer_sdp = ""

            # Rebuild pipeline
            self.pipeline = self._build_pipeline()
            self._watch_bus()
            self._connect_media(self.webrtc, "on-ice-candidate", self._on_ice_candidate)
            self._connect_media(self.webrtc, "pad-added", self._on_incoming_stream)
            try:
                self._connect_media(self.webrtc, "on-data-channel", self._on_data_channel)
            except TypeError:
                pass
            self.pipeline.set_state(Gst.State.PLAYING)
            self.recovering = False
            self.negotiation_started = time.monotonic()
            if request_offer:
                self.signaling.send({"type": "restart", "request_id": uuid.uuid4().hex})
            print("Machine pipeline successfully recovered and listening.", flush=True)
        except Exception as e:
            print(f"Failed to recover Machine pipeline: {e}", flush=True)
            self._schedule_recovery()
        return False

    def _on_incoming_stream(self, _webrtc, pad):
        if self.stopped or _webrtc is not self.webrtc:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or not caps.to_string().startswith("application/x-rtp"):
            return
        name, props = self._decoder()
        postprocess = self._decoder_postprocess()
        decoder = f"{name} {props}".strip()
        tail = f"{postprocess} ! " if postprocess else ""
        stream = self._stream_for_pad(pad)
        if stream is None:
            print(f"Ignoring unconfigured incoming WebRTC track {pad.get_name()}", flush=True)
            return
        stream_id = stream["id"]
        self.attached_streams.add(stream_id)
        self._probe_media(pad, self._on_rtp_packet, (self.generation, stream_id))
        description = self._media_description(decoder, tail, stream)
        print(
            f"Incoming RTP video track={stream_id}; decoder={decoder}; postprocess={postprocess or 'none'}",
            flush=True,
        )
        media = Gst.parse_bin_from_description(description, True)
        suffix = ros_safe_name(stream_id)
        sink = media.get_by_name(f"video_sink_{suffix}")
        ros_sink = media.get_by_name(f"ros_image_sink_{suffix}")
        if self.args.video_output in {"display", "both"} and sink is None:
            raise RuntimeError("failed to create video sink")
        if ros_sink is not None:
            ros_sink.set_property("emit-signals", True)
            self._connect_media(ros_sink, "new-sample", self._on_ros_image_sample, stream_id, self.generation)
        if sink is not None:
            if self.args.video_output == "display":
                self._probe_media(sink.get_static_pad("sink"), self._on_display_probe,
                                  (self.generation, stream_id))
            if self.args.video_sink.split()[0] == "fakesink":
                sink.set_property("sync", False)
            else:
                if sink.find_property("sync"):
                    sink.set_property("sync", False)
        self.pipeline.add(media)
        self.decoder_bins[stream_id] = media
        media.sync_state_with_parent()
        result = pad.link(media.get_static_pad("sink"))
        if result != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"failed to link WebRTC pad: {result}")

    def _stream_for_pad(self, pad):
        try:
            msid = str(pad.get_property("msid") or "") if pad.find_property("msid") else ""
        except Exception:
            msid = ""
        if msid:
            matches = [stream for stream in self.streams if stream["id"] in msid.split()]
            return matches[0] if len(matches) == 1 else None
        return self.streams[0] if len(self.streams) == 1 and not self.attached_streams else None

    def _media_description(self, decoder: str, tail: str, stream: dict) -> str:
        suffix = ros_safe_name(stream["id"])
        base = (
            "queue max-size-buffers=0 max-size-bytes=4194304 max-size-time=100000000 ! "
            "rtph264depay request-keyframe=true wait-for-keyframe=true ! video/x-h264,alignment=au ! h264parse ! "
            f"{decoder} ! "
            f"{tail}"
            "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        )

        if self.args.video_output == "ros2":
            caps = self.ros_image_publishers[stream["id"]].caps_filter()
            return (
                base
                + "videoconvert ! "
                + caps
                + f" ! appsink name=ros_image_sink_{suffix} emit-signals=true sync=false max-buffers=1 drop=true"
            )

        if self.args.video_output == "both":
            caps = self.ros_image_publishers[stream["id"]].caps_filter()
            return (
                base
                + "videoconvert ! "
                + caps
                + " ! tee name=video_tee "
                + "video_tee. ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "
                + f"! appsink name=ros_image_sink_{suffix} emit-signals=true sync=false max-buffers=1 drop=true "
                + "video_tee. ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "
                + f"! videoconvert ! {self.args.video_sink} name=video_sink_{suffix}"
            )

        return base + f"videoconvert ! {self.args.video_sink} name=video_sink_{suffix}"

    def _on_rtp_packet(self, _pad, info, context):
        generation, stream_id = context
        buffer = info.get_buffer()
        if self.stopped or generation != self.generation or buffer is None:
            return Gst.PadProbeReturn.OK
        ok, rtp = GstRtp.RTPBuffer.map(buffer, Gst.MapFlags.READ)
        if ok:
            try:
                self.matchers[stream_id].packet(int(buffer.pts), int(rtp.get_timestamp()))
            finally:
                rtp.unmap()
        return Gst.PadProbeReturn.OK

    def _on_display_probe(self, _pad, info, context):
        generation, stream_id = context
        if not self.stopped and generation == self.generation:
            self._on_video_frame(None, info.get_buffer(), None, stream_id)
        return Gst.PadProbeReturn.OK

    def _on_video_frame(self, _sink, _buffer, _pad, stream_id="primary"):
        if self.stopped:
            return
        now = time.monotonic()
        self._count_video_frame(stream_id, now)
        self._record_frame_latency(now, "decoded", stream_id, _buffer)

    def _on_ros_image_sample(self, sink, stream_id="primary", generation=None):
        if self.stopped or generation is not None and generation != self.generation:
            return Gst.FlowReturn.FLUSHING
        delivery = self.deliveries.get(stream_id)
        epoch = delivery.epoch if delivery is not None else None
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        if self.stopped or generation is not None and generation != self.generation:
            return Gst.FlowReturn.FLUSHING
        now = time.monotonic()
        self._count_video_frame(stream_id, now)
        if delivery is not None:
            delivery.submit(sample, epoch)
        self._record_frame_latency(now, "ros2", stream_id, sample.get_buffer())
        return Gst.FlowReturn.OK

    def _count_video_frame(self, stream_id: str, now: float):
        if self.video_first_sec is None:
            self.video_first_sec = now
        self.video_last_sec = now
        self.video_frames += 1
        if stream_id not in self.video_first_sec_by_stream:
            self.video_first_sec_by_stream[stream_id] = now
        self.video_last_sec_by_stream[stream_id] = now
        self.video_frames_by_stream[stream_id] += 1
        self.decoded_rates[stream_id].add(now)
        self.track_repairs[stream_id] = 0

    def _record_frame_latency(self, now: float, output_stage: str, stream_id: str = "primary", buffer=None):
        if buffer is None:
            return
        matched = self.matchers[stream_id].decoded(int(buffer.pts),
                    {"receive_wall_ns": time.time_ns(), "now": now, "output_stage": output_stage})
        if matched:
            self._append_matched_latency(*matched, stream_id)

    def _append_matched_latency(self, sent, output, stream_id):
        receive_wall_ns = output["receive_wall_ns"]
        raw_latency_ms = (receive_wall_ns - int(sent["sent_ns"])) / 1_000_000.0
        latency_ms = raw_latency_ms - self.args.clock_offset_ms
        clock_known = self.args.clock_offset_source != "none"
        timing_valid = clock_known and math.isfinite(latency_ms) and latency_ms >= 0
        is_fresh = timing_valid and latency_ms <= self.args.fresh_deadline_ms
        self.frame_latency_samples.append(
            {
                "seq": sent.get("seq"),
                "output_stage": output["output_stage"],
                "stream_id": stream_id,
                "receive_t_sec": output["now"] - self.observation_start,
                "latency_ms": latency_ms if clock_known else None,
                "clock_synchronized": clock_known,
                "raw_latency_ms": raw_latency_ms,
                "send_wall_ns": int(sent["sent_ns"]),
                "receive_wall_ns": receive_wall_ns,
                "fresh": is_fresh,
                "stale": timing_valid and not is_fresh,
                "timing_valid": timing_valid,
                "rtp_timestamp": sent["rtp_timestamp"],
                "measurement_stage": "sender_rtp_frame_to_decoded_output",
                "source": sent.get("source"),
            }
        )

    def _on_data_channel(self, _webrtc, channel):
        if self.stopped or _webrtc is not self.webrtc:
            return
        print(f"DataChannel received: {channel.props.label}", flush=True)
        if channel.props.label == "frame-clock":
            self._connect_media(channel, "on-message-string", self._on_frame_clock_message)
            return
        if channel.props.label != "cmd-vel":
            return
        self.cmd_channel = channel
        self._connect_media(channel, "on-open", self._on_cmd_channel_open)
        self._connect_media(channel, "on-message-string", self._on_cmd_message)

    def _on_frame_clock_message(self, _channel, text):
        if self.stopped:
            return
        try:
            message = json.loads(text)
        except Exception:
            logger.debug("Ignoring malformed frame clock message: %s", text, exc_info=True)
            return
        if message.get("type") != "frame_clock" or "sent_ns" not in message or "rtp_timestamp" not in message:
            return
        stream_id = str(message.get("stream_id") or "primary")
        if stream_id not in self.matchers or message.get("negotiation_id") != self.negotiation_id:
            return
        matched = self.matchers[stream_id].marker(message)
        delivery = self.deliveries.get(stream_id)
        if delivery is not None:
            delivery.wake()
        if matched:
            self._append_matched_latency(*matched, stream_id)

    def _on_cmd_channel_open(self, _channel):
        if self.stopped or _channel is not self.cmd_channel:
            return
        print("cmd-vel DataChannel open", flush=True)
        if self.args.cmd_rate > 0:
            interval_ms = max(1, int(1000.0 / self.args.cmd_rate))
            existing = getattr(self, "cmd_timer", None)
            if existing:
                source = GLib.MainContext.default().find_source_by_id(existing)
                if source is not None:
                    source.destroy()
            generation = self.generation
            self.cmd_timer = GLib.timeout_add(interval_ms, lambda: self._send_cmd_vel()
                              if generation == self.generation else False)

    def _send_cmd_vel(self):
        if self.stopped:
            return False
        if self.cmd_channel is None:
            return True
        command = {
            "type": "cmd_vel",
            "seq": self.cmd_seq,
            "sent_ns": time.time_ns(),
            "session_id": self.control_session_id,
            "linear_x": self.args.cmd_linear_x,
            "linear_y": 0.0,
            "linear_z": 0.0,
            "angular_x": 0.0,
            "angular_y": 0.0,
            "angular_z": self.args.cmd_angular_z,
        }
        if self.args.control_token:
            command["control_token"] = self.args.control_token
        self.cmd_channel.emit("send-string", json.dumps(command, separators=(",", ":")))
        self.cmd_seq += 1
        return True

    def _on_cmd_message(self, _channel, text):
        if self.stopped or _channel is not self.cmd_channel:
            return
        try:
            ack = json.loads(text)
        except Exception:
            logger.debug("Ignoring malformed cmd_vel ack message: %s", text, exc_info=True)
            return
        if ack.get("type") not in {"cmd_vel_ack", "control_ack"}:
            return
        if ack.get("command_type", "cmd_vel") != "cmd_vel":
            return
        sent_ns = int(ack.get("sent_ns") or 0)
        if sent_ns > 0:
            self.cmd_rtts.append((time.time_ns() - sent_ns) / 1_000_000.0)
        self.cmd_acks += 1


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
    parser.add_argument("--ice-transport-policy", choices=["all", "relay"], default="all")
    parser.add_argument("--video-sink", default="fakesink")
    parser.add_argument("--video-output", choices=["ros2", "display", "both"], default="display")
    parser.add_argument("--ros-image-topic", default="/camera/webrtc/image_raw")
    parser.add_argument("--ros-image-encoding", choices=["rgb8", "bgr8", "rgba8", "bgra8", "mono8"], default="rgb8")
    parser.add_argument("--ros-image-frame-id", default="webrtc_camera")
    parser.add_argument("--ros-image-qos", choices=["auto", "sensor_data", "default"], default="auto")
    parser.add_argument("--cmd-rate", type=float, default=0.0)
    parser.add_argument("--cmd-linear-x", type=float, default=0.0)
    parser.add_argument("--cmd-angular-z", type=float, default=0.0)
    parser.add_argument("--control-token", default=os.environ.get("HORUS_WEBRTC_CONTROL_TOKEN", ""))
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--clock-offset-ms", type=float, default=None)
    parser.add_argument("--clock-offset-json", default="")
    parser.add_argument("--fresh-deadline-ms", type=float, default=150.0)
    parser.add_argument("--latency-json", default="")
    args = parser.parse_args()
    if args.clock_offset_ms is None:
        args.clock_offset_ms, args.clock_offset_source = load_clock_offset(args.clock_offset_json)
    else:
        args.clock_offset_source = "manual"
    return args


def main():
    from media_runtime import invalidate_profile, recover_profile
    args = parse_args()
    recover_profile(args, "machine")
    receiver = H264MachineReceiver(args)
    try:
        receiver.start()
    except (RuntimeError, GLib.Error):
        invalidate_profile(args.profile)
        receiver.stop()
        raise
    raise SystemExit(getattr(receiver, "codec_exit_code", 0))


if __name__ == "__main__":
    main()
