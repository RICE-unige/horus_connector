"""Validated source-video relay contracts and bounded H.264 delivery."""

from collections import deque
import json
from pathlib import Path
import re
import threading
import time


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError("relay config must be a JSON object")
    streams = config.get("streams")
    if not isinstance(streams, list) or not 1 <= len(streams) <= 32:
        raise ValueError("relay requires 1..32 explicitly configured camera streams")
    ids, outputs = set(), set()
    for stream in streams:
        if not isinstance(stream, dict):
            raise ValueError("each relay stream must be a JSON object")
        identifier = stream.get("id", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", identifier) or identifier in ids:
            raise ValueError("relay camera IDs must be unique ROS-safe names")
        ids.add(identifier)
        for field in ("input_topic", "output_topic"):
            if not re.fullmatch(r"/[A-Za-z0-9_/]+", str(stream.get(field, ""))):
                raise ValueError(f"{field} must be an absolute ROS topic")
        if stream["output_topic"] in outputs or stream["output_topic"] == stream["input_topic"]:
            raise ValueError("relay outputs must be unique and distinct from source topics")
        outputs.add(stream["output_topic"])
        for field, default, low, high in (("bitrate_kbps", 1500, 100, 100000), ("framerate", 15, 1, 60)):
            value = stream.setdefault(field, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid relay {field}")
        if stream.setdefault("image_type", "raw") not in {"raw", "compressed"}:
            raise ValueError("source image_type must be raw or compressed")
    if outputs.intersection(stream["input_topic"] for stream in streams):
        raise ValueError("relay outputs cannot overlap any source input")
    for field, default in (("client_signal_topic", "/horus/remote/webrtc/client_signal"),
                           ("server_signal_topic", "/horus/remote/webrtc/server_signal")):
        if not re.fullmatch(r"/[A-Za-z0-9_/]+", config.setdefault(field, default)):
            raise ValueError(f"invalid {field}")
    return config


def contains_idr(data):
    start = 0
    while (start := data.find(b"\x00\x00\x01", start)) >= 0:
        start += 3
        if start < len(data) and data[start] & 31 == 5:
            return True
    return False


class EncodedDelivery:
    def __init__(self, publish, request_keyframe, *, max_frames=32, max_bytes=4 * 1024 * 1024):
        if max_frames < 1 or max_bytes < 1:
            raise ValueError("encoded delivery capacities must be positive")
        self.publish, self.request_keyframe = publish, request_keyframe
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.pending, self.bytes = deque(), 0
        self.waiting_idr, self.closed = True, False
        self.received = self.published = self.dropped = self.errors = 0
        self.last_published = None
        self.condition = threading.Condition()
        self.worker = threading.Thread(target=self.run, name="encoded-camera-delivery", daemon=True)
        self.worker.start()

    def push(self, data):
        request = False
        with self.condition:
            if self.closed:
                return False
            self.received += 1
            if len(self.pending) >= self.max_frames or self.bytes + len(data) > self.max_bytes:
                self.dropped += len(self.pending)
                self.pending.clear()
                self.bytes = 0
                self.waiting_idr, request = True, True
            if len(data) > self.max_bytes or (self.waiting_idr and not contains_idr(data)):
                self.dropped += 1
                accepted = False
            else:
                self.waiting_idr = False
                self.pending.append(data)
                self.bytes += len(data)
                self.condition.notify_all()
                accepted = True
        if request:
            self.request_keyframe()
        return accepted

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed)
                if self.closed:
                    return
                data = self.pending.popleft()
                self.bytes -= len(data)
            try:
                self.publish(data)
                with self.condition:
                    self.published += 1
                    self.last_published = time.monotonic()
            except Exception:
                with self.condition:
                    self.errors += 1
                    self.dropped += len(self.pending) + 1
                    self.pending.clear()
                    self.bytes = 0
                    self.waiting_idr = True
                self.request_keyframe()

    def stats(self):
        with self.condition:
            age = None if self.last_published is None else time.monotonic() - self.last_published
            return dict(received=self.received, published=self.published, dropped=self.dropped,
                        errors=self.errors, pending=len(self.pending), pending_bytes=self.bytes,
                        waiting_idr=self.waiting_idr, last_frame_age_sec=age,
                        state="flowing" if age is not None and age < 3 else "stale" if age is not None else "waiting")

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.bytes = 0
            self.condition.notify_all()
        self.worker.join(timeout=3)
