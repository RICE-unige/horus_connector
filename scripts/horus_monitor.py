#!/usr/bin/env python3
"""Interactive terminal monitor for HORUS connector."""

from __future__ import annotations

import argparse
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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
FG = {
    "green": "\033[38;5;40m",
    "yellow": "\033[38;5;220m",
    "red": "\033[38;5;203m",
    "blue": "\033[38;5;75m",
    "cyan": "\033[38;5;80m",
    "muted": "\033[38;5;244m",
    "white": "\033[38;5;255m",
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
    return values


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
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
        return ""


def process_state(run_dir: Path, name: str) -> ProcessState:
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


def tcp_open(host: str, port: int, timeout: float = 0.35) -> Optional[bool]:
    if not host:
        return None
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_listeners(ports: Sequence[int]) -> Dict[int, str]:
    output = run_command(["ss", "-ltnp"], timeout=1.0)
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

    cmd = (
        f"cd {shell_quote(str(root))}; "
        "set +u; "
        f"source {shell_quote(str(setup))}; "
        "ros2 topic list"
    )
    output = run_command(["bash", "-lc", cmd], timeout=2.5)
    topics = sorted([line.strip() for line in output.splitlines() if line.strip().startswith("/")])
    cache["ros_topics"] = topics
    cache["ros_topics_at"] = now
    return topics


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def status_label(level: str, text: str, use_color: bool) -> str:
    palette = {
        "ok": "green",
        "warn": "yellow",
        "down": "red",
        "info": "blue",
        "idle": "muted",
    }
    return color(f"[{text}]", palette.get(level, "white"), use_color)


def truncate(text: str, width: int) -> str:
    clean = strip_ansi(text)
    if len(clean) <= width:
        return text
    if width <= 1:
        return ""
    return clean[: max(0, width - 1)] + "."


def line(width: int, char: str = "-") -> str:
    return char * max(1, width)


def render_kv(key: str, value: str, width: int, use_color: bool) -> str:
    key_text = color(key.ljust(14), "muted", use_color)
    available = max(10, width - 16)
    return f"{key_text} {truncate(value, available)}"


def service_row(state: ServiceState, width: int, use_color: bool) -> str:
    label = status_label(state.level, state.status, use_color)
    title = style(state.title.ljust(14), BOLD, use_color)
    detail_width = max(10, width - 31)
    return f"{label} {title} {truncate(state.detail, detail_width)}"


def extract_state(root: Path, env_path: Path, cache: Dict[str, object], force_refresh: bool = False) -> Dict[str, object]:
    env = load_env(root, env_path)
    run_dir = root / ".run"
    role = env.get("HORUS_ROLE", "robot")
    topology = env.get("HORUS_TOPOLOGY", "hub")
    zenoh_port = int(env.get("ZENOH_PORT", "7447") or 7447)
    signal_port = int(env.get("WEBRTC_SIGNAL_PORT", "8765") or 8765)

    processes = {
        name: process_state(run_dir, name)
        for name in ("zenoh", "webrtc", "signal")
    }
    logs = {
        "zenoh": tail(run_dir / "zenoh.log"),
        "webrtc": tail(run_dir / "webrtc.log"),
        "signal": tail(run_dir / "signal.log"),
    }
    listeners = local_listeners([zenoh_port, signal_port])
    topics = ros_topics(env, root, cache, force_refresh)

    bridge_members = unique_matches(logs["zenoh"], r"New ROS 2 bridge detected:\s*([0-9A-Fa-f]+)")
    ros_nodes = unique_matches(logs["zenoh"], r"Discovered ROS Node\s+([/\w.-]+)", limit=8)
    signal_members = []
    for line_item in logs["signal"]:
        match = re.search(r"registered role=([a-z]+) room=([\w.-]+)", line_item)
        if match:
            signal_members.append(f"{match.group(1)}:{match.group(2)}")
    signal_members = list(dict.fromkeys(signal_members))[-8:]

    ips = local_ips()
    ts_ips = tailscale_ips()
    target = connection_target(env)
    target_ports = {}
    if target:
        target_ports[zenoh_port] = tcp_open(target, zenoh_port)
        target_ports[signal_port] = tcp_open(target, signal_port)

    return {
        "env": env,
        "role": role,
        "topology": topology,
        "ports": {"zenoh": zenoh_port, "signal": signal_port},
        "processes": processes,
        "logs": logs,
        "listeners": listeners,
        "topics": topics,
        "bridge_members": bridge_members,
        "ros_nodes": ros_nodes,
        "signal_members": signal_members,
        "ips": ips,
        "tailscale_ips": ts_ips,
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
    if not zenoh.running:
        services.append(ServiceState("Zenoh", "DOWN", "process is not running", "down"))
    elif zenoh_error and "New ROS 2 bridge detected" not in "\n".join(logs["zenoh"][-20:]):
        services.append(ServiceState("Zenoh", "WARN", truncate(zenoh_error, 90), "warn"))
    elif bridge_members:
        services.append(ServiceState("Zenoh", "OK", f"ROS bridge peers: {', '.join(bridge_members[-3:])}", "ok"))
    elif topology == "direct" and role == "machine" and ports["zenoh"] in listeners:
        services.append(ServiceState("Zenoh", "LISTEN", f"waiting on tcp/0.0.0.0:{ports['zenoh']}", "info"))
    elif state["target"] and target_ports.get(ports["zenoh"]) is True:
        services.append(ServiceState("Zenoh", "OPEN", f"target tcp/{state['target']}:{ports['zenoh']} reachable", "info"))
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
    elif "cmd-vel DataChannel open" in webrtc_log or "DataChannel received" in webrtc_log:
        services.append(ServiceState("WebRTC", "CTRL", "control DataChannel connected, waiting for video", "warn"))
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


def render(state: Dict[str, object], args: argparse.Namespace, show_help: bool, show_logs: bool) -> str:
    width = max(72, shutil.get_terminal_size((100, 30)).columns)
    width = min(width, 132)
    env: Dict[str, str] = state["env"]  # type: ignore[assignment]
    processes: Dict[str, ProcessState] = state["processes"]  # type: ignore[assignment]
    logs: Dict[str, List[str]] = state["logs"]  # type: ignore[assignment]
    ports: Dict[str, int] = state["ports"]  # type: ignore[assignment]
    use_color = not args.no_color

    out: List[str] = []
    title = "HORUS Connector Console"
    subtitle = f"{state['role']} / {state['topology']} / room {env.get('HORUS_ROOM', 'default')}"
    out.append(style(title, BOLD, use_color) + color(f"  {subtitle}", "muted", use_color))
    out.append(color(line(width), "blue", use_color))
    out.append(
        " ".join(
            [
                render_inline("updated", str(state["updated_at"]), use_color),
                render_inline("domain", env.get("ROS_DOMAIN_ID", "0"), use_color),
                render_inline("ros", env.get("ROS_DISTRO", "unknown"), use_color),
                render_inline("namespace", env.get("ZENOH_NAMESPACE", "/") or "/", use_color),
                render_inline("media", env.get("WEBRTC_MEDIA_MODE", "h264"), use_color),
            ]
        )
    )
    out.append("")

    out.append(style("Services", BOLD, use_color))
    for service in build_services(state):
        out.append(service_row(service, width, use_color))
    out.append("")

    out.append(style("Runtime", BOLD, use_color))
    for name in ("zenoh", "webrtc", "signal"):
        proc = processes[name]
        if proc.running:
            detail = f"pid {proc.pid}  up {proc.etime}  cpu {proc.cpu}%  mem {proc.mem}%"
            level = "ok"
        else:
            detail = "stopped"
            level = "down" if name in {"zenoh", "webrtc"} and state["role"] != "cloud" else "idle"
        out.append(f"{status_label(level, name.upper(), use_color)} {detail}")
    out.append("")

    out.append(style("Network", BOLD, use_color))
    ips = ", ".join(state["ips"][:6]) or "-"
    tailscale = ", ".join(state["tailscale_ips"]) or "-"
    out.append(render_kv("local ip", ips, width, use_color))
    out.append(render_kv("tailscale", tailscale, width, use_color))
    if state["target"]:
        out.append(render_kv("target", str(state["target"]), width, use_color))
        target_ports: Dict[int, Optional[bool]] = state["target_ports"]  # type: ignore[assignment]
        checks = []
        for label, port in (("zenoh", ports["zenoh"]), ("webrtc", ports["signal"])):
            value = target_ports.get(port)
            checks.append(f"{label}:{port}={'open' if value else 'closed'}")
        out.append(render_kv("target ports", ", ".join(checks), width, use_color))
    else:
        listeners: Dict[int, str] = state["listeners"]  # type: ignore[assignment]
        listen = []
        for label, port in (("zenoh", ports["zenoh"]), ("webrtc", ports["signal"])):
            listen.append(f"{label}:{port}={'listening' if port in listeners else 'closed'}")
        out.append(render_kv("listeners", ", ".join(listen), width, use_color))
    out.append("")

    out.append(style("Members", BOLD, use_color))
    bridge_members: List[str] = state["bridge_members"]  # type: ignore[assignment]
    signal_members: List[str] = state["signal_members"]  # type: ignore[assignment]
    ros_nodes: List[str] = state["ros_nodes"]  # type: ignore[assignment]
    out.append(render_kv("zenoh peers", ", ".join(bridge_members) or "-", width, use_color))
    out.append(render_kv("webrtc peers", ", ".join(signal_members) or infer_webrtc_peer(logs["webrtc"]) or "-", width, use_color))
    out.append(render_kv("ros nodes", ", ".join(ros_nodes[-5:]) or "-", width, use_color))
    out.append("")

    topics: List[str] = state["topics"]  # type: ignore[assignment]
    topic_preview = ", ".join(topics[:8]) if topics else "-"
    out.append(style("ROS Topics", BOLD, use_color))
    out.append(render_kv("visible", f"{len(topics)} topic(s)", width, use_color))
    out.append(render_kv("sample", topic_preview, width, use_color))

    if show_logs:
        out.append("")
        out.append(style("Recent Events", BOLD, use_color))
        events = recent_events(logs)
        if not events:
            out.append(color("no recent events", "muted", use_color))
        for event in events[-8:]:
            out.append(truncate(event, width))

    out.append("")
    if show_help:
        out.extend(
            [
                style("Keys", BOLD, use_color),
                "q quit   r refresh ROS/topics   l toggle events   h toggle this help",
                "Use ./horus doctor <role> for one-shot connectivity checks.",
            ]
        )
    else:
        out.append(color("q quit  r refresh  l events  h help", "muted", use_color))

    return "\n".join(out)


def render_inline(key: str, value: str, use_color: bool) -> str:
    return color(f"{key}=", "muted", use_color) + style(value, BOLD, use_color)


def infer_webrtc_peer(lines: List[str]) -> str:
    text = "\n".join(lines[-120:])
    if "Incoming RTP video" in text:
        return "video connected"
    if "cmd-vel DataChannel open" in text or "DataChannel received" in text:
        return "control connected"
    if "WebRTC signaling listening" in text:
        return "waiting for peer"
    return ""


def recent_events(logs: Dict[str, List[str]]) -> List[str]:
    markers = [
        "New ROS 2 bridge detected",
        "Discovered ROS Node",
        "Unable to connect",
        "Failed to start",
        "WebRTC signaling listening",
        "registered role=",
        "DataChannel received",
        "cmd-vel DataChannel open",
        "Incoming RTP video",
        "signaling reconnect after error",
        "ROS image appsrc rate",
        "Publishing decoded WebRTC images",
    ]
    events = []
    for name, lines in logs.items():
        for line_item in lines[-80:]:
            if any(marker in line_item for marker in markers):
                events.append(f"{name}: {line_item.strip()}")
    return events[-12:]


def clear_screen() -> str:
    return "\033[?1049h\033[H\033[2J"


def restore_screen() -> str:
    return "\033[?1049l\033[0m"


def interactive(args: argparse.Namespace):
    root = Path(args.root).resolve()
    env_path = Path(args.env).resolve()
    cache: Dict[str, object] = {}
    show_help = False
    show_logs = True
    old_attrs = None
    use_alt = sys.stdout.isatty() and not args.no_alt_screen

    def stop(_signum=None, _frame=None):
        if old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        if use_alt:
            sys.stdout.write(restore_screen())
            sys.stdout.flush()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if sys.stdin.isatty():
        old_attrs = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    if use_alt:
        sys.stdout.write(clear_screen())

    force_refresh = True
    try:
        while True:
            state = extract_state(root, env_path, cache, force_refresh)
            force_refresh = False
            frame = render(state, args, show_help, show_logs)
            sys.stdout.write("\033[H\033[2J" + frame + "\n")
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
        if old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        if use_alt:
            sys.stdout.write(restore_screen())
            sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HORUS connector interactive status console")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--no-alt-screen", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.once:
        state = extract_state(Path(args.root).resolve(), Path(args.env).resolve(), {}, True)
        print(render(state, args, show_help=True, show_logs=True))
        return
    interactive(args)


if __name__ == "__main__":
    main()
