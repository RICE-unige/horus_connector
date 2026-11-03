#!/usr/bin/env python3
"""Check the actual media interpreter and cache per-instance codec probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import logging
from pathlib import Path
from native_runtime import activate


def validate(mode, role, need_ros, root=None):
    activate()
    if sys.version_info < (3, 10):
        raise RuntimeError("HORUS connector requires Python 3.10 or newer")
    from websockets.sync.client import connect  # noqa: F401
    import json5  # noqa: F401
    if need_ros:
        import rclpy  # noqa: F401
        from sensor_msgs.msg import Image  # noqa: F401
    if role == "cloud":
        return
    if role == "teammate":
        from tf2_ros import TransformBroadcaster  # noqa: F401
        if os.environ.get("FIELD_TEAMMATE_RAW_IMAGE") == "1":
            import cv2  # noqa: F401
            import numpy  # noqa: F401
    elif mode == "jpeg":
        import aiortc  # noqa: F401
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    elif role != "cloud":
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        gi.require_version("GstRtp", "1.0")
        from gi.repository import Gst, GstRtp, GstSdp, GstWebRTC  # noqa: F401
        Gst.init(None)
        for directory in activate():
            Gst.Registry.get().scan_path(str(directory))
        if Gst.version()[:2] < (1, 20):
            raise RuntimeError("H.264 recovery requires GStreamer 1.20 or newer")
        if Gst.version()[:2] < (1, 22):
            from webrtc_streams import load_streams
            if len(load_streams(root or Path(__file__).resolve().parents[1], role)) > 1:
                raise RuntimeError("Bundled camera identity requires GStreamer 1.22 or newer")
        for name in ("webrtcbin", "nicesrc", "nicesink", "rtph264depay", "rtph264pay", "videoscale", "videoconvert", "videobox"):
            if Gst.ElementFactory.find(name) is None:
                raise RuntimeError(f"Missing GStreamer element: {name}")


def profile_fingerprint(root, role):
    keys = ("GST_PLUGIN_PATH", "GST_PLUGIN_SYSTEM_PATH", "WEBRTC_ENCODER_PREFERENCE",
            "WEBRTC_DECODER_PREFERENCE", "WEBRTC_H264_KEY_INT_MAX", "VIDEO_BITRATE_KBIT",
            "WEBRTC_VIDEO_WIDTH", "WEBRTC_VIDEO_HEIGHT", "WEBRTC_VIDEO_FPS")
    payload = {key: os.environ.get(key, "") for key in keys}
    payload.update(role=role, interpreter=sys.executable,
                   script=hashlib.sha256((root / "scripts/detect_video_stack.sh").read_bytes()).hexdigest())
    payload["python_runtime"] = (sys.version, str(Path(sys.executable).resolve()),
                                 Path(sys.executable).stat().st_mtime_ns)
    payload["python_environment"] = str(Path(sys.prefix) / "pyvenv.cfg")
    configuration = Path(sys.prefix) / "pyvenv.cfg"
    if configuration.exists():
        payload["python_environment"] += configuration.read_text()
    payload["stream_profiles"] = stream_profiles(root, role)
    result = subprocess.run(["gst-inspect-1.0", "--version"], capture_output=True, timeout=5, check=True)
    payload["gstreamer"] = result.stdout.decode(errors="replace")
    payload["devices"] = sorted(str(path) for directory in ("/dev/dri", "/dev")
                                for path in Path(directory).glob("render*" if directory.endswith("dri") else "video*"))
    payload["drivers"] = {}
    for path in Path("/sys/module").glob("*/version"):
        if path.parent.name in {"nvidia", "amdgpu", "i915"}:
            try:
                payload["drivers"][str(path)] = path.read_text().strip()
            except OSError:
                payload["drivers"][str(path)] = "unavailable"
    payload["codec_plugins"] = []
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    for name in ("webrtc", "nvcodec", "nvvideo4linux2", "va", "vaapi", "x264", "openh264", "libav", "videobox"):
        plugin = Gst.Registry.get().find_plugin(name)
        if plugin is not None:
            filename = plugin.get_filename()
            payload["codec_plugins"].append((name, plugin.get_version(), filename,
                Path(filename).stat().st_mtime_ns if filename and Path(filename).exists() else None))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def stream_profiles(root, role):
    from webrtc_streams import load_streams
    return sorted({(stream["width"], stream["height"], stream["fps"])
                   for stream in load_streams(root, role)})


def ensure_profile(root, path, role):
    fingerprint = profile_fingerprint(root, role)
    marker = path.with_suffix(path.suffix + ".fingerprint")
    if path.is_file() and marker.is_file() and marker.read_text() == fingerprint:
        path.with_suffix(path.suffix + ".invalid").unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        profiles = stream_profiles(root, role)
        probe_env = dict(os.environ)
        if profiles:
            probe_env["WEBRTC_PROBE_PROFILES_TSV"] = "\n".join("\t".join(map(str, profile)) for profile in profiles)
        subprocess.run([str(root / "scripts/detect_video_stack.sh"), "--role", role,
                        "--out", str(temporary)], env=probe_env, timeout=180, check=True,
                       stdout=subprocess.DEVNULL)
        # Detection proves codec availability, not the number of streams the device can sustain.
        content = temporary.read_text()
        required = "GST_H264_ENCODER_NAME" if role == "robot" else "GST_H264_DECODER_NAME"
        if f"{required}=''" in content:
            raise RuntimeError(f"No working {required}; existing cached profile was preserved")
        temporary.replace(path)
        marker.write_text(fingerprint)
        path.with_suffix(path.suffix + ".invalid").unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def invalidate_profile(path, element=None):
    """Invalidate only profiles owned by our probe; never rewrite a manual profile."""
    if not path:
        return False
    if element is not None:
        factory = element.get_factory()
        category = factory.get_metadata("klass") if factory is not None else ""
        if not category or not any(kind in category for kind in ("Encoder", "Decoder")):
            return False
    path = Path(path)
    marker = path.with_suffix(path.suffix + ".fingerprint")
    invalid = path.with_suffix(path.suffix + ".invalid")
    try:
        if marker.exists():
            marker.replace(invalid)
        return invalid.exists()
    except OSError:
        logging.getLogger(__name__).exception("Could not invalidate failed codec profile %s", path)
        return False


def recover_profile(args, role):
    if args.profile and Path(args.profile).with_suffix(Path(args.profile).suffix + ".invalid").exists():
        root = Path(args.root).resolve()
        validate("h264", role, False, root)
        ensure_profile(root, Path(args.profile), role)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("h264", "jpeg"), default="h264")
    parser.add_argument("--role", choices=("robot", "machine", "teammate", "cloud"), required=True)
    parser.add_argument("--need-ros", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    try:
        validate(args.mode, args.role, args.need_ros, args.root)
        if args.profile:
            ensure_profile(args.root, args.profile, args.role)
    except Exception as exc:
        print(f"Media preflight failed for {sys.executable}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
