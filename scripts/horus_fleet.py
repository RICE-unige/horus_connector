#!/usr/bin/env python3
"""Launch multiple HORUS rooms from one fleet config."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List


ENV_MAP = {
    "role": "HORUS_ROLE",
    "room": "HORUS_ROOM",
    "topology": "HORUS_TOPOLOGY",
    "cloud_ip": "HORUS_CLOUD_IP",
    "machine_ip": "HORUS_MACHINE_IP",
    "signal_ip": "HORUS_WEBRTC_SIGNAL_IP",
    "namespace": "ZENOH_NAMESPACE",
    "zenoh_enabled": "HORUS_ZENOH_ENABLED",
    "ros_domain_id": "ROS_DOMAIN_ID",
    "video_width": "WEBRTC_VIDEO_WIDTH",
    "video_height": "WEBRTC_VIDEO_HEIGHT",
    "video_fps": "WEBRTC_VIDEO_FPS",
    "image_input_topic": "WEBRTC_ROS_IMAGE_INPUT_TOPIC",
    "image_output_topic": "WEBRTC_ROS_IMAGE_OUTPUT_TOPIC",
    "image_qos": "WEBRTC_ROS_IMAGE_QOS",
    "ice_servers": "WEBRTC_ICE_SERVERS",
    "video_output": "WEBRTC_VIDEO_OUTPUT",
    "video_sink": "WEBRTC_VIDEO_SINK",
    "video_bitrate_kbit": "VIDEO_BITRATE_KBIT",
    "cmd_topic": "ROS_CMD_TOPIC",
    "cmd_rate": "CMD_RATE",
    "cmd_linear_x": "CMD_LINEAR_X",
    "cmd_angular_z": "CMD_ANGULAR_Z",
}


def load_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def write_env(path: Path, values: Dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={quote(value)}\n" for key, value in sorted(values.items())), encoding="utf-8")


def load_config(path: Path) -> List[Dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        members = raw.get("members") or raw.get("robots") or []
    else:
        members = raw
    if not isinstance(members, list):
        raise ValueError("fleet config must contain a members list")
    normalized: List[Dict[str, object]] = []
    for item in members:
        if not isinstance(item, dict):
            raise ValueError("each fleet member must be an object")
        name = str(item.get("name") or item.get("room") or "").strip()
        if not name:
            raise ValueError("each fleet member needs a name or room")
        normalized.append(item)
    return normalized


def member_name(member: Dict[str, object]) -> str:
    raw = str(member.get("name") or member.get("room") or "member")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)


def member_env(base: Dict[str, str], member: Dict[str, object], role_override: str) -> Dict[str, str]:
    values = dict(base)
    for source, target in ENV_MAP.items():
        if source in member and member[source] is not None:
            values[target] = str(member[source])
    if role_override:
        values["HORUS_ROLE"] = role_override
    if "HORUS_ROOM" not in values or not values["HORUS_ROOM"]:
        values["HORUS_ROOM"] = str(member.get("room") or member_name(member))
    return values


def run_horus(root: Path, env_file: Path, run_dir: Path, args: Iterable[str]) -> int:
    env = os.environ.copy()
    env["HORUS_ENV"] = str(env_file)
    env["HORUS_RUN_DIR"] = str(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "launcher.log"
    with log.open("ab") as stream:
        proc = subprocess.run([str(root / "horus"), *args], cwd=root, env=env, stdout=stream, stderr=stream, check=False)
    return proc.returncode


def command_launch(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    base_env = load_env(Path(args.base_env).resolve())
    members = load_config(Path(args.config).resolve())
    failed = 0
    for index, member in enumerate(members):
        name = member_name(member)
        env_file = root / ".run" / "fleet" / f"{name}.env"
        run_dir = root / ".run" / "fleet" / name
        role = args.role or str(member.get("role") or base_env.get("HORUS_ROLE") or "machine")
        values = member_env(base_env, member, role)
        if role == "machine":
            if "namespace" not in member:
                values["ZENOH_NAMESPACE"] = ""
            values["HORUS_ZENOH_ENABLED"] = str(member.get("zenoh_enabled", "1" if index == 0 else "0"))
        write_env(env_file, values)
        code = run_horus(root, env_file, run_dir, ["launch", role, "--no-monitor"])
        status = "started" if code == 0 else f"failed ({code})"
        print(f"{name}: {status} role={role} env={env_file}")
        failed += 1 if code else 0
    return 1 if failed else 0


def command_stop(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    members = load_config(Path(args.config).resolve())
    failed = 0
    for member in members:
        name = member_name(member)
        env_file = root / ".run" / "fleet" / f"{name}.env"
        run_dir = root / ".run" / "fleet" / name
        if not env_file.exists():
            print(f"{name}: skipped, no generated env")
            continue
        code = run_horus(root, env_file, run_dir, ["stop"])
        print(f"{name}: stopped" if code == 0 else f"{name}: stop failed ({code})")
        failed += 1 if code else 0
    return 1 if failed else 0


def command_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    members = load_config(Path(args.config).resolve())
    for member in members:
        name = member_name(member)
        env_file = root / ".run" / "fleet" / f"{name}.env"
        run_dir = root / ".run" / "fleet" / name
        if not env_file.exists():
            print(f"{name}: no generated env; run fleet launch first")
            continue
        env = os.environ.copy()
        env["HORUS_ENV"] = str(env_file)
        env["HORUS_RUN_DIR"] = str(run_dir)
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "horus_monitor.py"), "--root", str(root), "--env", str(env_file), "--json"],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        print(f"\n{name}")
        print(proc.stdout.strip() if proc.stdout else proc.stderr.strip())
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HORUS Connector fleet launcher")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--base-env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    sub = parser.add_subparsers(dest="command", required=True)

    launch = sub.add_parser("launch", help="launch all fleet members")
    launch.add_argument("config")
    launch.add_argument("--role", choices=["robot", "machine"], default="")
    launch.set_defaults(func=command_launch)

    stop = sub.add_parser("stop", help="stop all fleet members")
    stop.add_argument("config")
    stop.set_defaults(func=command_stop)

    status = sub.add_parser("status", help="print JSON status for each member")
    status.add_argument("config")
    status.set_defaults(func=command_status)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
