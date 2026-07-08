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


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


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


def load_config(root: Path) -> list[dict[str, Any]]:
    path = config_path(root)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        streams = data.get("streams", [])
    elif isinstance(data, list):
        streams = data
    else:
        raise ValueError(f"Invalid stream config shape in {path}")
    if not isinstance(streams, list):
        raise ValueError(f"streams must be a list in {path}")
    return [stream for stream in streams if isinstance(stream, dict)]


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
        "width": int(raw.get("width") or env_value("WEBRTC_VIDEO_WIDTH", "1280") or 1280),
        "height": int(raw.get("height") or env_value("WEBRTC_VIDEO_HEIGHT", "720") or 720),
        "fps": int(raw.get("fps") or env_value("WEBRTC_VIDEO_FPS", "30") or 30),
        "bitrate_kbit": int(raw.get("bitrate_kbit") or env_value("VIDEO_BITRATE_KBIT", "6000") or 6000),
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
    if stream["ros_image_qos"] not in {"auto", "sensor_data", "default"}:
        raise ValueError(f"Invalid ros_image_qos for stream {identifier}: {stream['ros_image_qos']}")
    if stream["video_source"] not in {"ros2", "gst", "testsrc"}:
        raise ValueError(f"Invalid video_source for stream {identifier}: {stream['video_source']}")
    return stream


def validate_unique_stream_fields(streams: list[dict[str, Any]]) -> None:
    fields = [
        ("id", "stream id"),
        ("service", "service name"),
        ("room", "WebRTC room"),
        ("output_topic", "output topic"),
    ]
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
    if not raw_streams:
        raw_streams = [default_stream(role)]
    streams = [
        normalize_stream(stream, role, index, len(raw_streams))
        for index, stream in enumerate(raw_streams)
        if bool(stream.get("enabled", True))
    ]
    if not streams:
        streams = [normalize_stream(default_stream(role), role, 0, 1)]
    validate_unique_stream_fields(streams)
    return streams


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
    parser.add_argument("command", choices=["list", "json"])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--role", default=env_value("HORUS_ROLE", "robot"), choices=["robot", "machine", "cloud", "teammate"])
    args = parser.parse_args()

    root = Path(args.root).resolve()
    try:
        streams = load_streams(root, args.role)
    except Exception as exc:
        print(f"Invalid WebRTC streams config: {exc}", file=sys.stderr)
        return 2

    if args.command == "json":
        print(json.dumps({"version": 1, "streams": streams}, indent=2))
    else:
        print_tsv(streams)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
