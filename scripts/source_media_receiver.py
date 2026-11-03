#!/usr/bin/env python3
"""Relay source WebRTC access units locally, without a video decoder or encoder."""

import argparse
from array import array
import json
import os
import signal
import time
import uuid

import gi
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from encoded_media import EncodedDelivery, load_config
from gst_webrtc_common import (Gst, GstWebRTC, configure_webrtcbin,
                               ensure_webrtc_runtime, make_session_description)
from media_health import RecoveryBackoff, recycle_media_process, write_status

gi.require_version("GLib", "2.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, GstVideo


class Camera:
    def __init__(self, owner, config):
        self.owner, self.config = owner, config
        self.pipeline = self.webrtc = self.sink = self.bus = None
        self.handlers = []
        self.session_id = self.offer_sdp = self.answer_sdp = ""
        self.candidates, self.mid = [], "0"
        self.started = self.last_offer = self.last_keyframe = self.idle_since = 0
        self.retry_at = 0
        self.backoff = RecoveryBackoff()
        self.generation = 0
        self.connected = False
        self.publisher = owner.node.create_publisher(CompressedImage, config["output_topic"],
            QoSProfile(depth=32, reliability=ReliabilityPolicy.RELIABLE))
        self.keyframes = owner.node.create_subscription(String, config["output_topic"] + "/request_keyframe",
            lambda _: self.request_keyframe(), 10)
        self.delivery = None

    def send(self, **payload):
        self.owner.signal.publish(String(data=json.dumps(dict(session_id=self.session_id, **payload))))

    def connect(self, element, name, callback, *values):
        self.handlers.append((element, element.connect(name, callback, *values)))

    def start(self):
        self.generation += 1
        generation = self.generation
        self.session_id = "source_relay_" + uuid.uuid4().hex
        self.offer_sdp = self.answer_sdp = ""
        self.candidates, self.mid = [], "0"
        self.started, self.connected = time.monotonic(), False
        self.pipeline = Gst.Pipeline.new(None)
        self.webrtc = Gst.ElementFactory.make("webrtcbin")
        if self.webrtc is None:
            raise RuntimeError("webrtcbin is unavailable")
        configure_webrtcbin(self.webrtc, "", "all")
        self.pipeline.add(self.webrtc)
        self.connect(self.webrtc, "pad-added", self.incoming, generation)
        self.connect(self.webrtc, "on-ice-candidate", self.candidate, generation)
        self.connect(self.webrtc, "notify::connection-state", self.connection_state, generation)
        caps = Gst.Caps.from_string("application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96")
        self.webrtc.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, caps)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.connect(self.bus, "message", self.bus_message, generation)
        self.delivery = EncodedDelivery(self.publish, self.request_keyframe)
        self.pipeline.set_state(Gst.State.PLAYING)
        promise = Gst.Promise.new_with_change_func(self.offer_created, (generation, self.webrtc))
        self.webrtc.emit("create-offer", None, promise)
        print(f"Encoded relay {self.config['id']}: negotiating source WebRTC", flush=True)

    def offer_created(self, promise, context):
        generation, element = context
        if generation != self.generation or element is not self.webrtc:
            return
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply else None
        if offer is None:
            GLib.idle_add(self.recover, "offer creation failed")
            return
        # GstSDPMedia borrows its parent message: keep that boxed owner alive.
        sdp = offer.sdp
        media = sdp.get_media(0) if sdp.medias_len() else None
        self.mid = (media.get_attribute_val("mid") if media is not None else None) or "0"
        self.offer_sdp = sdp.as_text()
        self.apply_description("local", offer, generation, element)

    def apply_description(self, side, description, generation, element):
        promise = Gst.Promise.new_with_change_func(self.description_applied,
                                                   (generation, element, side))
        element.emit(f"set-{side}-description", description, promise)

    def description_applied(self, promise, context):
        generation, element, side = context
        if generation != self.generation or element is not self.webrtc:
            return
        reply = promise.get_reply()
        error = reply.get_value("error") if reply is not None and reply.has_field("error") else None
        if error:
            GLib.idle_add(self.description_failed, generation, f"{side} SDP rejected: {error}")

    def description_failed(self, generation, reason):
        if generation == self.generation:
            self.recover(reason)
        return False

    def candidate(self, element, line, candidate, generation):
        if generation == self.generation and element is self.webrtc:
            self.candidates.append(dict(type="candidate", candidate=candidate,
                                       sdpMid=self.mid, sdpMLineIndex=int(line)))

    def connection_state(self, element, _property, generation):
        if generation != self.generation or element is not self.webrtc:
            return
        state = element.get_property("connection-state")
        self.connected = state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED
        if self.connected:
            self.backoff.reset()
        elif state == GstWebRTC.WebRTCPeerConnectionState.FAILED:
            GLib.idle_add(self.recover, "ICE/peer failed")

    def incoming(self, element, pad, generation):
        if generation != self.generation or element is not self.webrtc or self.sink is not None:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or not caps.to_string().startswith("application/x-rtp"):
            return
        media = Gst.parse_bin_from_description(
            "rtph264depay request-keyframe=true wait-for-keyframe=true ! "
            "h264parse config-interval=-1 disable-passthrough=true ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=encoded emit-signals=true sync=false max-buffers=2 drop=false", True)
        sink = media.get_by_name("encoded")
        self.sink = sink
        self.connect(sink, "new-sample", self.sample, generation)
        self.pipeline.add(media)
        media.sync_state_with_parent()
        if pad.link(media.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            GLib.idle_add(self.recover, "RTP depayloader link failed")

    def sample(self, sink, generation):
        sample = sink.emit("pull-sample")
        if sample is None or generation != self.generation or self.delivery is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        success, mapping = buffer.map(Gst.MapFlags.READ)
        if success:
            try:
                self.delivery.push(bytes(mapping.data))
            finally:
                buffer.unmap(mapping)
        return Gst.FlowReturn.OK

    def publish(self, data):
        message = CompressedImage()
        message.header.stamp = self.owner.node.get_clock().now().to_msg()
        message.header.frame_id = self.config.get("frame_id", "")
        message.format, message.data = "h264", array("B", data)
        self.publisher.publish(message)

    def request_keyframe(self):
        GLib.idle_add(self.keyframe_in_loop)

    def keyframe_in_loop(self):
        if self.sink is not None and time.monotonic() - self.last_keyframe > .5:
            self.last_keyframe = time.monotonic()
            self.sink.send_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))
        return False

    def receive(self, message):
        if self.webrtc is None:
            return
        kind = message.get("type")
        if kind == "answer":
            sdp = message.get("sdp", "")
            if sdp and sdp != self.answer_sdp:
                self.answer_sdp = sdp
                self.apply_description("remote", make_session_description("answer", sdp),
                                       self.generation, self.webrtc)
        elif kind == "candidate" and message.get("candidate"):
            self.webrtc.emit("add-ice-candidate", int(message.get("sdpMLineIndex", 0)), message["candidate"])
        elif kind == "error":
            self.recover(message.get("error", "source error"))

    def bus_message(self, _bus, message, generation):
        if generation == self.generation and message.type == Gst.MessageType.ERROR:
            error, _detail = message.parse_error()
            self.recover(str(error))

    def tick(self, now):
        demanded = self.publisher.get_subscription_count() > 0
        if not demanded:
            self.idle_since = self.idle_since or now
            if self.webrtc is not None and now - self.idle_since > 3:
                self.close()
            return
        self.idle_since = 0
        if self.webrtc is None:
            if now >= self.retry_at:
                self.start()
            return
        if not self.offer_sdp:
            if self.webrtc.get_property("ice-gathering-state") == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
                local = self.webrtc.get_property("local-description")
                if local:
                    self.offer_sdp = local.sdp.as_text()
        if self.offer_sdp and not self.answer_sdp and now - self.last_offer > 1:
            self.last_offer = now
            self.send(type="offer", sdp=self.offer_sdp, stream_topic=self.config["input_topic"],
                image_type=self.config["image_type"], bitrate_kbps=self.config["bitrate_kbps"],
                framerate=self.config["framerate"], stun_servers=[], wait_for_complete_gathering=True)
        if self.offer_sdp and self.last_offer >= self.started:
            while self.candidates:
                self.send(**self.candidates.pop(0))
        if not self.connected and now - self.started > 15:
            self.recover("source negotiation timeout")
        elif self.delivery is not None:
            age = self.delivery.stats()["last_frame_age_sec"]
            if age is None or age > 2.5:
                self.request_keyframe()

    def recover(self, reason):
        if self.owner.stopped:
            return False
        print(f"Encoded relay {self.config['id']}: {reason}; reconnecting", flush=True)
        if recycle_media_process(self.owner):
            return False
        self.close()
        self.retry_at = time.monotonic() + self.backoff.next_delay()
        return False

    def close(self):
        self.generation += 1
        if self.session_id:
            self.send(type="stop")
        self.session_id = ""
        self.offer_sdp = self.answer_sdp = ""
        self.candidates.clear()
        self.connected = False
        if self.delivery is not None:
            self.delivery.close()
            self.delivery = None
        for element, handler in self.handlers:
            element.disconnect(handler)
        self.handlers.clear()
        if self.bus is not None:
            self.bus.remove_signal_watch()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = self.webrtc = self.sink = self.bus = None


class Relay:
    def __init__(self, config, status):
        self.node = rclpy.create_node("horus_source_media_receiver")
        self.signal = self.node.create_publisher(String, config["client_signal_topic"], 10)
        self.subscription = self.node.create_subscription(String, config["server_signal_topic"], self.receive, 100)
        self.cameras = [Camera(self, stream) for stream in config["streams"]]
        self.loop, self.stopped, self.status = GLib.MainLoop(), False, status
        self.codec_exit_code = 0
        self.sources = [GLib.timeout_add(2, self.spin), GLib.timeout_add(250, self.tick)]

    def receive(self, message):
        try:
            value = json.loads(message.data)
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        for camera in self.cameras:
            if value.get("session_id") == camera.session_id and camera.session_id:
                try:
                    camera.receive(value)
                except (ValueError, TypeError, RuntimeError) as exc:
                    camera.recover(f"invalid source signal: {exc}")

    def spin(self):
        if self.stopped:
            return False
        rclpy.spin_once(self.node, timeout_sec=0)
        return True

    def tick(self):
        if self.stopped:
            return False
        now = time.monotonic()
        for camera in self.cameras:
            camera.tick(now)
        tracks = {camera.config["id"]: {
            **(camera.delivery.stats() if camera.delivery else {"state": "idle", "published": 0}),
            "subscribers": camera.publisher.get_subscription_count(),
            "peer_connected": camera.connected, "offer_ready": bool(camera.offer_sdp),
            "answer_received": bool(camera.answer_sdp)} for camera in self.cameras}
        write_status(self.status, dict(stage="encoded_local_ros", tracks=tracks,
            signaling_subscribers=self.signal.get_subscription_count(),
            quality="unchanged_H264_access_units", timing="local_relay_receipt_not_source_capture"))
        return True

    def stop(self):
        if self.stopped:
            return False
        self.stopped = True
        for source in self.sources:
            GLib.source_remove(source)
        self.sources.clear()
        for camera in self.cameras:
            camera.close()
        if self.loop.is_running():
            self.loop.quit()
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--status", default=os.environ.get("HORUS_MEDIA_STATUS_FILE"))
    args = parser.parse_args()
    config = load_config(args.config)
    Gst.init(None)
    ensure_webrtc_runtime()
    rclpy.init()
    relay = Relay(config, args.status)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: GLib.idle_add(relay.stop))
    try:
        relay.loop.run()
    finally:
        relay.stop()
        relay.node.destroy_node()
        rclpy.shutdown()
    return relay.codec_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
