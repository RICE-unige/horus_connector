#!/usr/bin/env python3
"""Shared helpers for the WebRTC camera test scripts."""

import asyncio
import json
import struct
from dataclasses import dataclass


FRAME_MAGIC = "zwc1"


@dataclass(frozen=True)
class CameraSpec:
    name: str
    width: int
    height: int
    fps: float
    jpeg_quality: int


def camera_specs(scale=1.0, quality_scale=1.0):
    def quality(value):
        return max(25, min(90, int(round(value * quality_scale))))

    return [
        CameraSpec("front", 1920, 1080, 10.0 * scale, quality(72)),
        CameraSpec("left", 1280, 720, 15.0 * scale, quality(68)),
        CameraSpec("rear", 1024, 768, 10.0 * scale, quality(64)),
    ]


def pack_frame(header, payload):
    header = dict(header)
    header["magic"] = FRAME_MAGIC
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(header_bytes)) + header_bytes + payload


def unpack_frame(message):
    if not isinstance(message, (bytes, bytearray)):
        return None, None
    if len(message) < 4:
        return None, None
    header_len = struct.unpack("!I", message[:4])[0]
    if header_len <= 0 or 4 + header_len > len(message):
        return None, None
    header = json.loads(message[4 : 4 + header_len].decode("utf-8"))
    if header.get("magic") != FRAME_MAGIC:
        return None, None
    return header, bytes(message[4 + header_len :])


def candidate_summary(sdp):
    candidates = []
    for line in sdp.splitlines():
        line = line.strip()
        if not line.startswith("a=candidate:"):
            continue
        parts = line[len("a=candidate:") :].split()
        if len(parts) >= 8:
            candidates.append(f"{parts[7]} {parts[2].lower()} {parts[4]}:{parts[5]}")
    return ", ".join(candidates) if candidates else "no candidates"


async def wait_for_ice_gathering_complete(pc, timeout=10.0):
    if pc.iceGatheringState == "complete":
        return
    complete = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_gathering_state_change():
        if pc.iceGatheringState == "complete":
            complete.set()

    await asyncio.wait_for(complete.wait(), timeout=timeout)
