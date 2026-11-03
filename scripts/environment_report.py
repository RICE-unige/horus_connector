"""Non-secret software and capability inventory for reproducible connector reports."""

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def report(root):
    packages = {}
    for name in ("websockets", "json5", "aiortc", "numpy", "Pillow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    bridge = Path(os.environ.get("ZENOH_BRIDGE_BINARY") or root / "zenoh-bridge-ros2dds")
    provenance = bridge.with_name(bridge.name + ".source.json")
    try:
        build = json.loads(provenance.read_text())
    except (OSError, ValueError):
        build = None
    return {"schema": 1, "platform": platform.platform(), "architecture": platform.machine(),
            "python": sys.version.split()[0], "python_executable": sys.executable, "packages": packages,
            "ros_distribution": os.environ.get("ROS_DISTRO"), "ros_domain": os.environ.get("ROS_DOMAIN_ID", "0"),
            "rmw": os.environ.get("RMW_IMPLEMENTATION", "system_default"),
            "zenoh": command([str(bridge), "--version"]), "zenoh_source": build,
            "priority_links": os.environ.get("HORUS_ZENOH_TRAFFIC_LANES", "0") == "1",
            "bulk_bytes_per_second": os.environ.get("HORUS_ZENOH_BULK_BYTES_PER_SECOND", "0"),
            "gstreamer": command(["gst-inspect-1.0", "--version"]),
            "gpu": command(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]),
            "revision": command(["git", "-C", str(root), "rev-parse", "HEAD"]),
            "branch": command(["git", "-C", str(root), "branch", "--show-current"]),
            "dirty": bool(command(["git", "-C", str(root), "status", "--porcelain"]))}


if __name__ == "__main__":
    print(json.dumps(report(Path(__file__).resolve().parents[1]), indent=2))
