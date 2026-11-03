"""Bounded media telemetry and exact RTP-frame correlation."""

import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

_status_errors = set()


def write_status(path, payload):
    if not path:
        return
    target = Path(path)
    temporary = target.with_name(target.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({"updated_wall_sec": time.time(), "pid": os.getpid(), **payload}), encoding="utf-8")
        temporary.replace(target)
        _status_errors.discard(str(target))
    except OSError as exc:
        if str(target) not in _status_errors:
            logging.getLogger(__name__).warning("Could not save media health to %s: %s", target, exc)
            _status_errors.add(str(target))
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class RecoveryBackoff:
    def __init__(self, initial_sec=2, maximum_sec=30):
        self.initial_sec = initial_sec
        self.maximum_sec = maximum_sec
        self.attempts = 0

    def next_delay(self):
        delay = min(self.maximum_sec, self.initial_sec * 2 ** min(self.attempts, 10))
        self.attempts += 1
        return delay

    def reset(self):
        self.attempts = 0


def recycle_media_process(owner):
    """Bound native ICE/codec lifetime only when an owned supervisor can restart us."""
    if os.environ.get("HORUS_MEDIA_SUPERVISED") != "1":
        return False
    limit = max(1, int(os.environ.get("WEBRTC_MAX_PIPELINE_REBUILDS", "4")))
    owner.process_pipeline_rebuilds = getattr(owner, "process_pipeline_rebuilds", 0) + 1
    if owner.process_pipeline_rebuilds < limit:
        return False
    print(f"Recycling owned media process after {limit} pipeline rebuilds; supervisor will reconnect", flush=True)
    owner.codec_exit_code = 75
    owner.stop()
    return True


class FrameMatcher:
    """Match metadata by RTP timestamp and decoded PTS, never arrival order."""

    def __init__(self, capacity=600, ttl_sec=5):
        self.capacity = capacity
        self.ttl_sec = ttl_sec
        self.packets = OrderedDict()
        self.markers = OrderedDict()
        self.outputs = OrderedDict()
        self.sources = OrderedDict()
        self.lock = threading.Lock()

    def _put(self, table, key, value):
        now = time.monotonic()
        table[key] = (now, value)
        table.move_to_end(key)
        while table and (len(table) > self.capacity or now - next(iter(table.values()))[0] > self.ttl_sec):
            table.popitem(last=False)

    def packet(self, pts, rtp_timestamp):
        with self.lock:
            self._put(self.packets, pts, rtp_timestamp)

    def marker(self, message):
        with self.lock:
            key = int(message["rtp_timestamp"])
            self._put(self.markers, key, message)
            self._put(self.sources, key, message.get("source"))
            output = self.outputs.pop(key, None)
            if output is not None and time.monotonic() - output[0] <= self.ttl_sec:
                self.markers.pop(key, None)
                return message, output[1]
        return None

    def decoded(self, pts, output):
        with self.lock:
            packet = self.packets.get(pts)
            if packet is None or time.monotonic() - packet[0] > self.ttl_sec:
                return None
            key = packet[1]
            marker = self.markers.pop(key, None)
            if marker is not None and time.monotonic() - marker[0] <= self.ttl_sec:
                return marker[1], output
            self._put(self.outputs, key, output)
        return None

    def metadata_for_pts(self, pts):
        with self.lock:
            packet = self.packets.get(pts)
            if packet is None or time.monotonic() - packet[0] > self.ttl_sec:
                return None
            source = self.sources.get(packet[1])
            return source[1] if source is not None and time.monotonic() - source[0] <= self.ttl_sec else None


class RollingRate:
    def __init__(self, window_sec=5, capacity=4096):
        self.window_sec = window_sec
        self.samples = deque(maxlen=capacity)
        self.lock = threading.Lock()

    def add(self, now=None):
        with self.lock:
            self.samples.append(time.monotonic() if now is None else now)

    def rate(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            while self.samples and self.samples[0] < now - self.window_sec:
                self.samples.popleft()
            return len(self.samples) / self.window_sec


def allocate_bitrates(streams, target):
    """Integer weighted allocation respecting per-track and session budgets."""
    lower = [int(stream["min_bitrate_kbit"]) for stream in streams]
    upper = [int(stream["max_bitrate_kbit"]) for stream in streams]
    if int(target) < sum(lower):
        raise ValueError(f"session bitrate {target} is below mandatory track minima {sum(lower)}")
    total = min(int(target), sum(upper))
    result = list(lower)
    remaining = total - sum(result)
    while remaining:
        active = [index for index in range(len(streams)) if result[index] < upper[index]]
        weights = sum(max(1, streams[index].get("priority", 1)) * streams[index]["bitrate_kbit"] for index in active)
        budget = remaining
        for index in active:
            weight = max(1, streams[index].get("priority", 1)) * streams[index]["bitrate_kbit"]
            share = max(1, int(budget * weight / weights))
            grant = min(share, upper[index] - result[index], remaining)
            result[index] += grant
            remaining -= grant
            if not remaining:
                break
    return result
