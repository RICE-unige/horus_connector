#!/usr/bin/env python3
"""ROS 2 Image helpers for the GStreamer WebRTC path."""

import threading
import time
import logging
import math
from array import array
from collections import deque, OrderedDict
from typing import Optional, Tuple
from media_health import RollingRate

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402

logger = logging.getLogger(__name__)


ROS_TO_GST = {
    "rgb8": ("RGB", 3),
    "bgr8": ("BGR", 3),
    "rgba8": ("RGBA", 4),
    "bgra8": ("BGRA", 4),
    "mono8": ("GRAY8", 1),
    "8uc1": ("GRAY8", 1),
    "yuv422": ("UYVY", 2),
    "yuv422_yuy2": ("YUY2", 2),
    "yuv422_uyvy": ("UYVY", 2),
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
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    return QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE if qos_name == "default"
                      else ReliabilityPolicy.BEST_EFFORT)


def subscription_qos_profiles(qos_name: str):
    return [qos_profile(qos_name)]


def camera_info_qos_profiles():
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    return [QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL)]


def ensure_rclpy():
    import rclpy

    if not rclpy.ok():
        rclpy.init(args=None)
    return rclpy


def contiguous_image_bytes(data, step: int, expected_step: int, height: int) -> bytes:
    raw = memoryview(data)
    if height <= 0 or expected_step <= 0 or step <= 0:
        raise ValueError("image dimensions and step must be positive")
    if step < expected_step:
        raise ValueError("image step cannot be smaller than a packed row")
    required_size = step * (height - 1) + expected_step
    if len(raw) < required_size:
        raise ValueError(f"image data too short: got {len(raw)} bytes, need at least {required_size}")
    expected_size = expected_step * height
    if step == expected_step:
        return bytes(raw[:expected_size])

    rows = []
    for row in range(height):
        start = row * step
        rows.append(raw[start : start + expected_step])
    return b"".join(bytes(row) for row in rows)


def packed_image_array(data, step, expected_step, height):
    raw = memoryview(data)
    if min(step, expected_step, height) <= 0 or step < expected_step or len(raw) < step * (height - 1) + expected_step:
        raise ValueError("invalid decoded image layout")
    result = array("B")
    if step == expected_step:
        result.frombytes(raw[:expected_step * height])
    else:
        for row in range(height):
            result.frombytes(raw[row * step:row * step + expected_step])
    return result


class RosImageAppSrc:
    """Subscribes to sensor_msgs/Image and pushes raw frames into appsrc."""

    def __init__(self, appsrc, topic: str, fps: int, qos_name: str, node_name: str = "horus_webrtc_image_source"):
        self.appsrc = appsrc
        self.topic = topic
        self.fps = max(1, int(fps or 30))
        self.qos_name = qos_name
        self.node_name = node_name
        self.rclpy = None
        self.node = None
        self.executor = None
        self.thread = None
        self.subscriptions = []
        self.image_type = None
        self.seq = 0
        self.received_count = 0
        self.pushed_count = 0
        self.total_received = 0
        self.total_pushed = 0
        self.last_report = time.monotonic()
        self.caps_string = ""
        self.unsupported_encoding = ""
        self.last_message_key = None
        self.recent_message_keys = deque(maxlen=32)
        self.deduplicate_qos_messages = False
        self.last_push_sec = -float("inf")
        self.next_frame_sec = 0.0
        self.rate_dropped = 0
        self.closed = False
        self.source_metadata = OrderedDict()
        self.metadata_lock = threading.Lock()
        self.camera_info_topic = ""
        self.camera_info = None
        self.output_size = None
        self.resize_mode = "letterbox"
        self.scale_caps = None
        self.padding = None
        self.latched_camera_info_subscription = False

    def start(self):
        if not self.topic:
            raise RuntimeError("ROS image source selected, but no ROS image input topic was configured.")

        self.rclpy = ensure_rclpy()
        from rclpy.executors import SingleThreadedExecutor
        from sensor_msgs.msg import Image

        self.image_type = Image
        self.node = self.rclpy.create_node(self.node_name)
        for profile in subscription_qos_profiles(self.qos_name):
            self.subscriptions.append(self.node.create_subscription(Image, self.topic, self._on_image, profile))
        if self.camera_info_topic:
            from sensor_msgs.msg import CameraInfo
            self.subscriptions.append(self.node.create_subscription(CameraInfo, self.camera_info_topic,
                                      self._on_camera_info, camera_info_qos_profiles()[0]))
            self.node.create_timer(.5, self._refresh_camera_info_qos)
        self._configure_live_appsrc()
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self._spin_ros, args=(self.executor, self.node), daemon=True)
        self.thread.start()
        print(f"Subscribing ROS images from {self.topic} with {self.qos_name} QoS", flush=True)

    def _refresh_camera_info_qos(self):
        if self.closed or self.latched_camera_info_subscription:
            return
        from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
        from sensor_msgs.msg import CameraInfo
        endpoints = self.node.get_publishers_info_by_topic(self.camera_info_topic)
        if any(endpoint.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for endpoint in endpoints):
            qos = camera_info_qos_profiles()[1]
            qos.reliability = ReliabilityPolicy.RELIABLE if any(
                endpoint.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL and
                endpoint.qos_profile.reliability == ReliabilityPolicy.RELIABLE for endpoint in endpoints) else ReliabilityPolicy.BEST_EFFORT
            self.subscriptions.append(self.node.create_subscription(CameraInfo, self.camera_info_topic,
                                      self._on_camera_info, qos))
            self.latched_camera_info_subscription = True

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
                    logger.debug("Failed to configure appsrc property %s=%r", name, value, exc_info=True)

    def close(self):
        self.closed = True
        executor = self.executor
        if executor is not None:
            executor.wake()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            if self.thread.is_alive():
                logger.warning("ROS image callback is still stopping; node cleanup is deferred until it returns")
            else:
                self.thread = None

    def _spin_ros(self, executor, node):
        from rclpy.executors import ExternalShutdownException, ShutdownException
        try:
            while not self.closed and self.rclpy.ok():
                executor.spin_once(timeout_sec=.1)
        except (ExternalShutdownException, ShutdownException):
            logger.debug("ROS camera context stopped")
        finally:
            executor.shutdown()
            node.destroy_node()
            if self.executor is executor:
                self.executor = None
            if self.node is node:
                self.node = None

    def _on_image(self, msg):
        if self.closed:
            return
        if self.deduplicate_qos_messages and self._is_duplicate_qos_message(msg):
            return
        self.received_count += 1
        self.total_received += 1
        now = time.monotonic()
        if now < self.next_frame_sec:
            self.rate_dropped += 1
            return
        try:
            gst_format, bytes_per_pixel = ros_encoding_to_gst(msg.encoding)
        except ValueError as exc:
            if self.unsupported_encoding != msg.encoding:
                print(str(exc), flush=True)
                self.unsupported_encoding = msg.encoding
            return

        width = int(msg.width)
        height = int(msg.height)
        if gst_format in {"UYVY", "YUY2"} and width % 2:
            logger.warning("Dropping packed YUV frame with odd width")
            return
        if width <= 0 or height <= 0:
            print(f"dropping malformed ROS image: width={width}, height={height}", flush=True)
            return
        expected_step = width * bytes_per_pixel
        step = int(msg.step or expected_step)
        if step < expected_step:
            print(f"dropping malformed ROS image: step={step}, expected at least {expected_step}", flush=True)
            return

        try:
            payload = contiguous_image_bytes(msg.data, step, expected_step, height)
        except ValueError as exc:
            print(f"dropping malformed ROS image: {exc}", flush=True)
            return
        caps = f"video/x-raw,format={gst_format},width={width},height={height},pixel-aspect-ratio=1/1,framerate={self.fps}/1"
        if caps != self.caps_string:
            if self.output_size and self.scale_caps is not None and self.padding is not None:
                transform = pixel_transform(width, height, *self.output_size, self.resize_mode)
                target_width, target_height = self.output_size
                self.scale_caps.set_property("caps", Gst.Caps.from_string(
                    f"video/x-raw,width={transform['content_width']},height={transform['content_height']},pixel-aspect-ratio=1/1"))
                x, y = transform["offset_x"], transform["offset_y"]
                for name, value in (("left", -x), ("top", -y),
                                    ("right", -(target_width - transform["content_width"] - x)),
                                    ("bottom", -(target_height - transform["content_height"] - y))):
                    self.padding.set_property(name, value)
            self.appsrc.set_property("caps", Gst.Caps.from_string(caps))
            self.caps_string = caps
            print(f"ROS image appsrc caps: {caps}", flush=True)

        buffer = Gst.Buffer.new_wrapped(payload)
        duration = Gst.util_uint64_scale_int(1, Gst.SECOND, self.fps)
        buffer.duration = duration
        clock = self.appsrc.get_clock()
        if clock is not None:
            buffer.pts = max(0, clock.get_time() - self.appsrc.get_base_time())
        self.seq += 1
        stamp = msg.header.stamp
        metadata = {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec),
                    "frame_id": msg.header.frame_id, "sequence": self.seq,
                    "width": width, "height": height, "clock_domain": "ros_source"}
        if self.camera_info is not None and self.camera_info["width"] == width and self.camera_info["height"] == height:
            metadata["camera_info"] = self.camera_info
        if self.output_size:
            metadata["pixel_transform"] = pixel_transform(width, height, *self.output_size, self.resize_mode)
        with self.metadata_lock:
            self.source_metadata[int(buffer.pts)] = metadata
            while len(self.source_metadata) > 120:
                self.source_metadata.popitem(last=False)
        if self.closed:
            return
        result = self.appsrc.emit("push-buffer", buffer)
        if result == Gst.FlowReturn.OK:
            self.last_push_sec = now
            interval = 1.0 / self.fps
            self.next_frame_sec = now + interval if self.next_frame_sec < now - interval else self.next_frame_sec + interval
            self.pushed_count += 1
            self.total_pushed += 1
        elif result not in (Gst.FlowReturn.FLUSHING,):
            print(f"appsrc push-buffer returned {result}", flush=True)
        self._maybe_report_rate()

    def metadata_for_running_time(self, pts):
        with self.metadata_lock:
            return self.source_metadata.pop(pts, None)

    def _on_camera_info(self, msg):
        if self.closed:
            return
        try:
            self.camera_info = normalize_calibration(msg)
        except (AttributeError, TypeError, ValueError, OverflowError):
            self.camera_info = None

    def _is_duplicate_qos_message(self, msg) -> bool:
        stamp = getattr(msg.header, "stamp", None)
        if stamp is None or (int(stamp.sec) == 0 and int(stamp.nanosec) == 0):
            return False
        key = (int(stamp.sec), int(stamp.nanosec), int(msg.width), int(msg.height), int(msg.step))
        if key in self.recent_message_keys:
            return True
        self.last_message_key = key
        self.recent_message_keys.append(key)
        return False

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

    def __init__(
        self,
        topic: str,
        encoding: str,
        frame_id: str,
        qos_name: str,
        node_name: str = "horus_webrtc_image_publisher",
    ):
        self.topic = topic
        self.encoding = encoding or "rgb8"
        self.frame_id = frame_id or "webrtc_camera"
        self.qos_name = qos_name
        self.node_name = node_name
        self.rclpy = None
        self.node = None
        self.publisher = None
        self.image_type = None
        self.gst_format, self.bytes_per_pixel = gst_format_for_ros_encoding(self.encoding)
        self._publish_lock = threading.Lock()
        self.camera_info_topic = ""
        self.camera_info_publisher = None
        self.preserve_source_header = False
        self.metadata_missing_frames = 0
        self.published_frames = 0
        self.last_published_sec = None
        self.active_delivery_epoch = 0
        self.last_published_epoch = None
        self.published_rate = RollingRate()
        self.camera_info_frames = 0

    def caps_filter(self) -> str:
        return f"video/x-raw,format={self.gst_format}"

    def start(self):
        if not self.topic:
            raise RuntimeError("ROS image output selected, but no ROS image output topic was configured.")

        self.rclpy = ensure_rclpy()
        from sensor_msgs.msg import Image

        self.image_type = Image
        self.node = self.rclpy.create_node(self.node_name)
        self.publisher = self.node.create_publisher(Image, self.topic, qos_profile(self.qos_name))
        if self.camera_info_topic:
            from sensor_msgs.msg import CameraInfo
            self.camera_info_publisher = self.node.create_publisher(CameraInfo, self.camera_info_topic, qos_profile(self.qos_name))
        print(f"Publishing decoded WebRTC images to {self.topic}", flush=True)

    def close(self):
        with self._publish_lock:
            self.publisher = None
            self.camera_info_publisher = None
            if self.node is not None:
                self.node.destroy_node()
                self.node = None

    def publish_sample(self, sample, source_metadata=None, delivery_epoch=None) -> bool:
        # A streaming callback may still be returning while the pipeline closes.
        with self._publish_lock:
            if delivery_epoch is not None and delivery_epoch != self.active_delivery_epoch:
                return False
            return self._publish_sample(sample, source_metadata, delivery_epoch)

    def _publish_sample(self, sample, source_metadata=None, delivery_epoch=None) -> bool:
        epoch = self.active_delivery_epoch if delivery_epoch is None else delivery_epoch
        if self.publisher is None or self.node is None:
            return False
        caps = sample.get_caps()
        if caps is None or caps.get_size() == 0:
            return False
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        if width <= 0 or height <= 0:
            print(f"dropping decoded frame with invalid dimensions {width}x{height}", flush=True)
            return False
        gst_format = structure.get_value("format")
        if gst_format != self.gst_format:
            print(f"dropping decoded frame with unexpected format {gst_format}", flush=True)
            return False

        buffer = sample.get_buffer()
        if buffer is None:
            return False
        video_info = GstVideo.VideoInfo.new_from_caps(caps)
        video_meta = GstVideo.buffer_get_video_meta(buffer)
        stride = int(video_meta.stride[0] if video_meta else video_info.stride[0])
        offset = int(video_meta.offset[0] if video_meta else video_info.offset[0])
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            if offset < 0:
                raise ValueError("negative decoded frame offset")
            data = packed_image_array(
                memoryview(info.data)[offset:], stride, width * self.bytes_per_pixel, height
            )
        except ValueError as exc:
            print(f"dropping malformed decoded frame: {exc}", flush=True)
            return False
        finally:
            buffer.unmap(info)

        msg = self.image_type()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        if self.preserve_source_header:
            try:
                sec, nanosec, frame_id = source_header(source_metadata)
            except (ValueError, TypeError, KeyError):
                self.metadata_missing_frames += 1
                return False
            msg.header.stamp.sec = sec
            msg.header.stamp.nanosec = nanosec
            msg.header.frame_id = frame_id
        msg.height = height
        msg.width = width
        msg.encoding = self.encoding
        msg.is_bigendian = 0
        msg.step = width * self.bytes_per_pixel
        msg.data = data
        if epoch != self.active_delivery_epoch:
            return False
        self.publisher.publish(msg)
        self.published_frames += 1
        self.last_published_sec = time.monotonic()
        self.last_published_epoch = epoch
        self.published_rate.add(self.last_published_sec)
        if isinstance(source_metadata, dict) and source_metadata.get("camera_info") and getattr(self, "camera_info_publisher", None):
            calibration = source_metadata["camera_info"]
            try:
                if (not isinstance(calibration["distortion_model"], str) or len(calibration["r"]) != 9
                        or calibration["width"] <= 0 or calibration["height"] <= 0
                        or not all(math.isfinite(value) for value in (*calibration["d"], *calibration["r"]))):
                    return True
                transform = source_metadata.get("pixel_transform")
                if transform is None:
                    if (width, height) != (calibration["width"], calibration["height"]):
                        return True
                    transform = pixel_transform(width, height, width, height)
                if (transform["width"], transform["height"]) != (width, height):
                    return True
                k = transform_intrinsics(calibration["k"], 3, transform)
                p = transform_intrinsics(calibration["p"], 4, transform)
            except (KeyError, TypeError, ValueError, OverflowError):
                return True
            from sensor_msgs.msg import CameraInfo
            info_msg = CameraInfo()
            info_msg.header = msg.header
            info_msg.width, info_msg.height = width, height
            info_msg.distortion_model = calibration["distortion_model"]
            info_msg.d, info_msg.r = calibration["d"], calibration["r"]
            info_msg.k, info_msg.p = k, p
            self.camera_info_publisher.publish(info_msg)
            self.camera_info_frames += 1
        return True


def source_header(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("missing source header")
    sec, nanosec, frame_id = metadata["sec"], metadata["nanosec"], metadata["frame_id"]
    if (type(sec) is not int or not -(2**31) <= sec < 2**31 or type(nanosec) is not int
            or not 0 <= nanosec < 10**9 or not isinstance(frame_id, str)):
        raise ValueError("invalid ROS source header")
    return sec, nanosec, frame_id


def scale_intrinsics(matrix, columns, x_scale, y_scale):
    if len(matrix) != columns * 3 or not all(math.isfinite(value) for value in (*matrix, x_scale, y_scale)):
        raise ValueError("invalid full-frame camera calibration")
    result = list(matrix)
    for index in range(columns):
        result[index] *= x_scale
        result[columns + index] *= y_scale
    return result


def pixel_transform(source_width, source_height, width, height, mode="letterbox"):
    if min(source_width, source_height, width, height) <= 0 or mode not in {"letterbox", "stretch"}:
        raise ValueError("invalid calibrated resize")
    sx, sy = width / source_width, height / source_height
    if mode == "letterbox":
        scale = min(sx, sy)
        # Chroma-aligned content dimensions also make the padding deterministic.
        content_width = max(2, min(width, int(source_width * scale) // 2 * 2))
        content_height = max(2, min(height, int(source_height * scale) // 2 * 2))
        sx, sy = content_width / source_width, content_height / source_height
        x, y = (width - content_width) // 2, (height - content_height) // 2
    else:
        content_width, content_height, x, y = width, height, 0, 0
    return {"scale_x": sx, "scale_y": sy, "offset_x": x, "offset_y": y,
            "width": width, "height": height, "content_width": content_width,
            "content_height": content_height, "mode": mode}


def transform_intrinsics(matrix, columns, transform):
    sx, sy, x, y = (transform[key] for key in ("scale_x", "scale_y", "offset_x", "offset_y"))
    if not all(math.isfinite(value) for value in (sx, sy, x, y)) or min(sx, sy) <= 0:
        raise ValueError("invalid pixel transform")
    result = scale_intrinsics(matrix, columns, sx, sy)
    for col in range(columns):
        result[col] += x * matrix[2 * columns + col]
        result[columns + col] += y * matrix[2 * columns + col]
    return result


def normalize_calibration(msg):
    width, height = int(msg.width), int(msg.height)
    bx, by = max(1, int(msg.binning_x)), max(1, int(msg.binning_y))
    x, y = int(msg.roi.x_offset), int(msg.roi.y_offset)
    crop_width, crop_height = int(msg.roi.width) or width, int(msg.roi.height) or height
    if (min(width, height, crop_width, crop_height) <= 0 or min(x, y) < 0
            or x + crop_width > width or y + crop_height > height
            or crop_width % bx or crop_height % by or len(msg.r) != 9
            or not all(math.isfinite(v) for v in (*msg.d, *msg.r))):
        raise ValueError("invalid source camera calibration")
    transform = {"scale_x": 1 / bx, "scale_y": 1 / by, "offset_x": -x / bx, "offset_y": -y / by}
    return {"width": crop_width // bx, "height": crop_height // by,
            "distortion_model": msg.distortion_model, "d": list(msg.d), "r": list(msg.r),
            "k": transform_intrinsics(msg.k, 3, transform), "p": transform_intrinsics(msg.p, 4, transform)}
