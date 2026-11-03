#!/usr/bin/env python3
"""Load HORUS WebRTC camera stream configuration."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from contextvars import ContextVar

_environment = ContextVar("stream_environment", default=None)


def env_value(name: str, default: str = "") -> str:
    environment = _environment.get()
    return (os.environ if environment is None else environment).get(name, default)


def configuration(root, role, environment):
    token = _environment.set(environment)
    try:
        return load_session(root, role), load_streams(root, role)
    finally:
        _environment.reset(token)


def ros_safe_name(value: str, fallback: str = "stream") -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def stream_id(value: str, fallback: str = "primary") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def is_topic(value: str) -> bool:
    return bool(re.match(r"^/[A-Za-z0-9_/]+$", value or ""))


def stream_output_topic(room: str, identifier: str) -> str:
    room_token = ros_safe_name(room, "robot")
    stream_token = ros_safe_name(identifier, "primary")
    if stream_token == "primary":
        return f"/{room_token}/camera/webrtc/image_raw"
    return f"/{room_token}/camera/{stream_token}/webrtc/image_raw"


def default_stream(role: str) -> dict[str, Any]:
    room = env_value("HORUS_ROOM", "default")
    identifier = "primary"
    output_topic = env_value("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC")
    if not output_topic:
        output_topic = stream_output_topic(room, identifier)
    return {
        "id": identifier,
        "label": "primary",
        "room": room,
        "enabled": True,
        "input_topic": env_value("WEBRTC_ROS_IMAGE_INPUT_TOPIC", env_value("WEBRTC_ROS_IMAGE_TOPIC", "/camera/image_raw")),
        "output_topic": output_topic,
        "width": int(env_value("WEBRTC_VIDEO_WIDTH", "1280") or 1280),
        "height": int(env_value("WEBRTC_VIDEO_HEIGHT", "720") or 720),
        "fps": int(env_value("WEBRTC_VIDEO_FPS", "30") or 30),
        "bitrate_kbit": int(env_value("VIDEO_BITRATE_KBIT", "6000") or 6000),
        "video_source": env_value("WEBRTC_VIDEO_SOURCE", "ros2"),
        "source_pipeline": env_value("WEBRTC_GST_VIDEO_SOURCE", ""),
        "ros_image_qos": env_value("WEBRTC_ROS_IMAGE_QOS", "auto"),
        "frame_id": env_value("WEBRTC_ROS_IMAGE_FRAME_ID", "webrtc_camera"),
        "role": role,
    }


def config_path(root: Path) -> Path:
    configured = env_value("HORUS_STREAMS_CONFIG", "config/webrtc_streams.json")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def load_document(root: Path) -> dict[str, Any]:
    path = config_path(root)
    if not path.exists():
        if env_value("HORUS_STREAMS_CONFIG") not in {"", "config/webrtc_streams.json"}:
            raise ValueError(f"WebRTC streams configuration not found: {path}")
        return {"version": 1, "streams": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    elif isinstance(data, list):
        return {"version": 1, "streams": data}
    else:
        raise ValueError(f"Invalid stream config shape in {path}")


def load_config(root: Path) -> list[dict[str, Any]]:
    data = load_document(root)
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise ValueError(f"streams must be a list in {config_path(root)}")
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict):
            raise ValueError(f"stream {index + 1} must be an object in {config_path(root)}")
    return streams


def load_session(root: Path, role: str) -> dict[str, Any]:
    document = load_document(root)
    raw_session = document.get("session", {})
    if not isinstance(raw_session, dict):
        raise ValueError("session must be an object")
    configured_mode = env_value("HORUS_WEBRTC_SESSION_MODE", "auto").strip().lower()
    if configured_mode == "auto":
        configured_mode = str(raw_session.get("mode") or "").strip().lower()
    if not configured_mode:
        configured_mode = "bundled" if int(document.get("version", 1)) >= 2 else "isolated"
    if configured_mode not in {"isolated", "bundled"}:
        raise ValueError(f"Invalid WebRTC session mode: {configured_mode}")
    room = str(
        env_value("HORUS_WEBRTC_SESSION_ROOM")
        or raw_session.get("room")
        or env_value("HORUS_ROOM", "default")
    ).strip() or "default"
    return {
        "version": int(document.get("version", 1)),
        "mode": configured_mode,
        "room": room,
        "id": str(raw_session.get("id") or room),
        "role": role,
        "bitrate_budget_kbit": int(raw_session.get("bitrate_budget_kbit") or env_value("WEBRTC_SESSION_BUDGET_KBIT", "0") or 0),
    }


def normalize_stream(raw: dict[str, Any], role: str, index: int, total: int) -> dict[str, Any]:
    fallback_id = "primary" if index == 0 else f"camera-{index + 1}"
    identifier = stream_id(str(raw.get("id") or raw.get("name") or fallback_id), fallback_id)
    room = str(raw.get("room") or env_value("HORUS_ROOM", "default")).strip() or "default"
    input_topic = str(raw.get("input_topic") or raw.get("ros_image_topic") or env_value("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "/camera/image_raw"))
    output_topic = str(raw.get("output_topic") or stream_output_topic(room, identifier))
    qos = str(raw.get("ros_image_qos") or env_value("WEBRTC_ROS_IMAGE_QOS", "auto"))
    video_source = str(raw.get("video_source") or env_value("WEBRTC_VIDEO_SOURCE", "ros2"))
    frame_id = str(raw.get("frame_id") or f"{ros_safe_name(room)}_{ros_safe_name(identifier)}_webrtc_camera")
    if identifier == "primary" and total == 1:
        service = "webrtc"
    else:
        service = "webrtc-" + stream_id(identifier)
    stream = {
        "service": service,
        "id": identifier,
        "label": str(raw.get("label") or identifier),
        "room": room,
        "enabled": bool(raw.get("enabled", True)),
        "input_topic": input_topic,
        "output_topic": output_topic,
        "input_camera_info_topic": str(raw.get("input_camera_info_topic") or ""),
        "output_camera_info_topic": str(raw.get("output_camera_info_topic") or ""),
        "preserve_source_header": raw.get("preserve_source_header", False),
        "resize_mode": str(raw.get("resize_mode", "letterbox")),
        "metadata_wait_ms": int(raw.get("metadata_wait_ms", 80)),
        "transport": str(raw.get("transport", "compatible")),
        "priority": int(raw.get("priority", 1)),
        "width": int(raw.get("width") or env_value("WEBRTC_VIDEO_WIDTH", "1280") or 1280),
        "height": int(raw.get("height") or env_value("WEBRTC_VIDEO_HEIGHT", "720") or 720),
        "fps": int(raw.get("fps") or env_value("WEBRTC_VIDEO_FPS", "30") or 30),
        "bitrate_kbit": int(raw.get("bitrate_kbit") or env_value("VIDEO_BITRATE_KBIT", "6000") or 6000),
        "min_bitrate_kbit": int(raw.get("min_bitrate_kbit") or env_value("WEBRTC_MIN_BITRATE_KBIT", "1000") or 1000),
        "max_bitrate_kbit": int(raw.get("max_bitrate_kbit") or raw.get("bitrate_kbit") or env_value("WEBRTC_MAX_BITRATE_KBIT", env_value("VIDEO_BITRATE_KBIT", "6000")) or 6000),
        "video_source": video_source,
        "source_pipeline": str(raw.get("source_pipeline") or env_value("WEBRTC_GST_VIDEO_SOURCE", "")),
        "ros_image_qos": qos,
        "frame_id": frame_id,
        "role": role,
    }
    if not is_topic(stream["input_topic"]):
        raise ValueError(f"Invalid input_topic for stream {identifier}: {stream['input_topic']}")
    if not is_topic(stream["output_topic"]):
        raise ValueError(f"Invalid output_topic for stream {identifier}: {stream['output_topic']}")
    for field in ("input_camera_info_topic", "output_camera_info_topic"):
        if stream[field] and not is_topic(stream[field]):
            raise ValueError(f"Invalid {field} for stream {identifier}: {stream[field]}")
    if stream["ros_image_qos"] not in {"auto", "sensor_data", "default"}:
        raise ValueError(f"Invalid ros_image_qos for stream {identifier}: {stream['ros_image_qos']}")
    if stream["video_source"] not in {"ros2", "gst", "testsrc"}:
        raise ValueError(f"Invalid video_source for stream {identifier}: {stream['video_source']}")
    if not isinstance(stream["preserve_source_header"], bool):
        raise ValueError(f"preserve_source_header must be a boolean for stream {identifier}")
    if stream["resize_mode"] not in {"letterbox", "stretch"}:
        raise ValueError("resize_mode must be letterbox or stretch")
    if stream["transport"] not in {"compatible", "webrtc_only"}:
        raise ValueError("transport must be compatible or webrtc_only")
    if not 0 <= stream["metadata_wait_ms"] <= 250 or not 1 <= stream["priority"] <= 100:
        raise ValueError("invalid camera metadata wait or priority")
    for field in ("width", "height", "fps", "bitrate_kbit", "min_bitrate_kbit", "max_bitrate_kbit"):
        if stream[field] <= 0:
            raise ValueError(f"{field} must be positive for stream {identifier}, got {stream[field]}")
    if stream["width"] > 8192 or stream["height"] > 8192:
        raise ValueError(
            f"Video dimensions exceed the 8192-pixel limit for stream {identifier}: "
            f"{stream['width']}x{stream['height']}"
        )
    if stream["fps"] > 240:
        raise ValueError(f"fps exceeds the 240 FPS limit for stream {identifier}: {stream['fps']}")
    if not stream["min_bitrate_kbit"] <= stream["bitrate_kbit"] <= stream["max_bitrate_kbit"]:
        raise ValueError(
            f"bitrate_kbit must be within min_bitrate_kbit and max_bitrate_kbit for stream "
            f"{identifier}: {stream['min_bitrate_kbit']} <= {stream['bitrate_kbit']} "
            f"<= {stream['max_bitrate_kbit']}"
        )
    return stream


def validate_unique_stream_fields(streams: list[dict[str, Any]], session_mode: str = "isolated") -> None:
    fields = [
        ("id", "stream id"),
        ("service", "service name"),
        ("output_topic", "output topic"),
        ("output_camera_info_topic", "output CameraInfo topic"),
    ]
    if session_mode == "isolated":
        fields.append(("room", "WebRTC room"))
    for field, label in fields:
        seen: dict[str, str] = {}
        for stream in streams:
            value = str(stream.get(field) or "")
            if not value:
                continue
            current = str(stream.get("label") or stream.get("id") or value)
            previous = seen.get(value)
            if previous is not None:
                raise ValueError(f"Duplicate {label} {value!r} for streams {previous!r} and {current!r}")
            seen[value] = current


def load_streams(root: Path, role: str) -> list[dict[str, Any]]:
    if role not in {"robot", "machine"}:
        return []
    raw_streams = load_config(root)
    if not raw_streams and not config_path(root).exists():
        raw_streams = [default_stream(role)]
    streams = [
        normalize_stream(stream, role, index, len(raw_streams))
        for index, stream in enumerate(raw_streams)
        if bool(stream.get("enabled", True))
    ]
    validate_unique_stream_fields(streams, load_session(root, role)["mode"])
    budget = load_session(root, role)["bitrate_budget_kbit"]
    if budget and budget < sum(stream["min_bitrate_kbit"] for stream in streams):
        raise ValueError("session bitrate budget is below the configured track minima")
    return streams


def capacity_report(root, role):
    streams, session = load_streams(root, role), load_session(root, role)
    rooms = list(dict.fromkeys([session["room"], *[room.strip() for room in env_value(
        "HORUS_WEBRTC_ADDITIONAL_ROOMS", "").split(",") if room.strip()]])) if role == "robot" else [session["room"]]
    pixels = sum(stream["width"] * stream["height"] * stream["fps"] for stream in streams)
    result = {"tracks_per_operator": len(streams), "operators": len(rooms), "encoder_instances": len(streams) * len(rooms),
              "encoded_pixels_per_sec": pixels * len(rooms),
              "configured_bitrate_kbit": min(sum(stream["bitrate_kbit"] for stream in streams),
                  session["bitrate_budget_kbit"] or 2**63) * len(rooms),
              "quality_reduced": False, "capacity_measured": False}
    for key, field in (("WEBRTC_MAX_OPERATORS", "operators"), ("WEBRTC_MAX_ENCODED_PIXELS_PER_SEC", "encoded_pixels_per_sec")):
        limit = int(env_value(key, "0") or 0)
        if limit and result[field] > limit:
            raise ValueError(f"configured {field}={result[field]} exceeds explicit {key}={limit}; no streams were silently degraded")
    return result


def print_tsv(streams: list[dict[str, Any]]) -> None:
    for stream in streams:
        pipeline_b64 = base64.b64encode(stream["source_pipeline"].encode("utf-8")).decode("ascii") or "-"
        fields = [
            stream["service"],
            stream["id"],
            stream["room"],
            stream["input_topic"],
            stream["output_topic"],
            str(stream["width"]),
            str(stream["height"]),
            str(stream["fps"]),
            str(stream["bitrate_kbit"]),
            stream["video_source"],
            pipeline_b64,
            stream["frame_id"],
            stream["ros_image_qos"],
        ]
        print("\t".join(fields))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "json", "session-json", "session-tsv", "capacity-json"])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--role", default=env_value("HORUS_ROLE", "robot"), choices=["robot", "machine", "cloud", "teammate"])
    args = parser.parse_args()

    root = Path(args.root).resolve()
    try:
        streams = load_streams(root, args.role)
        session = load_session(root, args.role)
    except Exception as exc:
        print(f"Invalid WebRTC streams config: {exc}", file=sys.stderr)
        return 2

    if args.command == "capacity-json":
        print(json.dumps(capacity_report(root, args.role)))
    elif args.command == "json":
        print(json.dumps({"version": session["version"], "session": session, "streams": streams}, indent=2))
    elif args.command == "session-json":
        print(json.dumps(session, separators=(",", ":")))
    elif args.command == "session-tsv":
        print("\t".join((session["mode"], session["room"], session["id"])))
    else:
        print_tsv(streams)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
