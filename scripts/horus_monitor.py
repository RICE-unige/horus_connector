#!/usr/bin/env python3
"""Interactive terminal monitor for HORUS connector."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


RESET = "\033[0m"
BOLD = "\033[1m"
FG = {
    "green": "\033[38;5;48m",
    "yellow": "\033[38;5;214m",
    "red": "\033[38;5;203m",
    "blue": "\033[38;5;39m",
    "cyan": "\033[38;5;51m",
    "magenta": "\033[38;5;170m",
    "muted": "\033[38;5;244m",
    "white": "\033[38;5;255m",
    "dark": "\033[38;5;236m",
}
BG = {
    "green": "\033[48;5;22m",
    "yellow": "\033[48;5;58m",
    "red": "\033[48;5;52m",
    "cyan": "\033[48;5;23m",
    "muted": "\033[48;5;236m",
}

MONITOR_ROS_NODES = {"/horus_monitor_topic_probe"}
ROLE_SUMMARIES = {
    "robot": "robot endpoint - sends camera/state, receives cmd_vel",
    "machine": "machine endpoint - receives camera/state, sends cmd_vel",
    "cloud": "cloud hub - routes Zenoh and WebRTC signaling",
}


@dataclass
class ProcessState:
    name: str
    pid: Optional[int]
    running: bool
    etime: str = "-"
    cpu: str = "-"
    mem: str = "-"


@dataclass
class ServiceState:
    title: str
    status: str
    detail: str
    level: str


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def color(text: str, name: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{FG.get(name, '')}{text}{RESET}"


def style(text: str, marker: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{marker}{text}{RESET}"


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def pad_visible(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_len(text))


def load_env_file(path: Path) -> Dict[str, str]:
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


def load_env(root: Path, env_path: Path) -> Dict[str, str]:
    values = load_env_file(env_path)
    values.update(load_env_file(root / ".zenoh_profile.env"))
    values.update(load_env_file(root / ".zenoh_tls_profile.env"))
    return values


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        logger.debug("Failed to read pid file %s", path, exc_info=True)
        return None


def pid_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def run_command(args: Sequence[str], timeout: float = 1.5) -> str:
    try:
        proc = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout.strip()
    except Exception:
        logger.debug("Command failed while collecting monitor state: %s", " ".join(args), exc_info=True)
        return ""


def process_state(run_dir: Path, name: str) -> ProcessState:
    if name == "turn":
        output = run_command(["pgrep", "-xo", "turnserver"], timeout=1.0)
        try:
            pid = int(output.splitlines()[0])
        except Exception:
            logger.debug("Failed to parse turnserver pid from pgrep output: %s", output, exc_info=True)
            return ProcessState(name=name, pid=None, running=False)
        state = ProcessState(name=name, pid=pid, running=pid_running(pid))
        if state.running:
            stats = run_command(["ps", "-p", str(pid), "-o", "etimes=,pcpu=,pmem="], timeout=1.0)
            parts = stats.split()
            if len(parts) >= 3:
                state.etime = format_elapsed(int(float(parts[0])))
                state.cpu = parts[1]
                state.mem = parts[2]
        return state

    pid = read_pid(run_dir / f"{name}.pid")
    running = pid_running(pid)
    state = ProcessState(name=name, pid=pid, running=running)
    if running and pid:
        output = run_command(["ps", "-p", str(pid), "-o", "etimes=,pcpu=,pmem="], timeout=1.0)
        parts = output.split()
        if len(parts) >= 3:
            state.etime = format_elapsed(int(float(parts[0])))
            state.cpu = parts[1]
            state.mem = parts[2]
    return state


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def tail(path: Path, lines: int = 200) -> List[str]:
    if not path.exists():
        return []
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        logger.debug("Failed to read log tail from %s", path, exc_info=True)
        return []
    return [strip_ansi(line) for line in data[-lines:]]


def find_last(lines: Iterable[str], patterns: Sequence[str]) -> str:
    for line in reversed(list(lines)):
        if any(pattern in line for pattern in patterns):
            return line.strip()
    return ""


def unique_matches(lines: Iterable[str], pattern: str, limit: int = 6) -> List[str]:
    regex = re.compile(pattern)
    seen: List[str] = []
    for line in lines:
        match = regex.search(line)
        if not match:
            continue
        value = match.group(1)
        if value not in seen:
            seen.append(value)
    return seen[-limit:]


def add_active(values: List[str], value: str):
    if value not in values:
        values.append(value)


def remove_active(values: List[str], value: str):
    try:
        values.remove(value)
    except ValueError:
        pass


def active_bridge_members(lines: Iterable[str], limit: int = 12) -> List[str]:
    active: List[str] = []
    join_regex = re.compile(r"New ROS 2 bridge detected:\s*([0-9A-Fa-f]+)")
    left_regex = re.compile(r"Remote ROS 2 bridge left:\s*([0-9A-Fa-f]+)")
    for line in lines:
        joined = join_regex.search(line)
        if joined:
            add_active(active, joined.group(1))
        left = left_regex.search(line)
        if left:
            remove_active(active, left.group(1))
    return active[-limit:]


def active_ros_nodes(lines: Iterable[str], limit: int = 8) -> List[str]:
    active: List[str] = []
    discovered_regex = re.compile(r"Discovered ROS Node\s+([/\w.-]+)")
    undiscovered_regex = re.compile(r"Undiscovered ROS Node\s+([/\w.-]+)")
    for line in lines:
        discovered = discovered_regex.search(line)
        if discovered:
            node = discovered.group(1)
            if node not in MONITOR_ROS_NODES:
                add_active(active, node)
        undiscovered = undiscovered_regex.search(line)
        if undiscovered:
            remove_active(active, undiscovered.group(1))
    return active[-limit:]


def active_signal_members(lines: Iterable[str], limit: int = 16) -> List[str]:
    active: List[str] = []
    registered_regex = re.compile(r"\bregistered role=([a-z]+) room=([\w.-]+)")
    unregistered_regex = re.compile(r"\bunregistered role=([a-z]+) room=([\w.-]+)")
    for line in lines:
        unregistered = unregistered_regex.search(line)
        if unregistered:
            remove_active(active, f"{unregistered.group(1)}:{unregistered.group(2)}")
            continue
        registered = registered_regex.search(line)
        if registered:
            add_active(active, f"{registered.group(1)}:{registered.group(2)}")
    return active[-limit:]


def load_signal_members_state(path: Path, limit: int = 16) -> List[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to load signaling member state from %s", path, exc_info=True)
        return []
    members: List[str] = []
    for peer in payload.get("peers", []):
        if not isinstance(peer, dict):
            continue
        role = str(peer.get("role", "")).strip()
        room = str(peer.get("room", "")).strip()
        if role and room:
            add_active(members, f"{role}:{room}")
    return members[-limit:]


def webrtc_registrations(lines: Iterable[str], limit: int = 8) -> List[str]:
    active: List[str] = []
    connected_regex = re.compile(r"WebRTC peer (?:registered|ready):\s*role=([a-z]+)\s+room=([\w.-]+)")
    left_regex = re.compile(r"WebRTC peer left:\s*role=([a-z]+)\s+room=([\w.-]+)")
    for line in lines:
        left = left_regex.search(line)
        if left:
            remove_active(active, f"{left.group(1)}:{left.group(2)}")
            continue
        match = connected_regex.search(line)
        if match:
            add_active(active, f"{match.group(1)}:{match.group(2)}")
            continue
        if "WebRTC signaling disconnected" in line or "WebRTC signaling peer disconnected" in line:
            active.clear()
    return active[-limit:]


def active_webrtc_peers(lines: Iterable[str], limit: int = 8) -> List[str]:
    active: List[str] = []
    connected_regex = re.compile(r"WebRTC signaling peer connected:\s*(.+)")
    disconnected_regex = re.compile(r"WebRTC signaling peer disconnected:\s*(.+)")
    for line in lines:
        connected = connected_regex.search(line)
        if connected:
            add_active(active, connected.group(1).strip())
            continue
        disconnected = disconnected_regex.search(line)
        if disconnected:
            remove_active(active, disconnected.group(1).strip())
            continue
        if "WebRTC signaling disconnected" in line:
            active.clear()
    return active[-limit:]


def active_webrtc_clients(lines: Iterable[str], limit: int = 8) -> List[str]:
    active: List[str] = []
    connected_regex = re.compile(r"WebRTC signaling connected:\s*(.+)")
    for line in lines:
        connected = connected_regex.search(line)
        if connected:
            active = [connected.group(1).strip()]
            continue
        if "WebRTC signaling disconnected" in line or "signaling reconnect after error" in line:
            active.clear()
    return active[-limit:]


def tcp_open(host: str, port: int, timeout: float = 0.35) -> Optional[bool]:
    if not host:
        return None
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def webrtc_signaling_state(lines: Sequence[str]) -> Optional[bool]:
    for line in reversed(lines[-160:]):
        if (
            "Incoming RTP video" in line
            or "cmd-vel DataChannel open" in line
            or "DataChannel received" in line
            or "WebRTC signaling connected:" in line
            or "WebRTC signaling peer connected:" in line
        ):
            return True
        if (
            "WebRTC signaling disconnected" in line
            or "signaling reconnect after error" in line
            or "Connection refused" in line
        ):
            return False
    return None


def local_listeners(ports: Sequence[int]) -> Dict[int, str]:
    output = run_command(["ss", "-ltunp"], timeout=1.0)
    result: Dict[int, str] = {}
    for line in output.splitlines():
        for port in ports:
            if f":{port} " in line or f":{port}\t" in line:
                result[port] = line.strip()
    return result


def local_ips() -> List[str]:
    output = run_command(["hostname", "-I"], timeout=1.0)
    return [item for item in output.split() if item]


def tailscale_ips() -> List[str]:
    if not shutil.which("tailscale"):
        return []
    output = run_command(["tailscale", "ip", "-4"], timeout=1.0)
    return [item for item in output.split() if item]


def is_vpn_ip(value: str) -> bool:
    return value.startswith("100.") or value.startswith("fd7a:")


def preferred_ip(ips: Sequence[str], ts_ips: Sequence[str]) -> str:
    for item in ts_ips:
        if item:
            return item
    for item in ips:
        if is_vpn_ip(item):
            return item
    for item in ips:
        if not item.startswith(("127.", "172.17.", "10.255.")) and ":" not in item:
            return item
    return ips[0] if ips else "-"


def ros_topics(env: Dict[str, str], root: Path, cache: Dict[str, object], refresh: bool) -> List[str]:
    now = time.time()
    cached_at = float(cache.get("ros_topics_at", 0.0))
    if not refresh and now - cached_at < 5.0:
        return list(cache.get("ros_topics", []))

    distro = env.get("ROS_DISTRO", "jazzy")
    setup = Path(f"/opt/ros/{distro}/setup.bash")
    if not setup.exists():
        cache["ros_topics"] = []
        cache["ros_topics_at"] = now
        return []

    ros_exports = ""
    for key in ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY", "ROS_AUTOMATIC_DISCOVERY_RANGE", "ROS_STATIC_PEERS", "RMW_IMPLEMENTATION"):
        value = env.get(key)
        if value is not None and value != "":
            ros_exports += f"export {key}={shell_quote(value)}; "

    probe = (
        "import rclpy; "
        "rclpy.init(); "
        "node = rclpy.create_node('horus_monitor_topic_probe'); "
        "[rclpy.spin_once(node, timeout_sec=0.1) for _ in range(12)]; "
        "print('\\n'.join(sorted(name for name, _ in node.get_topic_names_and_types()))); "
        "node.destroy_node(); "
        "rclpy.shutdown()"
    )
    cmd = (
        f"cd {shell_quote(str(root))}; "
        "set +u; "
        f"source {shell_quote(str(setup))}; "
        f"{ros_exports}"
        f"python3 -c {shell_quote(probe)}"
    )
    output = run_command(["bash", "-lc", cmd], timeout=4.0)
    topics = sorted([line.strip() for line in output.splitlines() if line.strip().startswith("/")])
    cache["ros_topics"] = topics
    cache["ros_topics_at"] = now
    return topics


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def truncate(text: str, width: int) -> str:
    clean = strip_ansi(text)
    if len(clean) <= width:
        return text
    if width <= 1:
        return ""
    return clean[: max(0, width - 1)] + "…"


def extract_state(root: Path, env_path: Path, cache: Dict[str, object], force_refresh: bool = False) -> Dict[str, object]:
    env = load_env(root, env_path)
    run_dir = Path(os.environ.get("HORUS_RUN_DIR", str(root / ".run"))).expanduser()
    role = env.get("HORUS_ROLE", "robot")
    topology = env.get("HORUS_TOPOLOGY", "hub")
    zenoh_port = int(env.get("ZENOH_PORT", "7447") or 7447)
    signal_port = int(env.get("WEBRTC_SIGNAL_PORT", "8765") or 8765)
    turn_port = int(env.get("TURN_PORT", "3478") or 3478)

    processes = {
        name: process_state(run_dir, name)
        for name in ("zenoh", "webrtc", "signal", "turn")
    }
    logs = {
        "zenoh": tail(run_dir / "zenoh.log", lines=2000),
        "webrtc": tail(run_dir / "webrtc.log", lines=1000),
        "signal": tail(run_dir / "signal.log", lines=1000),
    }
    listeners = local_listeners([zenoh_port, signal_port, turn_port])
    topics = ros_topics(env, root, cache, force_refresh)

    bridge_members = active_bridge_members(logs["zenoh"])
    ros_nodes = active_ros_nodes(logs["zenoh"], limit=8)
    signal = processes["signal"]
    signal_state = load_signal_members_state(run_dir / "signal_members.json") if signal.running else []
    signal_members = signal_state or active_signal_members(logs["signal"])
    webrtc_peers = active_webrtc_peers(logs["webrtc"], limit=8)
    webrtc_clients = active_webrtc_clients(logs["webrtc"], limit=8)
    webrtc_registered = webrtc_registrations(logs["webrtc"])

    ips = local_ips()
    ts_ips = tailscale_ips()
    local_route_ip = preferred_ip(ips, ts_ips)
    target = connection_target(env)
    target_ports = {}
    if target:
        target_ports[zenoh_port] = tcp_open(target, zenoh_port)
        target_ports[signal_port] = webrtc_signaling_state(logs["webrtc"])

    return {
        "env": env,
        "role": role,
        "topology": topology,
        "ports": {"zenoh": zenoh_port, "signal": signal_port, "turn": turn_port},
        "processes": processes,
        "logs": logs,
        "listeners": listeners,
        "topics": topics,
        "bridge_members": bridge_members,
        "ros_nodes": ros_nodes,
        "signal_members": signal_members,
        "webrtc_peers": webrtc_peers,
        "webrtc_clients": webrtc_clients,
        "webrtc_registrations": webrtc_registered,
        "ips": ips,
        "tailscale_ips": ts_ips,
        "local_route_ip": local_route_ip,
        "target": target,
        "target_ports": target_ports,
        "updated_at": time.strftime("%H:%M:%S"),
    }


def connection_target(env: Dict[str, str]) -> str:
    role = env.get("HORUS_ROLE", "robot")
    topology = env.get("HORUS_TOPOLOGY", "hub")
    if topology == "direct" and role == "robot":
        return env.get("HORUS_MACHINE_IP", "")
    if topology == "hub" and role in {"robot", "machine"}:
        return env.get("HORUS_CLOUD_IP", "")
    return ""


def zenoh_transport_name(env: Dict[str, str]) -> str:
    mode = env.get("ZENOH_TRANSPORT", "auto").strip().lower()
    if mode not in {"auto", "tcp", "quic"}:
        mode = "auto"
    if mode == "auto":
        quic_enabled = env.get("ZENOH_AUTO_ENABLE_QUIC", "0").strip().lower() in {"1", "true", "yes", "on"}
        has_root = path_exists(env.get("ZENOH_TLS_ROOT_CA", ""))
        has_listener = path_exists(env.get("ZENOH_TLS_LISTEN_KEY", "")) and path_exists(env.get("ZENOH_TLS_LISTEN_CERT", ""))
        if quic_enabled and (has_root or has_listener):
            return "quic+tcp"
        return "tcp"
    return mode


def path_exists(value: str) -> bool:
    if not value:
        return False
    return Path(os.path.expanduser(value)).exists()


def build_services(state: Dict[str, object]) -> List[ServiceState]:
    env = state["env"]
    role = str(state["role"])
    topology = str(state["topology"])
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    logs: Dict[str, List[str]] = state["logs"]  # type: ignore[assignment]
    listeners: Dict[int, str] = state["listeners"]  # type: ignore[assignment]
    ports: Dict[str, int] = state["ports"]  # type: ignore[assignment]
    target_ports: Dict[int, Optional[bool]] = state["target_ports"]  # type: ignore[assignment]
    bridge_members: List[str] = state["bridge_members"]  # type: ignore[assignment]
    topics: List[str] = state["topics"]  # type: ignore[assignment]

    services: List[ServiceState] = []

    zenoh = processes["zenoh"]
    zenoh_error = find_last(logs["zenoh"], ["Failed to start Zenoh runtime", "Unable to connect"])
    if env.get("HORUS_ZENOH_ENABLED", "1") == "0":
        services.append(ServiceState("Zenoh", "SKIP", "disabled for this fleet member", "idle"))
    elif not zenoh.running:
        services.append(ServiceState("Zenoh", "DOWN", "process is not running", "down"))
    elif zenoh_error and "New ROS 2 bridge detected" not in "\n".join(logs["zenoh"][-20:]):
        services.append(ServiceState("Zenoh", "WARN", truncate(zenoh_error, 90), "warn"))
    elif bridge_members:
        peers = ", ".join(named_zenoh_members(state)) or f"{len(bridge_members)} ROS bridge peer(s)"
        services.append(ServiceState("Zenoh", "OK", f"connected: {peers}", "ok"))
    elif topology == "direct" and role == "machine" and ports["zenoh"] in listeners:
        services.append(
            ServiceState("Zenoh", "LISTEN", f"waiting on {zenoh_transport_name(env)}/0.0.0.0:{ports['zenoh']}", "info")
        )
    elif state["target"] and target_ports.get(ports["zenoh"]) is True:
        services.append(ServiceState("Zenoh", "OPEN", f"TCP fallback reachable at {state['target']}:{ports['zenoh']}", "info"))
    else:
        services.append(ServiceState("Zenoh", "RUN", f"pid {zenoh.pid}, uptime {zenoh.etime}", "info"))

    webrtc = processes["webrtc"]
    webrtc_log = "\n".join(logs["webrtc"][-120:])
    if role == "cloud":
        signal = processes["signal"]
        if not signal.running:
            services.append(ServiceState("WebRTC", "DOWN", "signaling relay is not running", "down"))
        else:
            detail = "relay running"
            if state["signal_members"]:
                detail = "members: " + ", ".join(state["signal_members"][-4:])  # type: ignore[index]
            services.append(ServiceState("WebRTC", "OK", detail, "ok"))
    elif not webrtc.running:
        services.append(ServiceState("WebRTC", "DOWN", "process is not running", "down"))
    elif "Incoming RTP video" in webrtc_log:
        services.append(ServiceState("WebRTC", "VIDEO", "RTP video connected and receiver pipeline active", "ok"))
    elif role == "robot" and ("ROS image appsrc rate" in webrtc_log or "ROS image appsrc caps" in webrtc_log):
        detail = "camera stream publishing"
        if "cmd-vel DataChannel open" in webrtc_log or "DataChannel received" in webrtc_log:
            detail += "; control DataChannel connected"
        services.append(ServiceState("WebRTC", "VIDEO", detail, "ok"))
    elif "cmd-vel DataChannel open" in webrtc_log or "DataChannel received" in webrtc_log:
        services.append(ServiceState("WebRTC", "CTRL", "control DataChannel connected, waiting for video", "warn"))
    elif "WebRTC signaling peer connected" in webrtc_log or "WebRTC signaling connected" in webrtc_log:
        peer = ", ".join(named_webrtc_members(state, logs)) or infer_webrtc_peer(logs["webrtc"]) or "signaling peer connected"
        services.append(ServiceState("WebRTC", "SIGNAL", peer, "info"))
    elif "signaling reconnect after error" in webrtc_log or "Connection refused" in webrtc_log:
        detail = find_last(logs["webrtc"], ["signaling reconnect after error", "Connection refused"])
        services.append(ServiceState("WebRTC", "WARN", truncate(detail, 90), "warn"))
    elif topology == "direct" and role == "machine" and ports["signal"] in listeners:
        services.append(ServiceState("WebRTC", "LISTEN", f"waiting on ws://0.0.0.0:{ports['signal']}", "info"))
    elif state["target"] and target_ports.get(ports["signal"]) is True:
        services.append(ServiceState("WebRTC", "OPEN", f"target ws://{state['target']}:{ports['signal']} reachable", "info"))
    else:
        services.append(ServiceState("WebRTC", "RUN", f"pid {webrtc.pid}, uptime {webrtc.etime}", "info"))

    if role == "cloud":
        turn = processes["turn"]
        turn_enabled = env.get("HORUS_CLOUD_RUN_TURN", "0") == "1"
        turn_range = f"{env.get('TURN_MIN_PORT', '49152')}-{env.get('TURN_MAX_PORT', '65535')}"
        if not turn_enabled:
            services.append(ServiceState("TURN", "OFF", "media relay fallback is disabled", "idle"))
        elif turn.running and ports["turn"] in listeners:
            services.append(ServiceState("TURN", "OK", f"relay listening on udp/tcp {ports['turn']}, ports {turn_range}", "ok"))
        elif turn.running:
            services.append(ServiceState("TURN", "WARN", f"turnserver running, but port {ports['turn']} was not detected", "warn"))
        else:
            services.append(ServiceState("TURN", "DOWN", "media relay fallback is enabled but not running", "down"))

    if role == "cloud":
        services.append(ServiceState("ROS 2", "N/A", "cloud relay does not need local ROS 2 nodes", "idle"))
    elif topics:
        important = [topic for topic in topics if topic in {"/odom", "/scan", "/tf", "/joint_states"}]
        camera = env.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC") or env.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC") or ""
        if camera and camera in topics:
            important.append(camera)
        detail = f"{len(topics)} topics"
        if important:
            detail += ": " + ", ".join(dict.fromkeys(important[:5]))
        services.append(ServiceState("ROS 2", "OK", detail, "ok"))
    else:
        services.append(ServiceState("ROS 2", "WAIT", "no visible local topics yet", "warn"))

    return services


def level_color(level: str) -> str:
    return {
        "ok": "green",
        "warn": "yellow",
        "down": "red",
        "info": "cyan",
        "idle": "muted",
    }.get(level, "white")


def dot(level: str, use_color: bool) -> str:
    return color("*", level_color(level), use_color)


def rail(level: str, use_color: bool) -> str:
    return color("*", level_color(level), use_color)


def pill(text: str, level: str, use_color: bool) -> str:
    if not use_color:
        return text.lower()
    return color(text.lower(), level_color(level), use_color)


def section(title: str, use_color: bool) -> str:
    width = max(72, shutil.get_terminal_size((100, 30)).columns)
    width = min(width, 136)
    text = f"{title.title()} "
    line_len = max(5, width - len(text) - 1)
    return color(text + "-" * line_len, "muted", use_color)


def clean_status(status: str, level: str, use_color: bool) -> str:
    return color(status.lower(), level_color(level), use_color)


def flat_row(left: str, right: str, width: int, use_color: bool, left_color: str = "white") -> str:
    left_text = color(left, left_color, use_color)
    right_width = max(10, width - visible_len(left_text) - 3)
    return f"{left_text}  {truncate(right, right_width)}"


def overall_state(services: Sequence[ServiceState]) -> ServiceState:
    if any(svc.level == "down" for svc in services):
        failed = ", ".join(svc.title for svc in services if svc.level == "down")
        return ServiceState("System", "OFFLINE", f"needs attention: {failed}", "down")
    if any(svc.level == "warn" for svc in services):
        warnings = ", ".join(f"{svc.title}:{svc.status}" for svc in services if svc.level == "warn")
        return ServiceState("System", "DEGRADED", warnings, "warn")
    if any(svc.status in {"OK", "VIDEO", "CTRL", "SIGNAL", "LISTEN", "OPEN", "RUN"} for svc in services):
        return ServiceState("System", "ONLINE", "transport stack is running", "ok")
    return ServiceState("System", "STARTING", "waiting for transport state", "info")


def print_exit_summary(state: Dict[str, object], use_color: bool):
    out = []
    out.append("")
    services = build_services(state)
    overall = overall_state(services)

    status_label = pill(overall.status, overall.level, use_color)
    out.append(f"{style('HORUS Connector', BOLD, use_color)}")
    out.append(f"{color('Monitor closed', 'muted', use_color)}  {status_label}")
    out.append("")

    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore
    for svc in services:
        proc = processes.get(svc.title.lower().replace(" ", ""))
        runtime = ""
        if proc and proc.running:
            runtime = f" (PID {proc.pid} up {proc.etime})"
        label = pill(svc.status, svc.level, use_color)
        out.append(f"  {svc.title.ljust(8)} {label}{color(runtime, 'muted', use_color)}")
    out.append("")
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def render(state: Dict[str, object], args: argparse.Namespace, show_help: bool, show_logs: bool) -> str:
    terminal_size = shutil.get_terminal_size((100, 30))
    width = max(72, terminal_size.columns)
    width = min(width, 136)
    height = terminal_size.lines
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    logs: Dict[str, List[str]] = state["logs"]  # type: ignore[assignment]
    use_color = not args.no_color
    services = build_services(state)
    overall = overall_state(services)

    out: List[str] = []
    identity = f"{state['role']}:{env.get('HORUS_ROOM', 'default')}"
    status_indicator = pill(overall.status, overall.level, use_color)
    clock = color(f"{state['updated_at']}", "muted", use_color)

    heading = (
        style(color("HORUS Connector", "cyan", use_color), BOLD, use_color)
        + color("  ", "muted", use_color)
        + style(identity.upper(), BOLD, use_color)
        + color("  ", "muted", use_color)
        + status_indicator
    )
    out.append(pad_visible(heading, width - visible_len(clock) - 1) + clock)
    if height >= 14:
        subtitle = (
            color("Robot management transport layer", "muted", use_color)
            + color("  topology ", "dark", use_color)
            + style(str(state["topology"]).lower(), BOLD, use_color)
        )
        out.append(truncate(subtitle, width))
    out.append(color("-" * width, "muted", use_color))

    # Route details
    if height >= 13:
        out.append(f"{rail(overall.level, use_color)} " + style(route_line(state), BOLD, use_color))
        if height >= 16:
            out.append(f"  " + route_detail(state, env))
        if height >= 20:
            out.append(f"  " + connection_focus(state, logs, env))
        if height >= 15:
            out.append("")
    else:
        out.append(color("> ", "muted", use_color) + style(route_line(state), BOLD, use_color))

    # Status section
    out.append(section("status", use_color))
    for svc in services:
        out.append(service_line(svc, state, processes, width, use_color))

    # Members and ROS graph
    show_members = height >= 26
    show_ros = height >= 30

    if show_members:
        out.append("")
        out.append(section("members", use_color))
        for line_item in member_lines(state, logs, use_color):
            out.append("  " + truncate(line_item, width - 2))

    if show_ros:
        out.append("")
        out.append(section("ros graph", use_color))
        for line_item in ros_lines(state, env):
            out.append("  " + truncate(line_item, width - 2))

    # Events section
    if show_logs:
        reserved_lines = 3 if show_help else 2
        remaining_lines = height - len(out) - reserved_lines - 2
        if remaining_lines >= 2:
            out.append("")
            out.append(section("events", use_color))
            event_limit = min(remaining_lines, 8)
            for event in event_body(logs, limit=event_limit):
                out.append("  " + truncate(event, width - 2))

    # Footer
    if height - len(out) >= (3 if show_help else 2):
        out.append("")

    if show_help:
        keys = (
            color(" q", "cyan", use_color) + color(" quit", "muted", use_color)
            + color("  r", "cyan", use_color) + color(" rescan", "muted", use_color)
            + color("  l", "cyan", use_color) + color(" events", "muted", use_color)
            + color("  h", "cyan", use_color) + color(" help", "muted", use_color)
            + color("  |  headless: ", "dark", use_color)
            + color("HORUS_LAUNCH_MONITOR=0 ./horus launch <role>", "muted", use_color)
        )
        out.append(truncate(keys, width))
    else:
        keys = (
            color(" q", "cyan", use_color) + color(" quit", "muted", use_color)
            + color("  r", "cyan", use_color) + color(" rescan", "muted", use_color)
            + color("  l", "cyan", use_color) + color(" events", "muted", use_color)
            + color("  h", "cyan", use_color) + color(" help", "muted", use_color)
        )
        out.append(truncate(keys, width))

    # Hard cap to never exceed terminal height
    return "\n".join(out[: height - 1])


def route_line(state: Dict[str, object]) -> str:
    role = str(state["role"])
    topology = str(state["topology"])
    room = str(state["env"].get("HORUS_ROOM", "default"))  # type: ignore[index]
    local_ip = str(state["local_route_ip"])
    target = str(state["target"] or "")
    if topology == "direct" and role == "robot":
        return f"robot:{room} {local_ip}  ->  machine:{room} {target or '?'}"
    if topology == "direct" and role == "machine":
        return f"machine:{room} {local_ip}  <-  robot:{room}"
    if topology == "hub" and role in {"robot", "machine"}:
        return f"{role}:{room} {local_ip}  ->  cloud {target or '?'}"
    return f"cloud {local_ip}  <-  robots and machines"


def route_detail(state: Dict[str, object], env: Dict[str, str]) -> str:
    ports: Dict[str, int] = state["ports"]  # type: ignore[assignment]
    listeners: Dict[int, str] = state["listeners"]  # type: ignore[assignment]
    target_ports: Dict[int, Optional[bool]] = state["target_ports"]  # type: ignore[assignment]
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    role = str(state["role"])
    topology = str(state["topology"])
    if role == "cloud" or (topology == "direct" and role == "machine"):
        zenoh = "listening" if processes["zenoh"].running and ports["zenoh"] in listeners else "stopped"
        webrtc_process = processes["signal"] if role == "cloud" else processes["webrtc"]
        webrtc = "listening" if webrtc_process.running and ports["signal"] in listeners else "stopped"
    else:
        zenoh = "stopped" if not processes["zenoh"].running else "open" if target_ports.get(ports["zenoh"]) is True else "closed"
        signal = target_ports.get(ports["signal"]) if processes["webrtc"].running else None
        webrtc = "connected" if signal is True else "reconnecting" if signal is False else "stopped"
    detail = (
        f"zenoh {zenoh_transport_name(env)}/{ports['zenoh']} {zenoh}   "
        f"webrtc ws/{ports['signal']} {webrtc}   "
        f"domain {env.get('ROS_DOMAIN_ID', '0')}   namespace {env.get('ZENOH_NAMESPACE', '/') or '/'}"
    )
    if role == "cloud":
        if env.get("HORUS_CLOUD_RUN_TURN", "0") == "1":
            turn = "listening" if processes["turn"].running and ports.get("turn") in listeners else "stopped"
            detail += f"   turn udp/tcp/{ports.get('turn', 3478)} {turn}"
        else:
            detail += "   turn off"
    return detail


def service_line(
    svc: ServiceState,
    state: Dict[str, object],
    processes: Dict[str, ProcessState],
    width: int,
    use_color: bool,
) -> str:
    proc = processes.get(svc.title.lower().replace(" ", ""))
    runtime = ""
    if proc and proc.running:
        runtime = f"  pid {proc.pid} up {proc.etime}"
    label = pill(svc.status, svc.level, use_color)
    left = f"  {rail(svc.level, use_color)} {svc.title.ljust(7)} {label}"
    right = f"{svc.detail}{color(runtime, 'muted', use_color)}"
    return left + " " + truncate(right, max(12, width - visible_len(left) - 1))


def connection_focus(state: Dict[str, object], logs: Dict[str, List[str]], env: Dict[str, str]) -> str:
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    zenoh_names = named_zenoh_members(state)
    webrtc_names = named_webrtc_members(state, logs)
    parts = []
    zenoh_proc = processes.get("zenoh")
    webrtc_proc = processes.get("webrtc")
    signal_proc = processes.get("signal")
    if zenoh_names and (not zenoh_proc or zenoh_proc.running):
        parts.append("zenoh -> " + ",".join(zenoh_names))
    if webrtc_names and ((webrtc_proc and webrtc_proc.running) or (signal_proc and signal_proc.running)):
        parts.append("webrtc -> " + ",".join(webrtc_names))
    camera = env.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC") or env.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "")
    if camera:
        parts.append("camera -> " + camera)
    return "   ".join(parts) if parts else "waiting for named peers"


def named_zenoh_members(state: Dict[str, object]) -> List[str]:
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    zenoh_proc = processes.get("zenoh")
    if zenoh_proc and not zenoh_proc.running:
        return []
    bridge_members: List[str] = state["bridge_members"]  # type: ignore[assignment]
    if not bridge_members:
        return []
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    role = str(state["role"])
    topology = str(state["topology"])
    room = env.get("HORUS_ROOM", "default")
    if topology == "direct":
        if role == "machine":
            return [f"robot:{room}"]
        if role == "robot":
            return [f"machine:{room}"]
    signal_members: List[str] = state["signal_members"]  # type: ignore[assignment]
    if role == "cloud":
        return signal_members or [f"{len(bridge_members)} ROS bridge peer(s)"]
    target = str(state["target"] or "cloud")
    return [f"cloud:{target}"]


def named_webrtc_members(state: Dict[str, object], logs: Dict[str, List[str]]) -> List[str]:
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    role = str(state["role"])
    topology = str(state["topology"])
    room = env.get("HORUS_ROOM", "default")
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    if role == "cloud":
        signal_proc = processes.get("signal")
        if signal_proc and not signal_proc.running:
            return []
    else:
        webrtc_proc = processes.get("webrtc")
        if webrtc_proc and not webrtc_proc.running:
            return []
    registered: List[str] = state["webrtc_registrations"]  # type: ignore[assignment]
    peers: List[str] = state["webrtc_peers"]  # type: ignore[assignment]
    clients: List[str] = state["webrtc_clients"]  # type: ignore[assignment]
    signal_members: List[str] = state["signal_members"]  # type: ignore[assignment]
    if registered:
        return registered
    if signal_members:
        return signal_members
    if topology == "direct" and role == "machine" and peers:
        return [f"robot:{room} @ {summarize_webrtc_endpoint(peers[-1])}"]
    if topology == "direct" and role == "robot" and (clients or infer_webrtc_peer(logs["webrtc"])):
        target = str(state["target"] or "")
        return [f"machine:{room}" + (f" @ {target}" if target else "")]
    if topology == "hub" and role in {"robot", "machine"} and (clients or infer_webrtc_peer(logs["webrtc"])):
        target = str(state["target"] or "")
        return [f"cloud" + (f" @ {target}" if target else "")]
    return []


def group_role_members(members: Sequence[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {"robot": [], "machine": []}
    for member in members:
        if ":" not in member:
            continue
        role, room = member.split(":", 1)
        if role not in grouped:
            grouped[role] = []
        add_active(grouped[role], room)
    return grouped


def room_presence(members: Sequence[str]) -> List[str]:
    rooms: Dict[str, List[str]] = {}
    for member in members:
        if ":" not in member:
            continue
        role, room = member.split(":", 1)
        rooms.setdefault(room, [])
        add_active(rooms[room], role)
    output = []
    for room in sorted(rooms):
        roles = rooms[room]
        if "robot" in roles and "machine" in roles:
            output.append(f"{room}: robot + machine")
        else:
            output.append(f"{room}: " + " + ".join(sorted(roles)))
    return output


def endpoint_lines(state: Dict[str, object], use_color: bool) -> List[str]:
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    role = str(state["role"])
    room = env.get("HORUS_ROOM", "default")
    ip = str(state["local_route_ip"])
    label = ROLE_SUMMARIES.get(role, f"{role} endpoint")
    lines = [f"this    {style(role, BOLD, use_color)}:{room} @ {ip}  {color(label, 'muted', use_color)}"]
    signal_members: List[str] = state["signal_members"]  # type: ignore[assignment]
    if role == "cloud":
        grouped = group_role_members(signal_members)
        robots = ", ".join(grouped.get("robot", [])) or "none"
        machines = ", ".join(grouped.get("machine", [])) or "none"
        lines.append(f"users   robots {robots}   machines {machines}")
        rooms = room_presence(signal_members)
        lines.append("rooms   " + (", ".join(rooms) if rooms else "waiting for robot and machine endpoints"))
    else:
        webrtc_names: List[str] = state["webrtc_registrations"]  # type: ignore[assignment]
        peer_type = "machine" if role == "robot" else "robot"
        peer = ", ".join(webrtc_names) if webrtc_names else f"no {peer_type} peer yet"
        lines.append(f"users   local {role}:{room}   remote {peer}")
    return lines


def member_lines(state: Dict[str, object], logs: Dict[str, List[str]], use_color: bool = False) -> List[str]:
    lines: List[str] = []
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    zenoh_names = named_zenoh_members(state)
    webrtc_names = named_webrtc_members(state, logs)
    zenoh_proc = processes.get("zenoh")
    webrtc_proc = processes.get("webrtc")
    signal_proc = processes.get("signal")

    lines.extend(endpoint_lines(state, use_color))

    if zenoh_proc and not zenoh_proc.running:
        lines.append("zenoh   service stopped")
    elif zenoh_names:
        lines.append("zenoh   connected to " + ", ".join(zenoh_names))
    else:
        lines.append("zenoh   no named peer yet")

    webrtc_running = bool((webrtc_proc and webrtc_proc.running) or (signal_proc and signal_proc.running))
    if not webrtc_running:
        lines.append("webrtc  service stopped")
    elif webrtc_names:
        lines.append("webrtc  connected to " + ", ".join(webrtc_names))
    else:
        lines.append("webrtc  no named peer yet")
    ros_nodes: List[str] = state["ros_nodes"]  # type: ignore[assignment]
    visible_nodes = [node for node in ros_nodes if "_ros2cli_daemon" not in node]
    if visible_nodes:
        lines.append("ros2    " + ", ".join(visible_nodes[-4:]))
    else:
        lines.append("ros2    no remote ROS node names yet")
    return lines


def ros_lines(state: Dict[str, object], env: Dict[str, str]) -> List[str]:
    topics: List[str] = state["topics"]  # type: ignore[assignment]
    camera_in = env.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "/camera/image_raw")
    camera_out = env.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC", "/camera/webrtc/image_raw")
    key_topics = ["/odom", "/scan", "/tf", "/joint_states", "/points", camera_in, camera_out]
    visible = [topic for topic in key_topics if topic and topic in topics]
    lines = [f"{len(topics)} visible topic(s)"]
    if visible:
        lines.append("important " + ", ".join(dict.fromkeys(visible)))
    else:
        lines.append("important none of the configured key topics are visible")
    video_topic = env.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC") or env.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "-")
    lines.append(f"video topic {video_topic}")
    return lines


def summarize_webrtc_endpoint(value: str) -> str:
    url = re.search(r"url=wss?://([^/\s]+)", value)
    if url:
        return url.group(1)
    return value


def event_body(logs: Dict[str, List[str]], limit: int = 7) -> List[str]:
    events = recent_events(logs)[-limit:]
    return events or ["no recent events"]


def infer_webrtc_peer(lines: List[str]) -> str:
    text = "\n".join(lines[-120:])
    if "Incoming RTP video" in text:
        return "video connected"
    if "cmd-vel DataChannel open" in text or "DataChannel received" in text:
        return "control connected"
    if "WebRTC signaling peer connected" in text or "WebRTC signaling connected:" in text:
        return "signaling peer connected"
    if "WebRTC signaling listening" in text:
        return "waiting for peer"
    return ""


def recent_events(logs: Dict[str, List[str]]) -> List[str]:
    markers = [
        "New ROS 2 bridge detected",
        "Remote ROS 2 bridge left",
        "Discovered ROS Node",
        "Undiscovered ROS Node",
        "Unable to connect",
        "Failed to start",
        "WebRTC signaling listening",
        "WebRTC signaling connected",
        "WebRTC signaling peer connected",
        "WebRTC signaling peer disconnected",
        "WebRTC signaling disconnected",
        "WebRTC peer registered",
        "WebRTC peer ready",
        "WebRTC peer left",
        "registered role=",
        "unregistered role=",
        "DataChannel received",
        "cmd-vel DataChannel open",
        "Incoming RTP video",
        "signaling reconnect after error",
        "ROS image appsrc rate",
        "Publishing decoded WebRTC images",
        "cmd_vel watchdog",
        "GStreamer recovery",
        "Robot GStreamer pipeline recovered",
    ]
    events = []
    for name, lines in logs.items():
        for line_item in lines[-80:]:
            if any(marker in line_item for marker in markers):
                formatted = format_event(name, line_item.strip())
                if formatted:
                    events.append(formatted)
    success_indexes = [
        index
        for index, event in enumerate(events)
        if (
            "signaling connection established" in event
            or "peer connected from" in event
            or "incoming camera RTP is active" in event
            or "cmd_vel data channel is open" in event
        )
    ]
    if success_indexes:
        last_success = success_indexes[-1]
        events = [
            event
            for index, event in enumerate(events)
            if index >= last_success or "signaling reconnecting" not in event
        ]
    return events[-12:]


def format_event(name: str, line_item: str) -> str:
    bridge = re.search(r"New ROS 2 bridge detected:\s*([0-9A-Fa-f]+)", line_item)
    if bridge:
        return f"{name}: ROS bridge peer connected"
    bridge_left = re.search(r"Remote ROS 2 bridge left:\s*([0-9A-Fa-f]+)", line_item)
    if bridge_left:
        return f"{name}: ROS bridge peer left"
    node = re.search(r"Discovered ROS Node\s+([/\w.-]+)", line_item)
    if node:
        if "_ros2cli_daemon" in node.group(1) or node.group(1) in MONITOR_ROS_NODES:
            return ""
        return f"{name}: ROS node {node.group(1)} discovered"
    node_left = re.search(r"Undiscovered ROS Node\s+([/\w.-]+)", line_item)
    if node_left:
        if "_ros2cli_daemon" in node_left.group(1) or node_left.group(1) in MONITOR_ROS_NODES:
            return ""
        return f"{name}: ROS node {node_left.group(1)} left"
    unregistered = re.search(r"\bunregistered role=([a-z]+) room=([\w.-]+)", line_item)
    if unregistered:
        return f"{name}: {unregistered.group(1)} left room {unregistered.group(2)}"
    registered = re.search(r"\bregistered role=([a-z]+) room=([\w.-]+)", line_item)
    if registered:
        return f"{name}: {registered.group(1)} joined room {registered.group(2)}"
    direct_registered = re.search(r"WebRTC peer registered:\s*role=([a-z]+)\s+room=([\w.-]+)", line_item)
    if direct_registered:
        return f"webrtc: {direct_registered.group(1)}:{direct_registered.group(2)} registered"
    peer_ready = re.search(r"WebRTC peer ready:\s*role=([a-z]+)\s+room=([\w.-]+)", line_item)
    if peer_ready:
        return f"webrtc: {peer_ready.group(1)}:{peer_ready.group(2)} ready"
    peer_left = re.search(r"WebRTC peer left:\s*role=([a-z]+)\s+room=([\w.-]+)", line_item)
    if peer_left:
        return f"webrtc: {peer_left.group(1)}:{peer_left.group(2)} left"
    if "WebRTC signaling listening" in line_item:
        return "webrtc: signaling listener is ready"
    if "WebRTC signaling connected:" in line_item:
        return "webrtc: signaling connection established"
    if "WebRTC signaling peer connected:" in line_item:
        peer = line_item.split("WebRTC signaling peer connected:", 1)[-1].strip()
        return f"webrtc: peer connected from {peer}"
    if "WebRTC signaling peer disconnected:" in line_item:
        peer = line_item.split("WebRTC signaling peer disconnected:", 1)[-1].strip()
        return f"webrtc: peer disconnected from {peer}"
    if "WebRTC signaling disconnected" in line_item:
        return "webrtc: signaling disconnected"
    if "Incoming RTP video" in line_item:
        return "webrtc: incoming camera RTP is active"
    if "Publishing decoded WebRTC images" in line_item:
        topic = line_item.split(" to ", 1)[-1] if " to " in line_item else "ROS 2"
        return f"webrtc: decoded camera is publishing to {topic}"
    if "cmd-vel DataChannel open" in line_item:
        return "webrtc: cmd_vel data channel is open"
    if "DataChannel received" in line_item:
        return "webrtc: cmd_vel data channel received control data"
    if "signaling reconnect after error" in line_item:
        detail = line_item.split("signaling reconnect after error:", 1)[-1].strip()
        return f"webrtc: signaling reconnecting ({detail})"
    if "cmd_vel watchdog" in line_item:
        return "webrtc: cmd_vel watchdog published zero command"
    if "GStreamer recovery" in line_item:
        return "webrtc: media pipeline recovery scheduled"
    if "Robot GStreamer pipeline recovered" in line_item:
        return "webrtc: media pipeline recovered"
    if "Unable to connect" in line_item or "Failed to start" in line_item:
        return f"{name}: {line_item}"
    if "ROS image appsrc rate" in line_item:
        return f"webrtc: {line_item}"
    return f"{name}: {line_item}"


def is_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        logger.debug("Failed to detect WSL environment", exc_info=True)
        return False
    return "microsoft" in release or "wsl" in release


def enter_screen(use_alt: bool) -> str:
    prefix = "\033[?1049h" if use_alt else ""
    return prefix + "\033[?25l\033[H\033[J"


def restore_screen(use_alt: bool) -> str:
    suffix = "\033[?1049l" if use_alt else ""
    return "\033[?25h" + suffix + "\033[0m"


def interactive(args: argparse.Namespace):
    root = Path(args.root).resolve()
    env_path = Path(args.env).resolve()
    run_dir = Path(os.environ.get("HORUS_RUN_DIR", str(root / ".run"))).expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)
    monitor_pid = run_dir / "monitor.pid"
    monitor_pid.write_text(f"{os.getpid()}\n", encoding="utf-8")
    cache: Dict[str, object] = {}
    show_help = False
    show_logs = True
    old_attrs = None
    use_alt = sys.stdout.isatty() and not args.no_alt_screen
    restored = False
    last_state = None

    def restore_once():
        nonlocal restored
        if restored:
            return
        try:
            if monitor_pid.exists() and monitor_pid.read_text(encoding="utf-8").strip() == str(os.getpid()):
                monitor_pid.unlink()
        except OSError:
            pass
        if old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        sys.stdout.write(restore_screen(use_alt))
        sys.stdout.flush()
        restored = True
        if last_state is not None:
            print_exit_summary(last_state, use_color=not args.no_color)

    def stop(_signum=None, _frame=None):
        restore_once()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if sys.stdin.isatty():
        old_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    sys.stdout.write(enter_screen(use_alt))
    sys.stdout.flush()

    force_refresh = True
    try:
        while True:
            state = extract_state(root, env_path, cache, force_refresh)
            last_state = state
            force_refresh = False
            frame = render(state, args, show_help, show_logs)
            sys.stdout.write("\033[H\033[J" + frame + "\n")
            sys.stdout.flush()

            deadline = time.time() + args.interval
            while time.time() < deadline:
                wait = max(0.0, min(0.1, deadline - time.time()))
                if sys.stdin.isatty():
                    readable, _, _ = select.select([sys.stdin], [], [], wait)
                    if readable:
                        key = sys.stdin.read(1).lower()
                        if key == "q":
                            stop()
                        if key == "r":
                            force_refresh = True
                            break
                        if key == "h":
                            show_help = not show_help
                            break
                        if key == "l":
                            show_logs = not show_logs
                            break
                else:
                    time.sleep(wait)
    finally:
        restore_once()


def status_snapshot(state: Dict[str, object]) -> Dict[str, object]:
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    services = build_services(state)
    return {
        "updated_at": state.get("updated_at"),
        "role": state.get("role"),
        "role_summary": ROLE_SUMMARIES.get(str(state.get("role")), ""),
        "topology": state.get("topology"),
        "room": env.get("HORUS_ROOM", "default"),
        "ros": {
            "distro": env.get("ROS_DISTRO", ""),
            "domain_id": env.get("ROS_DOMAIN_ID", "0"),
            "namespace": env.get("ZENOH_NAMESPACE", ""),
        },
        "target": state.get("target", ""),
        "ports": state.get("ports", {}),
        "processes": {
            name: {
                "pid": process.pid,
                "running": process.running,
                "uptime": process.etime,
                "cpu_percent": process.cpu,
                "mem_percent": process.mem,
            }
            for name, process in processes.items()
        },
        "services": [
            {
                "title": service.title,
                "status": service.status,
                "detail": service.detail,
                "level": service.level,
            }
            for service in services
        ],
        "members": {
            "zenoh": named_zenoh_members(state),
            "webrtc": named_webrtc_members(state, state["logs"]),  # type: ignore[arg-type]
            "signal": state.get("signal_members", []),
        },
        "presence": room_presence(state.get("signal_members", [])),  # type: ignore[arg-type]
        "topics": state.get("topics", []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HORUS connector interactive status console")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-alt-screen", action="store_true")
    parser.add_argument("--alt-screen", action="store_true")
    parser.add_argument("--json", action="store_true", help="print a machine-readable status snapshot")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.json:
        state = extract_state(Path(args.root).resolve(), Path(args.env).resolve(), {}, True)
        print(json.dumps(status_snapshot(state), indent=2, sort_keys=True))
        return
    if args.once:
        state = extract_state(Path(args.root).resolve(), Path(args.env).resolve(), {}, True)
        print(render(state, args, show_help=True, show_logs=True))
        return
    interactive(args)


if __name__ == "__main__":
    main()
