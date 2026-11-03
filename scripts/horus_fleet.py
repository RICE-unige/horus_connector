#!/usr/bin/env python3
"""Launch multiple HORUS rooms from one fleet config."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List
from connector_config import load_env_file, quote


ENV_MAP = {
    "role": "HORUS_ROLE",
    "room": "HORUS_ROOM",
    "session_room": "HORUS_WEBRTC_SESSION_ROOM",
    "streams_config": "HORUS_STREAMS_CONFIG",
    "topology": "HORUS_TOPOLOGY",
    "cloud_ip": "HORUS_CLOUD_IP",
    "machine_ip": "HORUS_MACHINE_IP",
    "signal_ip": "HORUS_WEBRTC_SIGNAL_IP",
    "namespace": "ZENOH_NAMESPACE",
    "zenoh_enabled": "HORUS_ZENOH_ENABLED",
    "zenoh_port": "ZENOH_PORT",
    "ros_domain_id": "ROS_DOMAIN_ID",
    "dds_interface": "HORUS_DDS_INTERFACE",
    "rmw_implementation": "RMW_IMPLEMENTATION",
    "cyclonedds_uri": "CYCLONEDDS_URI",
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
    return load_env_file(path)


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
    names = set()
    rooms = set()
    output_topics = set()
    for item in members:
        if not isinstance(item, dict):
            raise ValueError("each fleet member must be an object")
        name = str(item.get("name") or item.get("room") or "").strip()
        if not name:
            raise ValueError("each fleet member needs a name or room")
        identity = member_name(item)
        if identity in {".", "..", "signaling", "launcher.lock"} or identity in names:
            raise ValueError(f"duplicate or invalid fleet instance: {identity}")
        names.add(identity)
        if "zenoh_enabled" in item and item["zenoh_enabled"] not in (False, True, 0, 1, "0", "1"):
            raise ValueError("zenoh_enabled must be a boolean or 0/1")
        if item.get("role", "machine") not in {"robot", "machine"}:
            raise ValueError("fleet role must be robot or machine")
        room = str(item.get("session_room") or item.get("room") or identity)
        if room in rooms:
            raise ValueError(f"duplicate fleet signaling room: {room}")
        rooms.add(room)
        topic = item.get("image_output_topic")
        if topic and topic in output_topics:
            raise ValueError(f"duplicate fleet camera output: {topic}")
        if topic:
            output_topics.add(topic)
        normalized.append(item)
    return normalized


def member_name(member: Dict[str, object]) -> str:
    raw = str(member.get("name") or member.get("room") or "member")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)


def member_env(base: Dict[str, str], member: Dict[str, object], role_override: str) -> Dict[str, str]:
    values = dict(base)
    for source, target in ENV_MAP.items():
        if source in member and member[source] is not None:
            value = member[source]
            values[target] = str(int(value)) if isinstance(value, bool) else str(value)
    if role_override:
        values["HORUS_ROLE"] = role_override
    values["HORUS_ROOM"] = str(member.get("room") or member_name(member))
    values["HORUS_WEBRTC_SESSION_ROOM"] = str(member.get("session_room") or member.get("room") or member_name(member))
    return values


def graph_identity(values):
    return tuple(values.get(key, default) for key, default in (
        ("ROS_DOMAIN_ID", "0"), ("HORUS_DDS_INTERFACE", ""), ("CYCLONEDDS_URI", ""),
        ("RMW_IMPLEMENTATION", ""), ("ROS_DISCOVERY_SERVER", ""),
        ("ROS_LOCALHOST_ONLY", ""), ("ROS_AUTOMATIC_DISCOVERY_RANGE", ""), ("ROS_STATIC_PEERS", ""),
        ("ZENOH_CONFIG", ""), ("HORUS_ROUTES_CONFIG", ""),
        ("ZENOH_NAMESPACE", ""), ("HORUS_TOPOLOGY", "hub"),
        ("HORUS_CLOUD_IP", ""), ("HORUS_MACHINE_IP", ""), ("ZENOH_PORT", "7447")))


def run_horus(root: Path, env_file: Path, run_dir: Path, args: Iterable[str], receipt: Path | None = None) -> int:
    env = os.environ.copy()
    env["HORUS_ENV"] = str(env_file)
    env["HORUS_RUN_DIR"] = str(run_dir)
    if receipt is not None:
        env["HORUS_LAUNCH_RECEIPT"] = str(receipt)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "launcher.log"
    with log.open("ab") as stream:
        proc = subprocess.run([str(root / "horus"), *args], cwd=root, env=env, stdout=stream, stderr=stream, check=False, timeout=240)
    return proc.returncode


def rollback_launches(root: Path, launches):
    for env_file, run_dir, receipt in reversed(launches):
        if not receipt.exists():
            continue
        services = []
        for row in receipt.read_text().splitlines():
            name, pid, start = row.split("\t")
            pid_file = run_dir / f"{name}.pid"
            start_file = run_dir / f"{name}.pid.start"
            if pid_file.exists() and start_file.exists() and pid_file.read_text().strip() == pid and start_file.read_text().strip() == start:
                services.append(name)
        if services:
            try:
                run_horus(root, env_file, run_dir, ["stop", "--services", *services])
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"Could not complete fleet rollback for {run_dir}: {exc}", file=sys.stderr)


def command_launch(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    base_env = load_env(Path(args.base_env).resolve())
    members = load_config(Path(args.config).resolve())
    prepared = []
    output_topics = set()
    bridged_graphs = set()
    listeners = set()
    from webrtc_streams import load_session, load_streams
    original_env = dict(os.environ)
    for index, member in enumerate(members):
        name = member_name(member)
        env_file = root / ".run" / "fleet" / f"{name}.env"
        run_dir = root / ".run" / "fleet" / name
        role = args.role or str(member.get("role") or base_env.get("HORUS_ROLE") or "machine")
        values = member_env(base_env, member, role)
        if role == "machine":
            if "namespace" not in member:
                values["ZENOH_NAMESPACE"] = ""
            graph = graph_identity(values)
            value = member.get("zenoh_enabled", graph not in bridged_graphs)
            values["HORUS_ZENOH_ENABLED"] = str(int(value)) if isinstance(value, bool) else str(value)
            if values["HORUS_ZENOH_ENABLED"] == "1":
                if graph in bridged_graphs:
                    raise ValueError("two machine bridges on the same graph can loop traffic; use one shared bridge or distinct graph identities")
                if values.get("HORUS_TOPOLOGY", "hub") == "direct":
                    port = values.get("ZENOH_PORT", "7447")
                    if port in listeners:
                        raise ValueError("independent direct DDS graphs require distinct zenoh_port values, also configured on their robot peers")
                    listeners.add(port)
                bridged_graphs.add(graph)
        if "streams_config" in member:
            configured = Path(str(member["streams_config"])).expanduser()
            if not configured.is_absolute():
                configured = Path(args.config).resolve().parent / configured
            values["HORUS_STREAMS_CONFIG"] = str(configured)
        try:
            os.environ.update(values)
            load_session(root, role)
            streams = load_streams(root, role)
            if role == "machine":
                for stream in streams:
                    for field in ("output_topic", "output_camera_info_topic"):
                        topic = stream.get(field)
                        if topic and topic in output_topics:
                            raise ValueError(f"fleet members publish the same camera topic: {topic}")
                        if topic:
                            output_topics.add(topic)
        finally:
            os.environ.clear()
            os.environ.update(original_env)
        prepared.append((name, env_file, run_dir, role, values))
    # Direct fleets use one room-aware listener instead of N servers binding one port.
    shared = any(role == "machine" and values.get("HORUS_TOPOLOGY", "hub") == "direct"
                 for _name, _env_file, _run_dir, role, values in prepared)
    shared_env = root / ".run/fleet/signaling.env"
    shared_run = root / ".run/fleet/signaling"
    started = []
    (root / ".run/fleet").mkdir(parents=True, exist_ok=True)
    receipts = tempfile.TemporaryDirectory(prefix="launch-", dir=root / ".run/fleet")
    def launch(env_file, run_dir, role):
        receipt = Path(receipts.name) / f"{len(started)}.tsv"
        started.append((env_file, run_dir, receipt))
        return run_horus(root, env_file, run_dir, ["launch", role, "--no-monitor"], receipt=receipt)
    try:
        return launch_prepared(root, base_env, prepared, shared, shared_env, shared_run, launch)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Fleet launch failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not getattr(launch, "completed", False):
            rollback_launches(root, started)
        receipts.cleanup()


def launch_prepared(root, base_env, prepared, shared, shared_env, shared_run, launch):
    if shared:
        values = dict(base_env, HORUS_ROLE="cloud", HORUS_ZENOH_ENABLED="0", HORUS_CLOUD_RUN_TURN="0",
                      HORUS_CLOUD_RUN_SIGNALING="1", HORUS_METRICS_PORT="0",
                      HORUS_WEBRTC_RELAY_ALLOW_UNAUTHENTICATED="1")
        write_env(shared_env, values)
        if launch(shared_env, shared_run, "cloud"):
            return 1
    for name, env_file, run_dir, role, values in prepared:
        if shared and role == "machine" and values.get("HORUS_TOPOLOGY", "hub") == "direct":
            values.update(HORUS_WEBRTC_USE_SHARED_SIGNAL="1", HORUS_WEBRTC_SIGNAL_IP="127.0.0.1")
        write_env(env_file, values)
        code = launch(env_file, run_dir, role)
        status = "started" if code == 0 else f"failed ({code})"
        print(f"{name}: {status} role={role} env={env_file}")
        if code:
            return 1
    launch.completed = True
    return 0


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
    shared_env = root / ".run/fleet/signaling.env"
    if shared_env.exists():
        failed += bool(run_horus(root, shared_env, root / ".run/fleet/signaling", ["stop"]))
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
    lock_path = Path(args.root).resolve() / ".run/fleet/launcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another fleet operation is in progress.", file=sys.stderr)
            return 1
        return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
