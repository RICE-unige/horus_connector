#!/usr/bin/env python3
"""Live HoloLens field-teammate relay.

This process is the ROS 2 driver for a human field teammate. The HoloLens app
keeps running as a lightweight edge device: it exposes a pose stream on the SI
port and JPEG personal-video frames with capture-time locatable-camera metadata
on the PV port. This relay dials the headset, converts those streams into the
normal HORUS field-teammate ROS contract, and leaves SDK registration unchanged.

Typical run:
    cd ~/horus_connector
    ./horus setup teammate
    ./horus doctor teammate
    ./horus launch teammate

Default video profile:
    fast60 = 640x360@60 request, JPEG q25, HoloLens VideoConferencing capture profile

Useful checks:
    ros2 run rqt_image_view rqt_image_view /field_teammate_1/fpv/image_raw
    ros2 topic hz /field_teammate_1/fpv/image_raw/compressed
    ros2 topic echo /field_teammate_1/fpv/camera_pose --once
    ros2 topic echo /field_teammate_1/fpv/camera_info --once
    ros2 topic echo /field_teammate_1/localization_confidence
    ros2 run tf2_ros tf2_echo map field_teammate_1/base
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import queue
import re
import select
import socket
import sys
import threading
import time
import uuid

from media_health import write_status
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

# This relay is the CANONICAL live implementation of the HORUS field-teammate
# topic contract (the SDK deliberately ships no copy). The source of truth for
# the topic shapes it emits/consumes is the SDK contract fixture:
#   horus_sdk/contracts/fixtures/field_teammate_hololens.json  (field_teammate.v1)
# The SDK freezes those topic names in
# test_field_teammate_payload.py::test_field_teammate_topic_contract_frozen;
# this repo freezes the relay dry-run contract and local ROS-only teammate role
# in tests/test_field_teammate_contract.py. If these tests ever change,
# update all three contract surfaces in lockstep.
FIELD_TEAMMATE_CONTRACT_VERSION = "field_teammate.v1"

DEFAULT_PV_PORT = 3810
DEFAULT_SPATIAL_INPUT_PORT = 3814
DEFAULT_UMQ_PORT = 3816
DEFAULT_PROFILE_HEIGHT_M = 1.75
DEFAULT_CAMERA_HEIGHT_RATIO = 0.92
DEFAULT_FLOOR_HEIGHT_M = 0.0
POSE_GREETING = b"HORUS_POSE_STREAM_V1\n"
VIDEO_GREETING = b"HORUS_PV_STREAM_V1\n"
UMQ_GREETING = b"HORUS_UMQ_STREAM_V1\n"

VIDEO_PROFILES: dict[str, dict[str, Any] | None] = {
    "app": None,
    "balanced": {
        "profile": "balanced",
        "captureMode": "auto",
        "width": 640,
        "height": 360,
        "fps": 30,
        "jpegQuality": 55,
    },
    "fast60": {
        "profile": "fast60",
        "captureMode": "video_conferencing",
        "width": 640,
        "height": 360,
        "fps": 60,
        "jpegQuality": 25,
    },
    "hd720": {
        "profile": "hd720",
        "captureMode": "auto",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "jpegQuality": 45,
    },
    "mapping": {
        "profile": "mapping",
        "captureMode": "auto",
        "width": 640,
        "height": 360,
        "fps": 15,
        "jpegQuality": 80,
    },
}


def _normalize_topic_leaf(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _normalize_frame_token(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = text.strip("/")
    text = re.sub(r"[^A-Za-z0-9_/]+", "_", text)
    text = re.sub(r"/+", "/", text)
    return text.strip("/") or fallback


@dataclass(frozen=True)
class FieldTeammateTopics:
    """Connector-local copy of the ROS topic/frame contract used by the SDK."""

    base_frame: str
    camera_frame: str
    first_person_video_raw_topic: str
    first_person_video_topic: str
    first_person_video_camera_info_topic: str
    first_person_video_camera_pose_topic: str
    localization_confidence_topic: str
    guidance_request_topic: str
    guidance_response_topic: str
    guidance_state_topic: str
    guidance_annotation_topic: str
    guidance_route_topic: str
    guidance_warning_topic: str
    status_topic: str
    audio_topic: str

    @classmethod
    def from_name(cls, name: str) -> "FieldTeammateTopics":
        leaf = _normalize_topic_leaf(name, "field_teammate")
        prefix = f"/{leaf}"
        return cls(
            base_frame=f"{leaf}/base",
            camera_frame=f"{leaf}/camera",
            first_person_video_raw_topic=f"{prefix}/fpv/image_raw",
            first_person_video_topic=f"{prefix}/fpv/image_raw/compressed",
            first_person_video_camera_info_topic=f"{prefix}/fpv/camera_info",
            first_person_video_camera_pose_topic=f"{prefix}/fpv/camera_pose",
            localization_confidence_topic=f"{prefix}/localization_confidence",
            guidance_request_topic=f"{prefix}/guidance/request",
            guidance_response_topic=f"{prefix}/guidance/response",
            guidance_state_topic=f"{prefix}/guidance/state",
            guidance_annotation_topic=f"{prefix}/guidance/annotation",
            guidance_route_topic=f"{prefix}/guidance/route",
            guidance_warning_topic=f"{prefix}/guidance/warning",
            status_topic=f"{prefix}/status",
            audio_topic=f"{prefix}/audio/message",
        )


@dataclass(frozen=True)
class HoloLensRelayEndpoint:
    host: str
    personal_video_port: int = DEFAULT_PV_PORT
    spatial_input_port: int = DEFAULT_SPATIAL_INPUT_PORT
    unity_message_queue_port: int = DEFAULT_UMQ_PORT


@dataclass(frozen=True)
class TimedCameraPose:
    """One HoloLens head-camera pose on the headset monotonic clock."""

    timestamp: float
    arrival_monotonic: float
    position: tuple[float, float, float]
    orientation: tuple[float, float, float, float]


def _pose_tracking_valid(payload: dict[str, Any]) -> bool:
    """Legacy poses remain valid; explicit tracking loss must not become TF."""
    if payload.get("tracking_valid", True) is not True:
        return False
    try:
        confidence = float(payload.get("confidence", 1.0))
    except (TypeError, ValueError):
        return False
    return math.isfinite(confidence) and confidence > 0.0


def _camera_pose_fallback_allowed(header: dict[str, Any]) -> bool:
    # Revision-aware apps explicitly withhold unlocatable/retired camera poses.
    return "tracking_origin_revision" not in header


def _camera_pose_epoch_matches(header: dict[str, Any], active_revision: int | None) -> bool:
    if "tracking_origin_revision" not in header:
        return True
    revision = header["tracking_origin_revision"]
    return type(revision) is int and revision >= 0 and revision == active_revision


def _tracking_packet(payload, frame, child_frame, stamp, session_id, source):
    """Atomic measured pose + origin identity, independent of display-base TF."""
    revision = payload.get("tracking_origin_revision")
    valid = (payload.get("tracking_valid") is True and _pose_tracking_valid(payload) and
             type(revision) is int and revision >= 0)
    position = orientation = None
    source_timestamp = None
    try:
        source_timestamp = float(payload["timestamp"])
        vectors = [tuple(float(payload[key][axis]) for axis in ("x", "y", "z"))
                   for key in ("position", "forward", "up")]
        valid &= (math.isfinite(source_timestamp) and source_timestamp > 0 and
                  all(math.isfinite(v) for vector in vectors for v in vector) and
                  _norm(vectors[1]) > 1e-6 and _norm(vectors[2]) > 1e-6 and
                  _norm(_cross(_normalize(vectors[1], (0, 0, 0)),
                               _normalize(vectors[2], (0, 0, 0)))) > .01)
        if valid:
            position = _unity_vec_to_ros(payload["position"])
            orientation = _orientation_from_pose(payload)
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if source_timestamp is not None and not math.isfinite(source_timestamp):
        source_timestamp = None
    return {"schema": "horus.field_tracking.v1", "session_id": session_id,
            "header": {"frame_id": frame, "stamp": {"sec": stamp.sec, "nanosec": stamp.nanosec}},
            "child_frame_id": child_frame, "pose_source": source,
            "source_timestamp_sec": source_timestamp,
            "tracking_origin_revision": revision if type(revision) is int and revision >= 0 else None,
            "tracking_valid": bool(valid), "position": position, "orientation_xyzw": orientation}


def _nearest_camera_pose(
    samples: Iterable[TimedCameraPose],
    timestamp: float,
    max_delta_seconds: float = 0.10,
) -> TimedCameraPose | None:
    """Return the closest capture-clock pose within a strict time tolerance."""

    if not math.isfinite(timestamp) or timestamp <= 0.0:
        return None
    nearest = min(samples, key=lambda sample: abs(sample.timestamp - timestamp), default=None)
    if nearest is None or abs(nearest.timestamp - timestamp) > max_delta_seconds:
        return None
    return nearest


def _queue_latest(mailbox, sample):
    try:
        mailbox.put_nowait(sample)
    except queue.Full:
        try:
            mailbox.get_nowait()
        except queue.Empty:
            pass
        mailbox.put_nowait(sample)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", default="field_teammate_1", help="Teammate entity name/namespace.")
    parser.add_argument(
        "--hololens-host",
        required=True,
        help="IPv4/hostname of the HoloLens as shown in the HORUS Field status panel.",
    )
    parser.add_argument("--pv-port", type=int, default=DEFAULT_PV_PORT, help="HoloLens personal-video stream port.")
    parser.add_argument(
        "--spatial-input-port",
        type=int,
        default=DEFAULT_SPATIAL_INPUT_PORT,
        help="HoloLens spatial/head-pose stream port.",
    )
    parser.add_argument(
        "--umq-port",
        type=int,
        default=DEFAULT_UMQ_PORT,
        help="HoloLens Unity message queue/back-channel port.",
    )
    parser.add_argument("--map-frame", default="map", help="Parent frame for the live HoloLens pose TF.")
    parser.add_argument(
        "--profile-height",
        type=float,
        default=DEFAULT_PROFILE_HEIGHT_M,
        help="Human profile height in meters. Used to place the base frame on the floor below the HoloLens camera.",
    )
    parser.add_argument(
        "--camera-height",
        type=float,
        default=0.0,
        help=(
            "Camera/eye height above the teammate base frame. "
            "Default: 0.92 * --profile-height."
        ),
    )
    parser.add_argument(
        "--floor-height",
        type=float,
        default=DEFAULT_FLOOR_HEIGHT_M,
        help="Minimum ROS Z height for the teammate base frame.",
    )
    parser.add_argument(
        "--pose-origin",
        choices=("camera", "base"),
        default="camera",
        help=(
            "Interpret the HoloLens pose stream as the camera/head pose or as the floor-level base pose. "
            "The deployed HORUS Field app publishes camera/head pose."
        ),
    )
    parser.add_argument(
        "--no-hand-tf", "--head-only", dest="no_hand_tf", action="store_true",
        help="Skip hand/skeleton TF for head-only presentations; head tracking is unchanged.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=2.0,
        help="Seconds used for endpoint connection attempts.",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=1.0,
        help="Seconds to wait before reconnecting after a stream disconnect.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print endpoint and topic contract without opening ROS or sockets.",
    )
    raw_image_group = parser.add_mutually_exclusive_group()
    raw_image_group.add_argument(
        "--raw-image",
        dest="no_raw_image",
        action="store_false",
        help="Also publish decoded sensor_msgs/Image frames. This costs CPU and can reduce live FPV framerate.",
    )
    raw_image_group.add_argument(
        "--no-raw-image",
        dest="no_raw_image",
        action="store_true",
        help="Only publish sensor_msgs/CompressedImage; skip the decoded sensor_msgs/Image helper topic.",
    )
    parser.set_defaults(no_raw_image=True)
    parser.add_argument(
        "--video-profile",
        choices=sorted(VIDEO_PROFILES.keys()),
        default="fast60",
        help=(
            "Runtime HoloLens PV profile. Default: fast60. 'app' keeps the deployed app config; "
            "'balanced'=640x360@30 q55; 'fast60'=640x360@60 q25 VideoConferencing; "
            "'hd720'=1280x720@30 q45; 'mapping'=640x360@15 q80 for concurrent Research Mode."
        ),
    )
    parser.add_argument(
        "--video-mode",
        choices=["auto", "video_conferencing", "high_frame_rate", "default"],
        help="Override the app capture mode for the selected/custom profile.",
    )
    parser.add_argument("--video-width", type=int, help="Override runtime HoloLens PV width.")
    parser.add_argument("--video-height", type=int, help="Override runtime HoloLens PV height.")
    parser.add_argument("--video-fps", type=int, help="Override runtime HoloLens PV target FPS.")
    parser.add_argument("--video-quality", type=int, help="Override runtime HoloLens JPEG quality, 25-95.")
    parser.add_argument(
        "--voice-signaling-host",
        default="",
        help="Voice signaling host. Default: use the relay's network route to the HoloLens.",
    )
    parser.add_argument("--voice-signaling-port", type=int, default=8765)
    parser.add_argument("--voice-signaling-secure", action="store_true")
    parser.add_argument(
        "--voice-token",
        default="",
        help="Optional production client credential. Empty enables trusted-development voice.",
    )
    parser.add_argument(
        "--voice-ice-server",
        action="append",
        default=[],
        help="Fallback STUN/TURN URI sent to HORUS Field. May be repeated.",
    )
    parser.add_argument("--disable-voice", action="store_true")
    parser.add_argument(
        "--check-endpoint",
        action="store_true",
        help="Probe configured HoloLens ports and exit.",
    )
    return parser


def _topic_contract(name: str) -> dict[str, Any]:
    config = FieldTeammateTopics.from_name(name)
    return {
        "contract_version": FIELD_TEAMMATE_CONTRACT_VERSION,
        "tf_topic": "/tf",
        "base_frame": config.base_frame,
        "camera_frame": config.camera_frame,
        "publishes": {
            "status": config.status_topic,
            "localization_confidence": config.localization_confidence_topic,
            "first_person_video_raw": config.first_person_video_raw_topic,
            "first_person_video": config.first_person_video_topic,
            "first_person_video_camera_info": config.first_person_video_camera_info_topic,
            "first_person_video_camera_pose": config.first_person_video_camera_pose_topic,
            "guidance_response": config.guidance_response_topic,
            "guidance_state": config.guidance_state_topic,
        },
        "subscribes": {
            "guidance_request": config.guidance_request_topic,
            "guidance_annotation": config.guidance_annotation_topic,
            "guidance_route": config.guidance_route_topic,
            "guidance_warning": config.guidance_warning_topic,
            "audio_message": config.audio_topic,
        },
    }


def _probe_port(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_endpoint(endpoint: HoloLensRelayEndpoint, timeout: float) -> dict[str, bool]:
    return {
        "personal_video": _probe_port(endpoint.host, endpoint.personal_video_port, timeout),
        "spatial_input": _probe_port(endpoint.host, endpoint.spatial_input_port, timeout),
        "unity_message_queue": _probe_port(endpoint.host, endpoint.unity_message_queue_port, timeout),
    }


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _build_video_profile_command(args: argparse.Namespace) -> dict[str, Any] | None:
    base = VIDEO_PROFILES.get(str(args.video_profile or "app"))
    has_override = any(
        value is not None
        for value in (
            args.video_mode,
            args.video_width,
            args.video_height,
            args.video_fps,
            args.video_quality,
        )
    )
    if base is None and not has_override:
        return None

    command = dict(base or VIDEO_PROFILES["balanced"] or {})
    command.update({"type": "set_video_profile", "enabled": True})
    if base is None:
        command["profile"] = "custom"
    if args.video_mode is not None:
        command["captureMode"] = args.video_mode
    if args.video_width is not None:
        command["width"] = _clamp_int(args.video_width, 160, 1920)
    if args.video_height is not None:
        command["height"] = _clamp_int(args.video_height, 120, 1080)
    if args.video_fps is not None:
        command["fps"] = _clamp_int(args.video_fps, 1, 60)
    if args.video_quality is not None:
        command["jpegQuality"] = _clamp_int(args.video_quality, 25, 95)
    if has_override and args.video_profile == "app":
        command["profile"] = "custom"
    return command


def _build_voice_config_command(
    args: argparse.Namespace,
    fallback_host: str = "",
) -> dict[str, Any] | None:
    if bool(args.disable_voice):
        return None
    host = str(args.voice_signaling_host or fallback_host or "").strip()
    if not host:
        return None
    room = str(args.name or "field_teammate_1").strip()
    token = str(args.voice_token or "")
    scheme = "wss" if bool(args.voice_signaling_secure) else "ws"
    return {
        "type": "configure_voice",
        "signalingUrl": f"{scheme}://{host}:{int(args.voice_signaling_port)}",
        "room": room,
        "clientId": f"field_teammate:{room}",
        "token": token,
        "trustedDevelopment": not bool(token),
        "autoConnect": True,
        "relayOnly": False,
        "fallbackIceServers": [
            str(value).strip() for value in args.voice_ice_server if str(value).strip()
        ],
    }


def _unity_vec_to_ros(vector: dict[str, Any]) -> tuple[float, float, float]:
    """Convert Unity x-right/y-up/z-forward into ROS x-forward/y-left/z-up."""
    ux = float(vector.get("x", 0.0))
    uy = float(vector.get("y", 0.0))
    uz = float(vector.get("z", 0.0))
    return (uz, -ux, uy)


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])


def _normalize(vector: tuple[float, float, float], fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    length = _norm(vector)
    if length < 1.0e-8:
        return fallback
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _matrix_to_quaternion(matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]) -> tuple[float, float, float, float]:
    m00, m01, m02 = matrix[0]
    m10, m11, m12 = matrix[1]
    m20, m21, m22 = matrix[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        return ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s)
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        return (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    if m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        return ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
    return ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)


def _quaternion_normalize(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    length = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if length < 1.0e-8:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / length, q[1] / length, q[2] / length, q[3] / length)


def _quaternion_multiply(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return _quaternion_normalize(
        (
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        )
    )


def _quaternion_inverse(q: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    qx, qy, qz, qw = _quaternion_normalize(q)
    return (-qx, -qy, -qz, qw)


def _rotate_vector(
    q: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    qx, qy, qz, qw = _quaternion_normalize(q)
    vx, vy, vz = vector
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def _yaw_to_quaternion(yaw: float) -> tuple[float, float, float, float]:
    half = yaw * 0.5
    return (0.0, 0.0, math.sin(half), math.cos(half))


def _orientation_from_pose(payload: dict[str, Any]) -> tuple[float, float, float, float]:
    forward = _normalize(_unity_vec_to_ros(payload.get("forward") or {}), (1.0, 0.0, 0.0))
    up = _normalize(_unity_vec_to_ros(payload.get("up") or {}), (0.0, 0.0, 1.0))
    left = _normalize(_cross(up, forward), (0.0, 1.0, 0.0))
    corrected_up = _normalize(_cross(forward, left), (0.0, 0.0, 1.0))
    matrix = (
        (forward[0], left[0], corrected_up[0]),
        (forward[1], left[1], corrected_up[1]),
        (forward[2], left[2], corrected_up[2]),
    )
    return _quaternion_normalize(_matrix_to_quaternion(matrix))


def _yaw_from_pose(payload: dict[str, Any]) -> float:
    forward = _normalize(_unity_vec_to_ros(payload.get("forward") or {}), (1.0, 0.0, 0.0))
    return math.atan2(forward[1], forward[0])


def _pose_heights(source_z: float, eye_height: float, floor_height: float, origin: str):
    if origin == "camera":
        base_z = max(floor_height, source_z - eye_height)
        # Clamping the display base to a floor must never move the measured head.
        return base_z, source_z - base_z
    return source_z, eye_height


def _normalize_hand_joint_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", text).replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-zA-Z0-9_]+", "", text).strip("_").lower()
    return text


def _parse_hand_joints(hand: dict[str, Any]) -> list[dict[str, Any]]:
    joints = hand.get("joints")
    if not isinstance(joints, list):
        return []

    parsed: list[dict[str, Any]] = []
    for joint in joints:
        if not isinstance(joint, dict):
            continue
        name = _normalize_hand_joint_name(joint.get("name") or joint.get("joint"))
        if not name:
            continue
        position = joint.get("position")
        if not isinstance(position, dict):
            continue
        parsed.append(
            {
                "name": name,
                "position": _unity_vec_to_ros(position),
                "rotation": _orientation_from_pose(joint),
            }
        )
    return parsed


def _hands_from_pose(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hands = payload.get("hands")
    if not isinstance(hands, list):
        return []

    parsed: list[dict[str, Any]] = []
    for hand in hands:
        if not isinstance(hand, dict):
            continue
        handedness = str(hand.get("hand") or hand.get("handedness") or hand.get("side") or "").strip().lower()
        if handedness not in {"left", "right"}:
            continue
        if hand.get("tracked") is False:
            continue
        position = hand.get("position")
        if not isinstance(position, dict):
            continue
        parsed.append(
            {
                "hand": handedness,
                "position": _unity_vec_to_ros(position),
                "rotation": _orientation_from_pose(hand),
                "joints": _parse_hand_joints(hand),
            }
        )
    return parsed


def _read_json_line(stream) -> dict[str, Any] | None:
    line = stream.readline(64 * 1024 + 1)
    if not line:
        return None
    if len(line) > 64 * 1024:
        raise ValueError("HoloLens JSON header exceeds 64 KiB")
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    return json.loads(text)


def _connect_stream(endpoint: HoloLensRelayEndpoint, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((endpoint.host, port), timeout=timeout)
    # Keep the connection attempt bounded, then use blocking reads for the live
    # stream. Python buffered socket files become unusable after read timeouts,
    # which would make normal frame gaps look like stream failures.
    sock.settimeout(None)
    return sock


class DeadlineReader:
    """Framed reads with absolute deadlines, without socket.makefile timeouts."""

    def __init__(self, sock, timeout=5.0):
        self.sock = sock
        self.timeout = max(.1, timeout)
        self.pending = bytearray()

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
            raise socket.timeout("HoloLens stream stalled; reconnecting")
        chunk = self.sock.recv(65536)
        if chunk:
            self.pending.extend(chunk)
        return bool(chunk)

    def readline(self, limit=65537):
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.pending and len(self.pending) < limit:
            if not self._receive(deadline):
                break
        newline = self.pending.find(b"\n")
        count = min(limit, newline + 1 if newline >= 0 else len(self.pending))
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def read(self, count):
        deadline = time.monotonic() + self.timeout
        while len(self.pending) < count:
            if not self._receive(deadline):
                break
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result


def _read_control_ack(stream, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        ack_line = stream.readline()
        if not ack_line:
            return None
        text = ack_line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            if text.startswith("HORUS_UMQ_STREAM") or text.startswith("HORUS Field"):
                continue
            return {"type": "unexpected_control_line", "line": text}
        if isinstance(parsed, dict):
            return parsed
    raise socket.timeout("timed out waiting for JSON control acknowledgement")


def _run_live(args: argparse.Namespace, endpoint: HoloLensRelayEndpoint) -> int:
    try:
        import rclpy
        from geometry_msgs.msg import PoseStamped, TransformStamped
        from rclpy.executors import ExternalShutdownException
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import CameraInfo, CompressedImage, Image
        from std_msgs.msg import Float32, String
        from tf2_ros import TransformBroadcaster
    except Exception as exc:
        print(f"ERROR: ROS 2 Python dependencies not available. Source ROS 2 first. Details: {exc}")
        return 1

    contract_config = FieldTeammateTopics.from_name(args.name)

    class LiveHoloLensFieldTeammateRelay(Node):
        def __init__(self) -> None:
            super().__init__("hololens_field_teammate_relay")
            self._stop = threading.Event()
            self._sockets_lock = threading.Lock()
            self._sockets: set[socket.socket] = set()
            self._endpoint = endpoint
            self._map_frame = str(args.map_frame or "map")
            self._base_frame = contract_config.base_frame
            self._camera_frame = contract_config.camera_frame
            frame_prefix = self._base_frame.rsplit("/", 1)[0] if "/" in self._base_frame else self._base_frame
            self._frame_prefix = frame_prefix
            self._hand_frames = {
                "left": f"{frame_prefix}/left_hand",
                "right": f"{frame_prefix}/right_hand",
            }
            self._profile_height = max(1.35, min(2.10, float(args.profile_height)))
            requested_camera_height = float(args.camera_height or 0.0)
            if requested_camera_height <= 0.0:
                requested_camera_height = self._profile_height * DEFAULT_CAMERA_HEIGHT_RATIO
            self._camera_height = max(1.10, min(2.05, requested_camera_height))
            self._floor_height = float(args.floor_height)
            self._pose_origin = str(args.pose_origin or "camera")
            self._connect_timeout = max(0.25, float(args.connect_timeout))
            self._reconnect_delay = max(0.1, float(args.reconnect_delay))
            self._video_profile_command = _build_video_profile_command(args)
            self._tf_broadcaster = TransformBroadcaster(self)
            self._hand_tf_broadcaster = TransformBroadcaster(self)
            self._hand_samples = queue.Queue(maxsize=1)
            self._status_pub = self.create_publisher(String, contract_config.status_topic, 10)
            self._confidence_pub = self.create_publisher(Float32, contract_config.localization_confidence_topic, 10)
            self._fpv_pub = self.create_publisher(CompressedImage, contract_config.first_person_video_topic, 10)
            self._camera_info_topic = contract_config.first_person_video_camera_info_topic
            self._camera_info_pub = self.create_publisher(CameraInfo, self._camera_info_topic, 10)
            self._camera_pose_topic = contract_config.first_person_video_camera_pose_topic
            self._camera_pose_pub = self.create_publisher(PoseStamped, self._camera_pose_topic, 10)
            # Local additive channels: never overload the image-synchronized PV pose.
            self._tracking_session_id = uuid.uuid4().hex
            self._head_pose_pub = self.create_publisher(String, f"/{frame_prefix}/tracking/head_pose",
                QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
            self._pv_pose_pub = self.create_publisher(String, f"/{frame_prefix}/tracking/pv_pose", 10)
            self._guidance_state_pub = self.create_publisher(String, contract_config.guidance_state_topic, 10)
            self._guidance_response_pub = self.create_publisher(String, contract_config.guidance_response_topic, 10)
            self._guidance_subscriptions = [
                self.create_subscription(
                    String,
                    contract_config.guidance_request_topic,
                    lambda msg: self._on_guidance_message("request", msg),
                    10,
                ),
                self.create_subscription(
                    String,
                    contract_config.guidance_annotation_topic,
                    lambda msg: self._on_guidance_message("annotation", msg),
                    10,
                ),
                self.create_subscription(
                    String,
                    contract_config.guidance_route_topic,
                    lambda msg: self._on_guidance_message("route", msg),
                    10,
                ),
                self.create_subscription(
                    String,
                    contract_config.guidance_warning_topic,
                    lambda msg: self._on_guidance_message("warning", msg),
                    10,
                ),
            ]
            self._raw_image_topic = contract_config.first_person_video_raw_topic
            self._raw_image_pub = None
            self._cv2 = None
            self._np = None
            if not args.no_raw_image:
                try:
                    import cv2  # type: ignore
                    import numpy as np  # type: ignore

                    self._cv2 = cv2
                    self._np = np
                    self._raw_image_pub = self.create_publisher(Image, self._raw_image_topic, 10)
                except Exception as exc:
                    self.get_logger().warning(
                        f"Raw HoloLens image topic disabled because JPEG decoding is unavailable: {exc}"
                    )
            self._last_status_time = 0.0
            self._last_pose_time = None
            self._last_video_time = None
            self._last_confidence = 0.0
            self._active_pose_revision: int | None = None
            self._status_lock = threading.Lock()
            self._pose_count = 0
            self._video_count = 0
            self._last_video_width = 0
            self._last_video_height = 0
            self._video_pose_count = 0
            self._video_native_pose_count = 0
            self._video_fallback_pose_count = 0
            self._pose_history_lock = threading.Lock()
            self._pose_history: deque[TimedCameraPose] = deque(maxlen=240)
            self._last_video_profile_ack: dict[str, Any] | None = None
            self._last_voice_config_ack: dict[str, Any] | None = None
            self._control_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
            if self._video_profile_command is not None:
                self._enqueue_control(self._video_profile_command)
            self._threads = [
                threading.Thread(target=self._pose_loop, name="hololens_pose_stream", daemon=True),
                threading.Thread(target=self._video_loop, name="hololens_video_stream", daemon=True),
                threading.Thread(target=self._control_loop, name="hololens_control_stream", daemon=True),
            ]
            if not args.no_hand_tf:
                self._threads.append(threading.Thread(target=self._hand_loop, name="hololens_hand_tf", daemon=True))
            for thread in self._threads:
                thread.start()
            self.create_timer(0.5, self._publish_status)
            self.get_logger().info(
                f"Live HoloLens relay started for {args.name}: "
                f"PV {endpoint.host}:{endpoint.personal_video_port}, "
                f"SI {endpoint.host}:{endpoint.spatial_input_port}"
            )
            if self._video_profile_command is not None:
                self.get_logger().info(
                    "Requested HoloLens video profile: "
                    + json.dumps(self._video_profile_command, sort_keys=True)
                )

        def stop(self) -> None:
            self._stop.set()
            with self._sockets_lock:
                sockets = list(self._sockets)
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            for thread in self._threads:
                thread.join(timeout=1.0)
            write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"), {"role": "teammate", "state": "stopped", "tracks": {}})

        def _track_socket(self, sock: socket.socket) -> None:
            with self._sockets_lock:
                self._sockets.add(sock)

        def _untrack_socket(self, sock: socket.socket) -> None:
            with self._sockets_lock:
                self._sockets.discard(sock)

        def _enqueue_control(self, payload: dict[str, Any]) -> None:
            payload = dict(payload)
            payload["_queued_monotonic"] = time.monotonic()
            try:
                self._control_queue.put_nowait(payload)
            except queue.Full:
                try:
                    self._control_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._control_queue.put_nowait(payload)
                except queue.Full:
                    self.get_logger().warning("Dropping HoloLens control message because the queue is full")

        def _control_loop(self) -> None:
            while not self._stop.is_set():
                try:
                    with _connect_stream(self._endpoint, self._endpoint.unity_message_queue_port, self._connect_timeout) as sock:
                        self._track_socket(sock)
                        try:
                            sock.settimeout(max(1.0, self._connect_timeout))
                            stream = sock.makefile("rb")
                            greeting = stream.readline()
                            if greeting and not greeting.startswith(UMQ_GREETING):
                                self.get_logger().warning(f"Unexpected UMQ greeting: {greeting!r}")
                            self.get_logger().info("Connected to HoloLens control stream.")
                            local_host = str(sock.getsockname()[0])
                            voice_command = _build_voice_config_command(args, local_host)
                            if voice_command is not None:
                                self._enqueue_control(voice_command)
                                self.get_logger().info(
                                    "Configured HoloLens voice from the HORUS relay route: "
                                    f"{voice_command['signalingUrl']} room={voice_command['room']}"
                                )
                            while not self._stop.is_set():
                                try:
                                    command = self._control_queue.get(timeout=0.1)
                                except queue.Empty:
                                    continue
                                created = command.pop("_queued_monotonic", time.monotonic())
                                if time.monotonic() - created > 5 and command.get("type") not in {"configure_voice", "set_video_profile"}:
                                    self.get_logger().warning("Dropped expired HoloLens control message")
                                    continue
                                payload = (json.dumps(command, separators=(",", ":")) + "\n").encode("utf-8")
                                sock.sendall(payload)
                                try:
                                    ack = _read_control_ack(stream, self._connect_timeout)
                                except socket.timeout:
                                    self.get_logger().warning("Timed out waiting for HoloLens control acknowledgement")
                                    break
                                if not ack:
                                    break
                                if ack.get("type") == "video_profile_ack":
                                    self._last_video_profile_ack = ack
                                elif ack.get("type") == "voice_config_ack":
                                    self._last_voice_config_ack = ack
                                self.get_logger().info(
                                    "HoloLens control acknowledged: " + json.dumps(ack, sort_keys=True)
                                )
                        finally:
                            self._untrack_socket(sock)
                except OSError as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens control stream unavailable: {exc}")
                except Exception as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens control stream error: {exc}")
                self._sleep_reconnect()

        def _pose_loop(self) -> None:
            while not self._stop.is_set():
                try:
                    with _connect_stream(self._endpoint, self._endpoint.spatial_input_port, self._connect_timeout) as sock:
                        self._track_socket(sock)
                        try:
                            stream = DeadlineReader(sock, max(5.0, self._connect_timeout))
                            greeting = stream.readline()
                            if greeting and greeting != POSE_GREETING:
                                self.get_logger().warning(f"Unexpected SI greeting: {greeting!r}")
                            self.get_logger().info("Connected to HoloLens pose stream.")
                            while not self._stop.is_set():
                                payload = _read_json_line(stream)
                                if payload is None:
                                    break
                                if payload.get("type") == "pose":
                                    self._publish_pose(payload)
                        finally:
                            self._untrack_socket(sock)
                except OSError as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens pose stream unavailable: {exc}")
                except Exception as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens pose stream error: {exc}")
                self._sleep_reconnect()

        def _video_loop(self) -> None:
            while not self._stop.is_set():
                try:
                    with _connect_stream(self._endpoint, self._endpoint.personal_video_port, self._connect_timeout) as sock:
                        self._track_socket(sock)
                        try:
                            stream = DeadlineReader(sock, max(5.0, self._connect_timeout))
                            greeting = stream.readline()
                            if greeting and greeting != VIDEO_GREETING:
                                self.get_logger().warning(f"Unexpected PV greeting: {greeting!r}")
                            self.get_logger().info("Connected to HoloLens PV video stream.")
                            while not self._stop.is_set():
                                header = _read_json_line(stream)
                                if header is None:
                                    break
                                if header.get("type") != "jpeg_frame":
                                    continue
                                length = int(header.get("content_length") or 0)
                                if length <= 0 or length > 8_000_000:
                                    raise ValueError(f"Invalid HoloLens JPEG length: {length}")
                                data = stream.read(length)
                                if len(data) != length:
                                    raise RuntimeError(f"short JPEG frame: {len(data)}/{length}")
                                self._publish_video(header, data)
                        finally:
                            self._untrack_socket(sock)
                except OSError as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens video stream unavailable: {exc}")
                except Exception as exc:
                    if not self._stop.is_set():
                        self.get_logger().warning(f"HoloLens video stream error: {exc}")
                self._sleep_reconnect()

        def _publish_pose(self, payload: dict[str, Any]) -> None:
            stamp = self.get_clock().now().to_msg()
            packet = _tracking_packet(payload, self._map_frame, f"{self._frame_prefix}/head",
                                      stamp, self._tracking_session_id, "unity_head")
            self._head_pose_pub.publish(String(data=json.dumps(packet, allow_nan=False)))
            if not _pose_tracking_valid(payload):
                self._active_pose_revision = None
                with self._pose_history_lock:
                    self._pose_history.clear()
                self._confidence_pub.publish(Float32(data=0.0))
                self._publish_status(0.0)
                return
            revision = payload.get("tracking_origin_revision")
            self._active_pose_revision = revision if type(revision) is int and revision >= 0 else None
            position = _unity_vec_to_ros(payload.get("position") or {})
            head_rotation = _orientation_from_pose(payload)
            source_timestamp = float(payload.get("timestamp") or 0.0)
            if math.isfinite(source_timestamp) and source_timestamp > 0.0:
                sample = TimedCameraPose(
                    timestamp=source_timestamp,
                    arrival_monotonic=time.monotonic(),
                    position=(float(position[0]), float(position[1]), float(position[2])),
                    orientation=head_rotation,
                )
                with self._pose_history_lock:
                    self._pose_history.append(sample)
            base_rotation = _yaw_to_quaternion(_yaw_from_pose(payload))
            camera_rotation = _quaternion_multiply(_quaternion_inverse(base_rotation), head_rotation)
            base_z, relative_camera_z = _pose_heights(
                float(position[2]), self._camera_height, self._floor_height, self._pose_origin)
            base_position = (float(position[0]), float(position[1]), base_z)

            base_tf = TransformStamped()
            base_tf.header.stamp = stamp
            base_tf.header.frame_id = self._map_frame
            base_tf.child_frame_id = self._base_frame
            base_tf.transform.translation.x = float(position[0])
            base_tf.transform.translation.y = float(position[1])
            base_tf.transform.translation.z = base_z
            base_tf.transform.rotation.x = base_rotation[0]
            base_tf.transform.rotation.y = base_rotation[1]
            base_tf.transform.rotation.z = base_rotation[2]
            base_tf.transform.rotation.w = base_rotation[3]

            camera_tf = TransformStamped()
            camera_tf.header.stamp = stamp
            camera_tf.header.frame_id = self._base_frame
            camera_tf.child_frame_id = self._camera_frame
            camera_tf.transform.translation.z = relative_camera_z
            camera_tf.transform.rotation.x = camera_rotation[0]
            camera_tf.transform.rotation.y = camera_rotation[1]
            camera_tf.transform.rotation.z = camera_rotation[2]
            camera_tf.transform.rotation.w = camera_rotation[3]

            self._tf_broadcaster.sendTransform([base_tf, camera_tf])
            confidence = float(payload.get("confidence", 1.0))
            self._last_confidence = confidence
            self._confidence_pub.publish(Float32(data=max(0.0, min(1.0, confidence))))
            self._pose_count += 1
            self._last_pose_time = time.monotonic()
            if not args.no_hand_tf:
                _queue_latest(self._hand_samples, (payload, stamp, base_position, base_rotation, time.monotonic()))

        def _hand_loop(self):
            while not self._stop.is_set():
                try:
                    sample = self._hand_samples.get(timeout=.2)
                except queue.Empty:
                    continue
                if time.monotonic() - sample[-1] > .25:
                    continue
                try:
                    self._publish_hands(*sample[:-1])
                except Exception as exc:
                    self.get_logger().warning(f"HoloLens hand TF error: {exc}")

        def _publish_hands(self, payload, stamp, base_position, base_rotation):
            transforms = []
            inverse_base_rotation = _quaternion_inverse(base_rotation)
            for hand in _hands_from_pose(payload):
                handedness = hand["hand"]
                hand_position = hand["position"]
                hand_rotation = hand["rotation"]
                hand_frame = self._hand_frames.get(handedness)
                if not hand_frame:
                    continue
                relative_position = (
                    float(hand_position[0]) - base_position[0],
                    float(hand_position[1]) - base_position[1],
                    float(hand_position[2]) - base_position[2],
                )
                local_position = _rotate_vector(inverse_base_rotation, relative_position)
                local_rotation = _quaternion_multiply(inverse_base_rotation, hand_rotation)

                hand_tf = TransformStamped()
                hand_tf.header.stamp = stamp
                hand_tf.header.frame_id = self._base_frame
                hand_tf.child_frame_id = hand_frame
                hand_tf.transform.translation.x = local_position[0]
                hand_tf.transform.translation.y = local_position[1]
                hand_tf.transform.translation.z = local_position[2]
                hand_tf.transform.rotation.x = local_rotation[0]
                hand_tf.transform.rotation.y = local_rotation[1]
                hand_tf.transform.rotation.z = local_rotation[2]
                hand_tf.transform.rotation.w = local_rotation[3]
                transforms.append(hand_tf)

                inverse_hand_rotation = _quaternion_inverse(hand_rotation)
                for joint in hand.get("joints", []):
                    joint_name = _normalize_hand_joint_name(joint.get("name"))
                    if not joint_name:
                        continue
                    joint_position = joint["position"]
                    joint_rotation = joint["rotation"]
                    joint_relative_position = (
                        float(joint_position[0]) - float(hand_position[0]),
                        float(joint_position[1]) - float(hand_position[1]),
                        float(joint_position[2]) - float(hand_position[2]),
                    )
                    joint_local_position = _rotate_vector(inverse_hand_rotation, joint_relative_position)
                    joint_local_rotation = _quaternion_multiply(inverse_hand_rotation, joint_rotation)

                    joint_tf = TransformStamped()
                    joint_tf.header.stamp = stamp
                    joint_tf.header.frame_id = hand_frame
                    joint_tf.child_frame_id = f"{hand_frame}/{joint_name}"
                    joint_tf.transform.translation.x = joint_local_position[0]
                    joint_tf.transform.translation.y = joint_local_position[1]
                    joint_tf.transform.translation.z = joint_local_position[2]
                    joint_tf.transform.rotation.x = joint_local_rotation[0]
                    joint_tf.transform.rotation.y = joint_local_rotation[1]
                    joint_tf.transform.rotation.z = joint_local_rotation[2]
                    joint_tf.transform.rotation.w = joint_local_rotation[3]
                    transforms.append(joint_tf)

                    joint_base_relative_position = (
                        float(joint_position[0]) - base_position[0],
                        float(joint_position[1]) - base_position[1],
                        float(joint_position[2]) - base_position[2],
                    )
                    joint_base_local_position = _rotate_vector(
                        inverse_base_rotation,
                        joint_base_relative_position,
                    )
                    joint_base_local_rotation = _quaternion_multiply(inverse_base_rotation, joint_rotation)

                    flat_joint_tf = TransformStamped()
                    flat_joint_tf.header.stamp = stamp
                    flat_joint_tf.header.frame_id = self._base_frame
                    flat_joint_tf.child_frame_id = f"{self._frame_prefix}/{handedness}_hand_{joint_name}"
                    flat_joint_tf.transform.translation.x = joint_base_local_position[0]
                    flat_joint_tf.transform.translation.y = joint_base_local_position[1]
                    flat_joint_tf.transform.translation.z = joint_base_local_position[2]
                    flat_joint_tf.transform.rotation.x = joint_base_local_rotation[0]
                    flat_joint_tf.transform.rotation.y = joint_base_local_rotation[1]
                    flat_joint_tf.transform.rotation.z = joint_base_local_rotation[2]
                    flat_joint_tf.transform.rotation.w = joint_base_local_rotation[3]
                    transforms.append(flat_joint_tf)

            if transforms:
                self._hand_tf_broadcaster.sendTransform(transforms)

        def _on_guidance_message(self, kind: str, msg: String) -> None:
            command = self._build_guidance_command(kind, msg.data)
            self._enqueue_control(command)
            self._guidance_state_pub.publish(
                String(
                    data=json.dumps(
                        {
                            "teammate": args.name,
                            "state": "guidance_sent",
                            "kind": kind,
                            "commandId": command["commandId"],
                            "label": command["label"],
                        }
                    )
                )
            )
            self._guidance_response_pub.publish(
                String(
                    data=json.dumps(
                        {
                            "teammate": args.name,
                            "response": "queued",
                            "kind": kind,
                            "commandId": command["commandId"],
                        }
                    )
                )
            )

        def _build_guidance_command(self, kind: str, data: str) -> dict[str, Any]:
            payload: dict[str, Any] = {}
            if data:
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:
                    payload = {}

            command_id = str(
                payload.get("commandId")
                or payload.get("command_id")
                or payload.get("id")
                or f"{kind}-{time.time_ns()}"
            )
            label = str(payload.get("label") or payload.get("text") or payload.get("message") or data or kind)
            position = (
                payload.get("localPosition")
                or payload.get("local_position")
                or payload.get("position")
                or payload.get("target")
                or {}
            )
            if not isinstance(position, dict):
                position = {}
            return {
                "type": "guidance",
                "commandId": command_id,
                "label": label[:80],
                "x": float(position.get("x", 0.0)),
                "y": float(position.get("y", 0.0)),
                "z": float(position.get("z", 2.0)),
                "source": f"ros:{kind}",
                "timestamp": time.time(),
            }

        def _publish_video(self, header: dict[str, Any], data: bytes) -> None:
            msg = CompressedImage()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = self._camera_frame
            msg.format = "jpeg"
            msg.data = data
            width = int(header.get("width") or 0)
            height = int(header.get("height") or 0)
            self._publish_frame_camera_pose(msg, header)
            if width > 0 and height > 0:
                if width != self._last_video_width or height != self._last_video_height:
                    self.get_logger().info(f"HoloLens PV frame size: {width}x{height}")
                self._last_video_width = width
                self._last_video_height = height
                self._publish_camera_info(msg, width, height, header)
            # Pose and calibration are deliberately published first with this
            # exact timestamp so downstream capture can persist one atomic frame.
            self._fpv_pub.publish(msg)
            self._publish_raw_image(msg, data)
            self._video_count += 1
            self._last_video_time = time.monotonic()
            self._publish_status()

        def _publish_frame_camera_pose(self, compressed: CompressedImage, header: dict[str, Any]) -> None:
            camera_payload = header.get("camera_pose")
            native = dict(camera_payload) if isinstance(camera_payload, dict) else {}
            native.update(timestamp=header.get("timestamp"),
                          tracking_origin_revision=header.get("tracking_origin_revision"),
                          tracking_valid=(header.get("tracking_valid") is True and
                                          _camera_pose_epoch_matches(header, self._active_pose_revision)))
            packet = _tracking_packet(native, self._map_frame, f"{self._frame_prefix}/pv_camera",
                                      compressed.header.stamp, self._tracking_session_id,
                                      "hololens_locatable_pv_camera")
            self._pv_pose_pub.publish(String(data=json.dumps(packet, allow_nan=False)))
            if not _camera_pose_epoch_matches(header, self._active_pose_revision):
                return
            position = None
            orientation = None
            if bool(header.get("tracking_valid")):
                camera_pose = header.get("camera_pose")
                if isinstance(camera_pose, dict) and isinstance(camera_pose.get("position"), dict):
                    position = _unity_vec_to_ros(camera_pose["position"])
                    orientation = _orientation_from_pose(camera_pose)
                    self._video_native_pose_count += 1

            if position is None or orientation is None:
                if not _camera_pose_fallback_allowed(header):
                    return
                source_timestamp = float(header.get("timestamp") or 0.0)
                with self._pose_history_lock:
                    history = tuple(self._pose_history)
                nearest = _nearest_camera_pose(history, source_timestamp)
                if nearest is None and history:
                    # Current HORUS Field builds timestamp WinMD PV callbacks
                    # with Unix time while the pose stream uses Unity uptime.
                    # Until the corrected app is redeployed, use the newest
                    # pose received on this same relay clock, but only while it
                    # is fresh enough to represent this video frame.
                    latest = history[-1]
                    if time.monotonic() - latest.arrival_monotonic <= 0.10:
                        nearest = latest
                if nearest is None:
                    return
                position = nearest.position
                orientation = nearest.orientation
                self._video_fallback_pose_count += 1

            pose = PoseStamped()
            pose.header.stamp = compressed.header.stamp
            pose.header.frame_id = self._map_frame
            pose.pose.position.x = float(position[0])
            pose.pose.position.y = float(position[1])
            pose.pose.position.z = float(position[2])
            pose.pose.orientation.x = orientation[0]
            pose.pose.orientation.y = orientation[1]
            pose.pose.orientation.z = orientation[2]
            pose.pose.orientation.w = orientation[3]
            self._camera_pose_pub.publish(pose)
            self._video_pose_count += 1

        def _publish_camera_info(
            self,
            compressed: CompressedImage,
            width: int,
            height: int,
            frame_header: dict[str, Any],
        ) -> None:
            intrinsics = frame_header.get("camera_intrinsics")
            calibrated = isinstance(intrinsics, dict)
            if calibrated:
                source_width = max(1, int(intrinsics.get("width") or width))
                source_height = max(1, int(intrinsics.get("height") or height))
                scale_x = float(width) / float(source_width)
                scale_y = float(height) / float(source_height)
                fx = float(intrinsics.get("fx") or 0.0) * scale_x
                fy = float(intrinsics.get("fy") or 0.0) * scale_y
                cx = float(intrinsics.get("cx") or 0.0) * scale_x
                cy = float(intrinsics.get("cy") or 0.0) * scale_y
                distortion = [
                    float(intrinsics.get("k1") or 0.0),
                    float(intrinsics.get("k2") or 0.0),
                    float(intrinsics.get("p1") or 0.0),
                    float(intrinsics.get("p2") or 0.0),
                    float(intrinsics.get("k3") or 0.0),
                ]
            else:
                fx = float(width) * 0.8
                fy = float(width) * 0.8
                cx = float(width) * 0.5
                cy = float(height) * 0.5
                distortion = [0.0, 0.0, 0.0, 0.0, 0.0]

            info = CameraInfo()
            info.header = compressed.header
            info.width = int(width)
            info.height = int(height)
            info.distortion_model = "plumb_bob" if calibrated else "synthetic_pinhole"
            info.d = distortion
            info.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
            info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
            info.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
            self._camera_info_pub.publish(info)

        def _publish_raw_image(self, compressed: CompressedImage, data: bytes) -> None:
            if self._raw_image_pub is None or self._cv2 is None or self._np is None:
                return
            encoded = self._np.frombuffer(data, dtype=self._np.uint8)
            bgr = self._cv2.imdecode(encoded, self._cv2.IMREAD_COLOR)
            if bgr is None:
                self.get_logger().warning("Dropping HoloLens frame because JPEG decode returned no image")
                return
            rgb = self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2RGB)
            rgb = self._np.ascontiguousarray(rgb)

            raw = Image()
            raw.header = compressed.header
            raw.height = int(rgb.shape[0])
            raw.width = int(rgb.shape[1])
            raw.encoding = "rgb8"
            raw.is_bigendian = 0
            raw.step = raw.width * 3
            raw.data = rgb.tobytes()
            self._raw_image_pub.publish(raw)

        def _publish_status(self, confidence: float | None = None) -> None:
            if confidence is not None:
                self._last_confidence = confidence
            if self._stop.is_set() or not self._status_lock.acquire(blocking=False):
                return
            try:
                self._publish_status_locked()
            finally:
                self._status_lock.release()

        def _publish_status_locked(self) -> None:
            now = time.monotonic()
            if now - self._last_status_time < 0.5:
                return
            self._last_status_time = now
            confidence = self._last_confidence
            tracks = {}
            for name, last, count in (("pose", self._last_pose_time, self._pose_count),
                                      ("video", self._last_video_time, self._video_count)):
                age = None if last is None else now - last
                tracks[name] = {"received_frames": count, "frame_age_sec": age,
                                "state": "waiting" if age is None else "flowing" if age < 3 else "stale"}
            state = "live" if tracks["pose"]["state"] == "flowing" else tracks["pose"]["state"]
            write_status(os.environ.get("HORUS_MEDIA_STATUS_FILE"),
                         {"role": "teammate", "state": state, "tracks": tracks})
            self._status_pub.publish(
                String(
                    data=json.dumps(
                        {
                            "teammate": args.name,
                            "state": state,
                            "stream_health": tracks,
                            "wearable": "hololens2",
                            "localization_confidence": round(float(confidence), 3),
                            "pose_frames": self._pose_count,
                            "video_frames": self._video_count,
                            "video_frames_with_locatable_pose": self._video_native_pose_count,
                            "video_frames_with_color_pose": self._video_pose_count,
                            "video_frames_with_native_pv_pose": self._video_native_pose_count,
                            "video_frames_with_head_pose_fallback": self._video_fallback_pose_count,
                            "video_width": self._last_video_width,
                            "video_height": self._last_video_height,
                            "video_profile_ack": self._last_video_profile_ack,
                            "view_frame": self._camera_frame,
                            "pose_origin": self._pose_origin,
                            "camera_height_m": round(self._camera_height, 3),
                            "floor_height_m": round(self._floor_height, 3),
                            "source": "horus_field",
                        }
                    )
                )
            )

        def _sleep_reconnect(self) -> None:
            deadline = time.monotonic() + self._reconnect_delay
            while not self._stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)

    rclpy.init()
    node = LiveHoloLensFieldTeammateRelay()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.stop()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    endpoint = HoloLensRelayEndpoint(
        host=args.hololens_host,
        personal_video_port=int(args.pv_port),
        spatial_input_port=int(args.spatial_input_port),
        unity_message_queue_port=int(args.umq_port),
    )
    contract = _topic_contract(args.name)
    video_profile_command = _build_video_profile_command(args)
    profile_height = max(1.35, min(2.10, float(args.profile_height)))
    camera_height = float(args.camera_height or 0.0)
    if camera_height <= 0.0:
        camera_height = profile_height * DEFAULT_CAMERA_HEIGHT_RATIO
    frame_prefix = contract["base_frame"].rsplit("/", 1)[0]
    pose_contract = {
        "pose_origin": args.pose_origin,
        "profile_height_m": round(profile_height, 3),
        "camera_height_m": round(max(1.10, min(2.05, camera_height)), 3),
        "floor_height_m": float(args.floor_height),
        "base_frame": contract["base_frame"],
        "camera_frame": contract["camera_frame"],
        "hand_frames": {
            "left": f"{frame_prefix}/left_hand",
            "right": f"{frame_prefix}/right_hand",
        },
    }

    if args.dry_run:
        print(
            json.dumps(
                {
                    "endpoint": asdict(endpoint),
                    "video_profile_command": video_profile_command,
                    "pose_contract": pose_contract,
                    "ros_contract": contract,
                },
                indent=2,
            )
        )
        return 0

    if args.check_endpoint:
        result = _check_endpoint(endpoint, float(args.connect_timeout))
        print(json.dumps({"endpoint": asdict(endpoint), "reachable": result}, indent=2))
        return 0 if all(result.values()) else 2

    print(
        json.dumps(
            {
                "endpoint": asdict(endpoint),
                "video_profile_command": video_profile_command,
                "pose_contract": pose_contract,
                "ros_contract": contract,
            },
            indent=2,
        )
    )
    return _run_live(args, endpoint)


if __name__ == "__main__":
    sys.exit(main())
