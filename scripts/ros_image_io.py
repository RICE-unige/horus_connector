#!/usr/bin/env python3
"""ROS 2 Image helpers for the GStreamer WebRTC path."""

import threading
import time
from array import array
from typing import Optional, Tuple

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


ROS_TO_GST = {
    "rgb8": ("RGB", 3),
    "bgr8": ("BGR", 3),
    "rgba8": ("RGBA", 4),
    "bgra8": ("BGRA", 4),
    "mono8": ("GRAY8", 1),
    "8uc1": ("GRAY8", 1),
    "yuv422": ("YUY2", 2),
    "yuyv": ("YUY2", 2),
    "uyvy": ("UYVY", 2),
}

GST_TO_ROS = {
    "RGB": ("rgb8", 3),
    "BGR": ("bgr8", 3),
    "RGBA": ("rgba8", 4),
    "BGRA": ("bgra8", 4),
    "GRAY8": ("mono8", 1),
}


def ros_encoding_to_gst(encoding: str) -> Tuple[str, int]:
    key = (encoding or "").lower()
    if key not in ROS_TO_GST:
        raise ValueError(f"unsupported ROS image encoding for WebRTC: {encoding}")
    return ROS_TO_GST[key]


def gst_format_for_ros_encoding(encoding: str) -> Tuple[str, int]:
    gst_format, bytes_per_pixel = ros_encoding_to_gst(encoding)
    if gst_format not in GST_TO_ROS:
        raise ValueError(f"unsupported ROS output encoding for decoded WebRTC image: {encoding}")
    return gst_format, bytes_per_pixel


def qos_profile(qos_name: str):
    if qos_name == "default":
        return 10
    from rclpy.qos import qos_profile_sensor_data

    return qos_profile_sensor_data


def ensure_rclpy():
    import rclpy

    if not rclpy.ok():
        rclpy.init(args=None)
    return rclpy


def contiguous_image_bytes(data, step: int, expected_step: int, height: int) -> bytes:
    raw = memoryview(data)
    expected_size = expected_step * height
    if step == expected_step:
        return bytes(raw[:expected_size])

    rows = []
    for row in range(height):
        start = row * step
        rows.append(raw[start : start + expected_step])
    return b"".join(bytes(row) for row in rows)


class RosImageAppSrc:
    """Subscribes to sensor_msgs/Image and pushes raw frames into appsrc."""

    def __init__(self, appsrc, topic: str, fps: int, qos_name: str):
        self.appsrc = appsrc
        self.topic = topic
        self.fps = max(1, int(fps or 30))
        self.qos_name = qos_name
        self.rclpy = None
        self.node = None
        self.executor = None
        self.thread = None
        self.image_type = None
        self.seq = 0
        self.received_count = 0
        self.pushed_count = 0
        self.last_report = time.monotonic()
        self.caps_string = ""
        self.unsupported_encoding = ""

    def start(self):
        if not self.topic:
            raise RuntimeError("ROS image source selected, but no ROS image input topic was configured.")

        self.rclpy = ensure_rclpy()
        from rclpy.executors import SingleThreadedExecutor
        from sensor_msgs.msg import Image

        self.image_type = Image
        self.node = self.rclpy.create_node("horus_webrtc_image_source")
        self.node.create_subscription(Image, self.topic, self._on_image, qos_profile(self.qos_name))
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        self._configure_live_appsrc()
        print(f"Subscribing ROS images from {self.topic}", flush=True)

    def _configure_live_appsrc(self):
        properties = {
            "block": False,
            "max-buffers": 1,
            "max-bytes": 0,
            "max-time": 0,
            "leaky-type": 2,
        }
        for name, value in properties.items():
            if self.appsrc.find_property(name):
                try:
                    self.appsrc.set_property(name, value)
                except Exception:
                    pass

    def close(self):
        if self.executor is not None:
            self.executor.shutdown()
            self.executor = None
        if self.node is not None:
            self.node.destroy_node()
            self.node = None
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None

    def _on_image(self, msg):
        self.received_count += 1
        try:
            gst_format, bytes_per_pixel = ros_encoding_to_gst(msg.encoding)
        except ValueError as exc:
            if self.unsupported_encoding != msg.encoding:
                print(str(exc), flush=True)
                self.unsupported_encoding = msg.encoding
            return

        width = int(msg.width)
        height = int(msg.height)
        expected_step = width * bytes_per_pixel
        step = int(msg.step or expected_step)
        if step < expected_step:
            print(f"dropping malformed ROS image: step={step}, expected at least {expected_step}", flush=True)
            return

        payload = contiguous_image_bytes(msg.data, step, expected_step, height)
        caps = f"video/x-raw,format={gst_format},width={width},height={height},framerate={self.fps}/1"
        if caps != self.caps_string:
            self.appsrc.set_property("caps", Gst.Caps.from_string(caps))
            self.caps_string = caps
            print(f"ROS image appsrc caps: {caps}", flush=True)

        buffer = Gst.Buffer.new_allocate(None, len(payload), None)
        buffer.fill(0, payload)
        duration = Gst.util_uint64_scale_int(1, Gst.SECOND, self.fps)
        buffer.duration = duration
        self.seq += 1
        result = self.appsrc.emit("push-buffer", buffer)
        if result == Gst.FlowReturn.OK:
            self.pushed_count += 1
        elif result not in (Gst.FlowReturn.FLUSHING,):
            print(f"appsrc push-buffer returned {result}", flush=True)
        self._maybe_report_rate()

    def _maybe_report_rate(self):
        now = time.monotonic()
        elapsed = now - self.last_report
        if elapsed < 5.0:
            return
        print(
            "ROS image appsrc rate: "
            f"received={self.received_count / elapsed:.2f}fps "
            f"pushed={self.pushed_count / elapsed:.2f}fps",
            flush=True,
        )
        self.received_count = 0
        self.pushed_count = 0
        self.last_report = now


class RosImagePublisher:
    """Publishes decoded WebRTC frames as sensor_msgs/Image."""

    def __init__(self, topic: str, encoding: str, frame_id: str, qos_name: str):
        self.topic = topic
        self.encoding = encoding or "rgb8"
        self.frame_id = frame_id or "webrtc_camera"
        self.qos_name = qos_name
        self.rclpy = None
        self.node = None
        self.publisher = None
        self.image_type = None
        self.gst_format, self.bytes_per_pixel = gst_format_for_ros_encoding(self.encoding)

    def caps_filter(self) -> str:
        return f"video/x-raw,format={self.gst_format}"

    def start(self):
        if not self.topic:
            raise RuntimeError("ROS image output selected, but no ROS image output topic was configured.")

        self.rclpy = ensure_rclpy()
        from sensor_msgs.msg import Image

        self.image_type = Image
        self.node = self.rclpy.create_node("horus_webrtc_image_publisher")
        self.publisher = self.node.create_publisher(Image, self.topic, qos_profile(self.qos_name))
        print(f"Publishing decoded WebRTC images to {self.topic}", flush=True)

    def close(self):
        if self.node is not None:
            self.node.destroy_node()
            self.node = None

    def publish_sample(self, sample) -> bool:
        if self.publisher is None:
            return False
        caps = sample.get_caps()
        if caps is None or caps.get_size() == 0:
            return False
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        gst_format = structure.get_value("format")
        if gst_format != self.gst_format:
            print(f"dropping decoded frame with unexpected format {gst_format}", flush=True)
            return False

        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)

        msg = self.image_type()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.height = height
        msg.width = width
        msg.encoding = self.encoding
        msg.is_bigendian = 0
        msg.step = width * self.bytes_per_pixel
        msg.data = array("B", data)
        self.publisher.publish(msg)
        return True
