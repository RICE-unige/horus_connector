#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import shlex
import subprocess
import time


ROOT_WIN = r"\\wsl.localhost\Ubuntu\home\omotoye\horus_connector"
ROOT_POSIX = "~/horus_connector"
LOCAL_RUN_WIN = Path(ROOT_WIN) / ".run"
BENCH_WIN = LOCAL_RUN_WIN / "distributed_benchmark"
BENCH_REMOTE = ".run/bench"
DURATION = float(os.environ.get("HORUS_BENCH_DURATION", "300"))
REPETITIONS = int(os.environ.get("HORUS_BENCH_REPETITIONS", "1"))
FPS = 30
DEADLINE_MS = 150
QOS_DEPTH = 1
CLOCK_PORT = int(os.environ.get("HORUS_BENCH_CLOCK_PORT", "8765"))
CLOCK_SAMPLES = int(os.environ.get("HORUS_BENCH_CLOCK_SAMPLES", "80"))
CLOCK_DRIFT_LIMIT_MS = float(os.environ.get("HORUS_BENCH_CLOCK_DRIFT_LIMIT_MS", "5.0"))
RESOURCE_INTERVAL_SEC = float(os.environ.get("HORUS_BENCH_RESOURCE_INTERVAL_SEC", "1.0"))
CONTROL_RATE_HZ = float(os.environ.get("HORUS_BENCH_CONTROL_RATE_HZ", "20.0"))
ZENOH_PRODUCTION_ARM = os.environ.get("HORUS_BENCH_ZENOH_ARM", "quic_dgram")
ZENOH_ARMS = {
    "tcp": {"endpoint": "tcp/{host}:7447", "protocol": "tcp", "tls": False},
    "tls": {"endpoint": "tls/{host}:7447", "protocol": "tls", "tls": True},
    "quic": {"endpoint": "quic/{host}:7447?multistream=1", "protocol": "quic", "tls": True},
    "quic_dgram": {"endpoint": "quic/{host}:7447?multistream=1;mixed_rel=auto", "protocol": "quic", "tls": True},
}

NETWORK_PROFILES = {
    "unconstrained": {"bandwidth_mbps": None, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "bw40": {"bandwidth_mbps": 40.0, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "bw20": {"bandwidth_mbps": 20.0, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "bw10": {"bandwidth_mbps": 10.0, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "bw5": {"bandwidth_mbps": 5.0, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "bw2": {"bandwidth_mbps": 2.0, "loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "loss1": {"bandwidth_mbps": None, "loss_percent": 1.0, "delay_ms": 0.0, "jitter_ms": 0.0},
    "loss3": {"bandwidth_mbps": None, "loss_percent": 3.0, "delay_ms": 0.0, "jitter_ms": 0.0},
}


NODES = {
    "wsl": {"kind": "wsl", "distro": "jazzy", "root": "/home/omotoye/horus_connector", "host": "local", "ip": "100.70.153.10"},
    "arancino": {"kind": "ssh", "alias": "arancino", "distro": "jazzy", "root": "/home/omotoye/horus_connector", "ip": "10.186.13.53"},
    "arancina": {"kind": "ssh", "alias": "arancina", "distro": "jazzy", "root": "/home/rice/horus_connector", "ip": "10.186.13.39"},
    "poke": {"kind": "ssh", "alias": "poke", "distro": "humble", "root": "/home/rice/horus_connector", "ip": "10.186.13.16"},
    "cloud": {"kind": "ssh", "alias": "googlecloud", "distro": "", "root": "/home/adeko/horus_connector", "ip": "34.7.220.13"},
}

for node_name, info in NODES.items():
    prefix = f"HORUS_BENCH_{node_name.upper()}_"
    info["root"] = os.environ.get(prefix + "ROOT", info["root"])
    info["ip"] = os.environ.get(prefix + "IP", info["ip"])
    if info["kind"] == "ssh":
        info["alias"] = os.environ.get(prefix + "ALIAS", info["alias"])

RESOLUTIONS = {
    "1080p30": (1920, 1080),
    "720p30": (1280, 720),
}

PATHS = {
    "lan": {"sender": "arancina", "receiver": "poke", "target": NODES["poke"]["ip"], "clock_target": NODES["poke"]["ip"]},
    "vpn": {"sender": "arancina", "receiver": "wsl", "target": NODES["wsl"]["ip"], "clock_target": NODES["wsl"]["ip"]},
    "cloud": {"sender": "arancina", "receiver": "wsl", "target": NODES["cloud"]["ip"], "clock_target": NODES["cloud"]["ip"], "clock_hub": "cloud", "hub": "cloud"},
}
if os.environ.get("HORUS_BENCH_VPN_SENDER"):
    PATHS["vpn"]["sender"] = os.environ["HORUS_BENCH_VPN_SENDER"]
if os.environ.get("HORUS_BENCH_VPN_RECEIVER"):
    PATHS["vpn"]["receiver"] = os.environ["HORUS_BENCH_VPN_RECEIVER"]
    PATHS["vpn"]["target"] = NODES[PATHS["vpn"]["receiver"]]["ip"]
    PATHS["vpn"]["clock_target"] = NODES[PATHS["vpn"]["receiver"]]["ip"]
if os.environ.get("HORUS_BENCH_LAN_SENDER"):
    PATHS["lan"]["sender"] = os.environ["HORUS_BENCH_LAN_SENDER"]
if os.environ.get("HORUS_BENCH_LAN_RECEIVER"):
    PATHS["lan"]["receiver"] = os.environ["HORUS_BENCH_LAN_RECEIVER"]
    PATHS["lan"]["target"] = NODES[PATHS["lan"]["receiver"]]["ip"]
    PATHS["lan"]["clock_target"] = NODES[PATHS["lan"]["receiver"]]["ip"]


ZENOH_PUB_CONFIG = """{
  plugins: {
    ros2dds: {
      ros_localhost_only: true,
      ros_automatic_discovery_range: "LOCALHOST",
      allow: {
        publishers: ["^/benchmark/camera$", "^/benchmark/cmd_vel_ack$"],
        subscribers: ["^/benchmark/cmd_vel$"],
        service_servers: [],
        service_clients: [],
        action_servers: [],
        action_clients: [],
      },
      pub_max_frequencies: [".*/benchmark/camera$=30"],
      pub_priorities: [".*/benchmark/camera$=5", ".*/benchmark/cmd_vel_ack$=2:express"],
      reliable_routes_blocking: false,
      transient_local_cache_multiplier: 1,
    },
  },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    unicast: {
      qos: { enabled: true },
    },
    link: {
      tx: {
        queue: {
          batching: { enabled: false },
          congestion_control: { drop: { wait_before_drop: 0 } },
        },
      },
    },
  },
}
"""

ZENOH_SUB_CONFIG = """{
  plugins: {
    ros2dds: {
      ros_localhost_only: true,
      ros_automatic_discovery_range: "LOCALHOST",
      allow: {
        publishers: ["^/benchmark/cmd_vel$"],
        subscribers: ["^/benchmark/camera$", "^/benchmark/cmd_vel_ack$"],
        service_servers: [],
        service_clients: [],
        action_servers: [],
        action_clients: [],
      },
      reliable_routes_blocking: false,
      pub_priorities: [".*/benchmark/cmd_vel$=1:express"],
      transient_local_cache_multiplier: 1,
    },
  },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    unicast: {
      qos: { enabled: true },
    },
    link: {
      tx: {
        queue: {
          batching: { enabled: false },
          congestion_control: { drop: { wait_before_drop: 0 } },
        },
      },
    },
  },
}
"""

ZENOH_CLOUD_CONFIG = """{
  plugins: {
    ros2dds: {
      ros_localhost_only: true,
      ros_automatic_discovery_range: "LOCALHOST",
      allow: {
        publishers: [],
        subscribers: [],
        service_servers: [],
        service_clients: [],
        action_servers: [],
        action_clients: [],
      },
    },
  },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    unicast: {
      qos: { enabled: true },
    },
    link: {
      tx: {
        queue: {
          batching: { enabled: false },
          congestion_control: { drop: { wait_before_drop: 0 } },
        },
      },
    },
  },
}
"""


def node_cmd(node: str, script: str, *, input_text: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    info = NODES[node]
    if info["kind"] == "wsl":
        if os.name == "nt":
            script = script.replace("$", "\\$")
            cmd = ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", script]
        else:
            cmd = ["bash", "-lc", script]
    else:
        cmd = ["ssh", info["alias"], script]
    return subprocess.run(cmd, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def checked(node: str, script: str, *, input_text: str | None = None, timeout: float | None = None) -> str:
    proc = node_cmd(node, script, input_text=input_text, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{node} command failed ({proc.returncode}):\n{script}\n--- output ---\n{proc.stdout}")
    return proc.stdout


def sudo_bash(node: str, body: str) -> str:
    password = os.environ.get(f"HORUS_BENCH_{node.upper()}_SUDO_PASSWORD") or os.environ.get("HORUS_BENCH_SUDO_PASSWORD")
    quoted = shlex.quote(body)
    if password:
        return f"printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' bash -lc {quoted}"
    return f"sudo -n bash -lc {quoted}"


def python_bin_expr() -> str:
    return "$(if [ -x .venv-webrtc/bin/python ]; then printf %s .venv-webrtc/bin/python; else printf %s python3; fi)"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def parse_json_output(output: str) -> dict:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"no JSON object found in output:\n{output}")
    return json.loads(output[start : end + 1])


def write_local_json(name: str, payload: dict) -> None:
    BENCH_WIN.mkdir(parents=True, exist_ok=True)
    (BENCH_WIN / name).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def network_profile_suffix(profile_name: str) -> str:
    return "" if profile_name == "unconstrained" else f"_{profile_name}"


def profile_needs_shaping(profile_name: str) -> bool:
    profile = NETWORK_PROFILES[profile_name]
    return bool(profile["bandwidth_mbps"] or profile["loss_percent"] or profile["delay_ms"] or profile["jitter_ms"])


def bash_prefix(node: str, domain: int, localhost_only: int) -> str:
    distro = NODES[node]["distro"]
    source = f"source /opt/ros/{distro}/setup.bash" if distro else "true"
    return (
        f"cd {shlex.quote(NODES[node]['root'])}; "
        f"{source}; "
        f"export ROS_DOMAIN_ID={domain}; "
        f"export ROS_LOCALHOST_ONLY={localhost_only}; "
        f"export ROS_AUTOMATIC_DISCOVERY_RANGE={'LOCALHOST' if localhost_only else 'SUBNET'}; "
    )


def setup_node(node: str) -> None:
    checked(
        node,
        f"cd {shlex.quote(NODES[node]['root'])}; "
        f"mkdir -p {BENCH_REMOTE}; "
        f"find {BENCH_REMOTE} -maxdepth 1 -type f -name 'modeb_*' -delete; "
        f"find {BENCH_REMOTE} -maxdepth 1 -type f -name 'cmd_*' -delete; "
        f"find {BENCH_REMOTE} -maxdepth 1 -type f -name '*_resources.csv' -delete; "
        f"find {BENCH_REMOTE} -maxdepth 1 -type f -name '*_network.json' -delete; "
        f"find {BENCH_REMOTE} -maxdepth 1 -type f -name '*_clock*.json' -delete",
        timeout=120,
    )
    put_text(node, f"{BENCH_REMOTE}/zenoh_pub.json5", ZENOH_PUB_CONFIG)
    put_text(node, f"{BENCH_REMOTE}/zenoh_sub.json5", ZENOH_SUB_CONFIG)
    put_text(node, f"{BENCH_REMOTE}/zenoh_cloud.json5", ZENOH_CLOUD_CONFIG)


def put_text(node: str, path: str, text: str) -> None:
    checked(
        node,
        f"cd {shlex.quote(NODES[node]['root'])}; mkdir -p {shlex.quote(str(Path(path).parent))}; cat > {shlex.quote(path)}",
        input_text=text,
        timeout=30,
    )


def zenoh_endpoint(arm: str, host: str) -> str:
    if arm not in ZENOH_ARMS:
        raise ValueError(f"unknown Zenoh arm: {arm}")
    return ZENOH_ARMS[arm]["endpoint"].format(host=host)


def rendered_zenoh_config(base_config: str, arm: str, *, listener: bool) -> str:
    if not ZENOH_ARMS[arm]["tls"]:
        return base_config
    suffix = "listener" if listener else "client"
    return f"{base_config}.{arm}.{suffix}.json5"


def render_zenoh_config_command(base_config: str, arm: str, *, listener: bool) -> str:
    out_config = rendered_zenoh_config(base_config, arm, listener=listener)
    if not ZENOH_ARMS[arm]["tls"]:
        return f"test -f {shlex.quote(base_config)}"
    required = (
        'test -n "${ZENOH_TLS_ROOT_CA:-}" && test -f "${ZENOH_TLS_ROOT_CA:-}"'
        if not listener
        else 'test -n "${ZENOH_TLS_LISTEN_KEY:-}" && test -f "${ZENOH_TLS_LISTEN_KEY:-}" && test -n "${ZENOH_TLS_LISTEN_CERT:-}" && test -f "${ZENOH_TLS_LISTEN_CERT:-}"'
    )
    return (
        "set -e; "
        "source .zenoh_tls_profile.env 2>/dev/null || true; "
        f"{required}; "
        "python3 scripts/render_zenoh_config.py "
        f"--base {shlex.quote(base_config)} --out {shlex.quote(out_config)} "
        '--root-ca "${ZENOH_TLS_ROOT_CA:-}" '
        '--listen-private-key "${ZENOH_TLS_LISTEN_KEY:-}" '
        '--listen-certificate "${ZENOH_TLS_LISTEN_CERT:-}" '
        "--verify-name-on-connect 0"
    )


def zenoh_bridge_command(config: str, endpoint: str, mode: str, arm: str, *, listener: bool) -> str:
    rendered = rendered_zenoh_config(config, arm, listener=listener)
    return (
        f"{render_zenoh_config_command(config, arm, listener=listener)}; "
        f"./zenoh-bridge-ros2dds -c {shlex.quote(rendered)} "
        f"{'-l' if listener else '-e'} {shlex.quote(endpoint)} {mode}"
    )


def assert_process_alive(node: str, label: str) -> None:
    checked(
        node,
        f"cd {shlex.quote(NODES[node]['root'])}; "
        f"pid=$(cat {BENCH_REMOTE}/{label}.pid); ps -p $pid >/dev/null",
        timeout=10,
    )


def assert_zenoh_transport(labels: list[tuple[str, str]], arm: str) -> None:
    protocol = ZENOH_ARMS[arm]["protocol"]
    needle = f"{protocol}/"
    for node, label in labels:
        assert_process_alive(node, label)
        log = checked(
            node,
            f"cd {shlex.quote(NODES[node]['root'])}; tail -n 160 {BENCH_REMOTE}/{label}.log",
            timeout=10,
        )
        if needle not in log:
            raise RuntimeError(f"{label} on {node} did not log expected Zenoh transport {needle}.\n{log}")


def start_bg(node: str, label: str, command: str, *, domain: int = 0, localhost_only: int = 1) -> int:
    prefix = bash_prefix(node, domain, localhost_only) if NODES[node]["distro"] else f"cd {shlex.quote(NODES[node]['root'])}; "
    wrapped = (
        f"mkdir -p {BENCH_REMOTE}; "
        f"nohup bash -lc {shlex.quote(prefix + command)} "
        f"> {BENCH_REMOTE}/{label}.log 2>&1 < /dev/null & "
        f"echo $! > {BENCH_REMOTE}/{label}.pid; cat {BENCH_REMOTE}/{label}.pid"
    )
    out = checked(node, f"cd {shlex.quote(NODES[node]['root'])}; {wrapped}", timeout=30).strip()
    return int(out.splitlines()[-1])


def stop_label(node: str, label: str) -> None:
    node_cmd(
        node,
        f"cd {shlex.quote(NODES[node]['root'])}; "
        f"if [ -f {BENCH_REMOTE}/{label}.pid ]; then "
        f"pid=$(cat {BENCH_REMOTE}/{label}.pid); kill $pid 2>/dev/null || true; sleep 0.5; "
        f"kill -9 $pid 2>/dev/null || true; rm -f {BENCH_REMOTE}/{label}.pid; fi",
        timeout=10,
    )


def stop_all(nodes: set[str] | None = None) -> None:
    for node in nodes or set(NODES):
        cleanup = (
            f"cd {shlex.quote(NODES[node]['root'])} 2>/dev/null || exit 0; "
            "sudo -n systemctl stop horus-cloud.service 2>/dev/null || true; "
            f"for p in {BENCH_REMOTE}/*.pid; do [ -f \"$p\" ] || continue; "
            "pid=$(cat \"$p\"); kill $pid 2>/dev/null || true; rm -f \"$p\"; done; "
            "sleep 0.5; "
            f"for p in {BENCH_REMOTE}/*.pid; do [ -f \"$p\" ] || continue; "
            "pid=$(cat \"$p\"); kill -9 $pid 2>/dev/null || true; rm -f \"$p\"; done; "
            "pkill -f '[b]enchmark_camera_ros.py' 2>/dev/null || true; "
            "pkill -f '[g]st_webrtc_h264_' 2>/dev/null || true; "
            "pkill -f '[w]ebrtc_signal_relay.py' 2>/dev/null || true; "
            "pkill -f '[z]enoh-bridge-ros2dds' 2>/dev/null || true; "
            "pkill -f '[h]orus-cloud-supervisor' 2>/dev/null || true; "
            "sleep 0.5; "
            "pkill -9 -f '[b]enchmark_camera_ros.py' 2>/dev/null || true; "
            "pkill -9 -f '[g]st_webrtc_h264_' 2>/dev/null || true; "
            "pkill -9 -f '[w]ebrtc_signal_relay.py' 2>/dev/null || true; "
            "pkill -9 -f '[z]enoh-bridge-ros2dds' 2>/dev/null || true; "
            "pkill -9 -f '[h]orus-cloud-supervisor' 2>/dev/null || true"
        )
        try:
            node_cmd(
                node,
                f"timeout 12s bash -lc {shlex.quote(cleanup)}",
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            print(f"warning: cleanup timed out on {node}", flush=True)


def route_interface(node: str, target_ip: str) -> str:
    output = checked(
        node,
        f"ip route get {shlex.quote(target_ip)} | awk '{{for (i=1; i<=NF; i++) if ($i == \"dev\") {{print $(i+1); exit}}}}'",
        timeout=10,
    ).strip()
    if not output:
        raise RuntimeError(f"could not determine route interface from {node} to {target_ip}")
    return output.splitlines()[0].strip()


def clear_network_profile(node: str, iface: str | None) -> None:
    if not iface:
        return
    node_cmd(node, sudo_bash(node, f"tc qdisc del dev {shlex.quote(iface)} root 2>/dev/null || true"), timeout=20)


def apply_network_profile(node: str, target_ip: str, profile_name: str) -> dict:
    if profile_name not in NETWORK_PROFILES:
        raise ValueError(f"unknown network profile: {profile_name}")
    profile = NETWORK_PROFILES[profile_name]
    iface = route_interface(node, target_ip)
    payload = {
        "profile": profile_name,
        "sender": node,
        "target_ip": target_ip,
        "interface": iface,
        **profile,
        "active": profile_needs_shaping(profile_name),
    }
    clear_network_profile(node, iface)
    if not payload["active"]:
        payload["tc_qdisc"] = checked(node, f"tc -s qdisc show dev {shlex.quote(iface)}", timeout=10)
        return payload

    bandwidth = profile["bandwidth_mbps"]
    loss = float(profile["loss_percent"])
    delay = float(profile["delay_ms"])
    jitter = float(profile["jitter_ms"])
    netem_parts: list[str] = []
    if loss > 0:
        netem_parts.append(f"loss {loss:g}%")
    if delay > 0:
        netem_parts.append(f"delay {delay:g}ms {jitter:g}ms" if jitter > 0 else f"delay {delay:g}ms")

    if bandwidth:
        lines = [
            f"tc qdisc replace dev {shlex.quote(iface)} root handle 1: htb default 10",
            (
                f"tc class replace dev {shlex.quote(iface)} parent 1: classid 1:10 "
                f"htb rate {bandwidth:g}mbit ceil {bandwidth:g}mbit burst 64k cburst 64k"
            ),
        ]
        if netem_parts:
            lines.append(f"tc qdisc replace dev {shlex.quote(iface)} parent 1:10 handle 10: netem {' '.join(netem_parts)}")
    else:
        lines = [f"tc qdisc replace dev {shlex.quote(iface)} root netem {' '.join(netem_parts)}"]
    script = "set -e; " + "; ".join(lines)
    checked(node, sudo_bash(node, script), timeout=30)
    payload["tc_qdisc"] = checked(node, f"tc -s qdisc show dev {shlex.quote(iface)}", timeout=10)
    return payload


def fetch(node: str, remote_path: str, local_name: str) -> None:
    dest = BENCH_WIN / local_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = NODES[node]
    if info["kind"] == "wsl":
        src = Path(ROOT_WIN) / remote_path.replace("/", "\\")
        if src.exists():
            dest.write_bytes(src.read_bytes())
        return
    subprocess.run(["scp", f"{info['alias']}:{NODES[node]['root']}/{remote_path}", str(dest)], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def start_resource_samplers(name: str, node_labels: dict[str, list[str]], duration: float) -> list[tuple[str, str, str]]:
    samplers: list[tuple[str, str, str]] = []
    for node, labels in sorted(node_labels.items()):
        if not labels:
            continue
        sampler_label = f"{name}_{node}_resources"
        remote_csv = f"{BENCH_REMOTE}/{sampler_label}.csv"
        pid_args = " ".join(
            f"--pid-label {shlex.quote(label + '=' + BENCH_REMOTE + '/' + label + '.pid')}" for label in sorted(set(labels))
        )
        command = (
            f"python3 scripts/sample_process_metrics.py --output {shlex.quote(remote_csv)} "
            f"--interval {RESOURCE_INTERVAL_SEC:g} --duration {duration:g} {pid_args}"
        )
        start_bg(node, sampler_label, command, domain=0, localhost_only=1)
        samplers.append((node, sampler_label, remote_csv))
    return samplers


def stop_and_fetch_samplers(samplers: list[tuple[str, str, str]]) -> None:
    for node, label, _remote_csv in samplers:
        stop_label(node, label)
    for node, label, remote_csv in samplers:
        fetch(node, remote_csv, f"{label}.csv")


def run_clock_probe(name: str, phase: str, sender: str, receiver: str, receiver_ip: str) -> dict:
    label = f"{name}_clock_{phase}"
    start_bg(
        receiver,
        label,
        f"python3 scripts/clock_offset_probe.py --server --host 0.0.0.0 --port {CLOCK_PORT}",
        domain=0,
        localhost_only=1,
    )
    time.sleep(1.0)
    try:
        output = checked(
            sender,
            f"cd {shlex.quote(NODES[sender]['root'])}; "
            f"python3 scripts/clock_offset_probe.py --host {shlex.quote(receiver_ip)} --port {CLOCK_PORT} --samples {CLOCK_SAMPLES}",
            timeout=max(20, CLOCK_SAMPLES * 0.2),
        )
    finally:
        stop_label(receiver, label)
    payload = parse_json_output(output)
    payload.update({"phase": phase, "sender": sender, "receiver": receiver, "receiver_ip": receiver_ip})
    write_local_json(f"{name}_clock_{phase}.json", payload)
    return payload


def run_hub_clock_probe(name: str, phase: str, sender: str, receiver: str, hub: str, hub_ip: str) -> dict:
    label = f"{name}_clock_{phase}_hub"
    start_bg(
        hub,
        label,
        f"python3 scripts/clock_offset_probe.py --server --host 0.0.0.0 --port {CLOCK_PORT}",
        domain=0,
        localhost_only=1,
    )
    time.sleep(1.0)
    try:
        sender_output = checked(
            sender,
            f"cd {shlex.quote(NODES[sender]['root'])}; "
            f"python3 scripts/clock_offset_probe.py --host {shlex.quote(hub_ip)} --port {CLOCK_PORT} --samples {CLOCK_SAMPLES}",
            timeout=max(20, CLOCK_SAMPLES * 0.2),
        )
        receiver_output = checked(
            receiver,
            f"cd {shlex.quote(NODES[receiver]['root'])}; "
            f"python3 scripts/clock_offset_probe.py --host {shlex.quote(hub_ip)} --port {CLOCK_PORT} --samples {CLOCK_SAMPLES}",
            timeout=max(20, CLOCK_SAMPLES * 0.2),
        )
    finally:
        stop_label(hub, label)
    sender_to_hub = parse_json_output(sender_output)
    receiver_to_hub = parse_json_output(receiver_output)
    offset = float(sender_to_hub["offset_ms"]) - float(receiver_to_hub["offset_ms"])
    payload = {
        "phase": phase,
        "method": "hub_reference_clock_offset_probe",
        "offset_ms": offset,
        "sender": sender,
        "receiver": receiver,
        "hub": hub,
        "hub_ip": hub_ip,
        "sender_to_hub": sender_to_hub,
        "receiver_to_hub": receiver_to_hub,
        "best_rtt_ms": max(float(sender_to_hub.get("best_rtt_ms", 0.0)), float(receiver_to_hub.get("best_rtt_ms", 0.0))),
    }
    write_local_json(f"{name}_clock_{phase}.json", payload)
    return payload


def prepare_clock_file(name: str, sender: str, receiver: str, receiver_ip: str, *, clock_hub: str | None = None) -> tuple[dict, str]:
    pre = (
        run_hub_clock_probe(name, "pre", sender, receiver, clock_hub, receiver_ip)
        if clock_hub
        else run_clock_probe(name, "pre", sender, receiver, receiver_ip)
    )
    remote_path = f"{BENCH_REMOTE}/{name}_clock_pre.json"
    put_text(receiver, remote_path, json.dumps(pre, indent=2, sort_keys=True))
    return pre, remote_path


def finish_clock_file(name: str, sender: str, receiver: str, receiver_ip: str, pre: dict, *, clock_hub: str | None = None) -> dict:
    post = (
        run_hub_clock_probe(name, "post", sender, receiver, clock_hub, receiver_ip)
        if clock_hub
        else run_clock_probe(name, "post", sender, receiver, receiver_ip)
    )
    offset = (float(pre["offset_ms"]) + float(post["offset_ms"])) / 2.0
    drift = abs(float(pre["offset_ms"]) - float(post["offset_ms"]))
    payload = {
        "offset_ms": offset,
        "drift_ms": drift,
        "drift_limit_ms": CLOCK_DRIFT_LIMIT_MS,
        "method": "mean_of_pre_and_post_clock_offset_probes",
        "clock_topology": "hub_reference" if clock_hub else "direct_pair",
        "pre": pre,
        "post": post,
        "sender": sender,
        "receiver": receiver,
        "receiver_ip": receiver_ip,
    }
    write_local_json(f"{name}_clock.json", payload)
    if drift > CLOCK_DRIFT_LIMIT_MS:
        raise RuntimeError(f"{name} clock drift {drift:.2f} ms exceeded {CLOCK_DRIFT_LIMIT_MS:.2f} ms")
    return payload


def recompute_samples(samples_payload: dict, clock: dict) -> tuple[list[dict], list[float], int]:
    offset = float(clock["offset_ms"])
    fresh_count = 0
    latencies: list[float] = []
    for sample in samples_payload.get("samples", []):
        raw = sample.get("raw_latency_ms")
        if raw is None:
            raw = sample.get("latency_ms")
        if raw is None:
            continue
        latency = float(raw) - offset
        sample["latency_ms"] = latency
        sample["fresh"] = latency <= DEADLINE_MS
        sample["stale"] = latency > DEADLINE_MS
        if sample["fresh"]:
            fresh_count += 1
        latencies.append(latency)
    samples_payload["clock_offset_ms"] = offset
    samples_payload["clock_offset_source"] = "mean_pre_post_clock_offset_probe"
    samples_payload["clock_drift_ms"] = clock["drift_ms"]
    samples_payload["clock_offset_pre_ms"] = clock["pre"]["offset_ms"]
    samples_payload["clock_offset_post_ms"] = clock["post"]["offset_ms"]
    return samples_payload.get("samples", []), latencies, fresh_count


def apply_ros_clock_correction(name: str, clock: dict) -> None:
    sub_path = BENCH_WIN / f"{name}_sub.json"
    samples_path = BENCH_WIN / f"{name}_sub.samples.json"
    if not sub_path.exists() or not samples_path.exists():
        raise RuntimeError(f"{name} missing ROS benchmark artifacts")
    sub = json.loads(sub_path.read_text(encoding="utf-8"))
    samples_payload = json.loads(samples_path.read_text(encoding="utf-8"))
    _samples, latencies, fresh_count = recompute_samples(samples_payload, clock)
    estimated = int(sub.get("estimated_published_frames") or sub.get("messages") or len(latencies))
    observed = float(sub.get("observed_sec") or sub.get("elapsed_sec") or DURATION)
    stale_count = max(0, len(latencies) - fresh_count)
    sub.update(
        {
            "clock_offset_ms": clock["offset_ms"],
            "clock_offset_source": "mean_pre_post_clock_offset_probe",
            "clock_drift_ms": clock["drift_ms"],
            "clock_offset_pre_ms": clock["pre"]["offset_ms"],
            "clock_offset_post_ms": clock["post"]["offset_ms"],
            "fresh_messages": fresh_count,
            "stale_messages": stale_count,
            "fresh_frame_sla": fresh_count / estimated if estimated else 0.0,
            "fresh_frame_sla_percent": (fresh_count / estimated * 100.0) if estimated else 0.0,
            "usable_fps": fresh_count / observed if observed else 0.0,
            "latency_ms_median": percentile(latencies, 0.50),
            "latency_ms_p50": percentile(latencies, 0.50),
            "latency_ms_p95": percentile(latencies, 0.95),
            "latency_ms_p99": percentile(latencies, 0.99),
        }
    )
    sub_path.write_text(json.dumps(sub, indent=2, sort_keys=True), encoding="utf-8")
    samples_path.write_text(json.dumps(samples_payload, indent=2, sort_keys=True), encoding="utf-8")


def apply_webrtc_clock_correction(name: str, clock: dict) -> None:
    path = BENCH_WIN / f"{name}_latency.json"
    if not path.exists():
        raise RuntimeError(f"{name} missing WebRTC latency artifact")
    payload = json.loads(path.read_text(encoding="utf-8"))
    _samples, latencies, fresh_count = recompute_samples(payload, clock)
    stale_count = max(0, len(latencies) - fresh_count)
    observed = float(payload.get("video_observed_sec") or DURATION)
    payload.update(
        {
            "clock_offset_ms": clock["offset_ms"],
            "clock_offset_source": "mean_pre_post_clock_offset_probe",
            "clock_drift_ms": clock["drift_ms"],
            "clock_offset_pre_ms": clock["pre"]["offset_ms"],
            "clock_offset_post_ms": clock["post"]["offset_ms"],
            "video_latency_samples": len(latencies),
            "video_latency_ms_median": percentile(latencies, 0.50),
            "video_latency_ms_p50": percentile(latencies, 0.50),
            "video_latency_ms_p95": percentile(latencies, 0.95),
            "video_latency_ms_p99": percentile(latencies, 0.99),
            "fresh_latency_samples": fresh_count,
            "stale_latency_samples": stale_count,
            "fresh_sample_sla": fresh_count / len(latencies) if latencies else 0.0,
            "fresh_fps_estimate": fresh_count / observed if observed else 0.0,
        }
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def zenoh_arms_from_selection(selected_transports: set[str]) -> list[tuple[str, str]]:
    arms: list[tuple[str, str]] = []
    if "zenoh" in selected_transports:
        arms.append(("zenoh", ZENOH_PRODUCTION_ARM))
    token_map = {
        "zenoh-tcp": ("zenoh_tcp", "tcp"),
        "zenoh-tls": ("zenoh_tls", "tls"),
        "zenoh-quic": ("zenoh_quic", "quic"),
        "zenoh-quic-dgram": ("zenoh_quic_dgram", "quic_dgram"),
        "zenoh-quic_dgram": ("zenoh_quic_dgram", "quic_dgram"),
    }
    for token, value in token_map.items():
        if token in selected_transports and value not in arms:
            arms.append(value)
    return arms


def ros_pair(
    name: str,
    sender: str,
    receiver: str,
    profile: str,
    width: int,
    height: int,
    domain: int,
    *,
    transport: str,
    target: str | None = None,
    cloud: str | None = None,
    clock_target: str,
    clock_hub: str | None = None,
    zenoh_arm: str = "tcp",
    network_profile: str = "unconstrained",
) -> None:
    print(f"\n=== {name} ===", flush=True)
    labels: list[tuple[str, str]] = []
    resource_labels: dict[str, list[str]] = {}
    samplers: list[tuple[str, str, str]] = []
    network_state: dict | None = None
    pre_clock: dict | None = None
    try:
        network_state = apply_network_profile(sender, clock_target, network_profile)
        write_local_json(f"{name}_network.json", network_state)
        pre_clock, clock_path = prepare_clock_file(name, sender, receiver, clock_target, clock_hub=clock_hub)
        if transport == "zenoh":
            if zenoh_arm not in ZENOH_ARMS:
                raise ValueError(f"unknown Zenoh arm: {zenoh_arm}")
            if cloud:
                listen_endpoint = zenoh_endpoint(zenoh_arm, "0.0.0.0")
                connect_endpoint = zenoh_endpoint(zenoh_arm, target)
                start_bg(
                    cloud,
                    f"{name}_cloud_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_cloud.json5", listen_endpoint, "router", zenoh_arm, listener=True),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((cloud, f"{name}_cloud_bridge"))
                resource_labels.setdefault(cloud, []).append(f"{name}_cloud_bridge")
                time.sleep(2)
                start_bg(
                    receiver,
                    f"{name}_sub_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_sub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                start_bg(
                    sender,
                    f"{name}_pub_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_pub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                labels.extend([(receiver, f"{name}_sub_bridge"), (sender, f"{name}_pub_bridge")])
                resource_labels.setdefault(receiver, []).append(f"{name}_sub_bridge")
                resource_labels.setdefault(sender, []).append(f"{name}_pub_bridge")
            else:
                listen_endpoint = zenoh_endpoint(zenoh_arm, "0.0.0.0")
                connect_endpoint = zenoh_endpoint(zenoh_arm, target)
                start_bg(
                    receiver,
                    f"{name}_sub_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_sub.json5", listen_endpoint, "router", zenoh_arm, listener=True),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((receiver, f"{name}_sub_bridge"))
                resource_labels.setdefault(receiver, []).append(f"{name}_sub_bridge")
                time.sleep(2)
                start_bg(
                    sender,
                    f"{name}_pub_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_pub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((sender, f"{name}_pub_bridge"))
                resource_labels.setdefault(sender, []).append(f"{name}_pub_bridge")
            time.sleep(5)
            assert_zenoh_transport(labels, zenoh_arm)

        localhost_only = 0 if transport == "dds" else 1
        sub_cmd = (
            f"python3 scripts/benchmark_camera_ros.py sub --profile {profile} --topic /benchmark/camera "
            f"--width {width} --height {height} --fps {FPS} --duration {DURATION + 5:.0f} --qos-depth {QOS_DEPTH} "
            f"--fresh-deadline-ms {DEADLINE_MS} --json {BENCH_REMOTE}/{name}_sub.json "
            f"--clock-offset-json {clock_path} --samples-json {BENCH_REMOTE}/{name}_sub.samples.json"
        )
        pub_cmd = (
            f"python3 scripts/benchmark_camera_ros.py pub --profile {profile} --topic /benchmark/camera "
            f"--width {width} --height {height} --fps {FPS} --duration {DURATION:.0f} --qos-depth {QOS_DEPTH} "
            f"--jpeg-quality 72 --scene textured --json {BENCH_REMOTE}/{name}_pub.json"
        )
        start_bg(receiver, f"{name}_sub", sub_cmd, domain=domain, localhost_only=localhost_only)
        labels.append((receiver, f"{name}_sub"))
        resource_labels.setdefault(receiver, []).append(f"{name}_sub")
        time.sleep(3)
        start_bg(sender, f"{name}_pub", pub_cmd, domain=domain, localhost_only=localhost_only)
        labels.append((sender, f"{name}_pub"))
        resource_labels.setdefault(sender, []).append(f"{name}_pub")
        samplers = start_resource_samplers(name, resource_labels, DURATION + 15)
        time.sleep(DURATION + 10)
        fetch(sender, f"{BENCH_REMOTE}/{name}_pub.json", f"{name}_pub.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_sub.json", f"{name}_sub.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_sub.samples.json", f"{name}_sub.samples.json")
        stop_and_fetch_samplers(samplers)
        samplers = []
        clock = finish_clock_file(name, sender, receiver, clock_target, pre_clock, clock_hub=clock_hub)
        apply_ros_clock_correction(name, clock)
    finally:
        if samplers:
            stop_and_fetch_samplers(samplers)
        for node, label in reversed(labels):
            stop_label(node, label)
        if network_state:
            clear_network_profile(sender, network_state.get("interface"))


def webrtc_pair(
    name: str,
    sender: str,
    receiver: str,
    width: int,
    height: int,
    domain: int,
    *,
    target: str,
    clock_target: str,
    clock_hub: str | None = None,
    cloud: str | None = None,
    network_profile: str = "unconstrained",
) -> None:
    print(f"\n=== {name} ===", flush=True)
    labels: list[tuple[str, str]] = []
    resource_labels: dict[str, list[str]] = {}
    samplers: list[tuple[str, str, str]] = []
    network_state: dict | None = None
    pre_clock: dict | None = None
    signal_py = "python3"
    machine_py = python_bin_expr()
    robot_py = python_bin_expr()
    try:
        network_state = apply_network_profile(sender, clock_target, network_profile)
        write_local_json(f"{name}_network.json", network_state)
        pre_clock, clock_path = prepare_clock_file(name, sender, receiver, clock_target, clock_hub=clock_hub)
        if cloud:
            start_bg(cloud, f"{name}_signal", f"{signal_py} scripts/webrtc_signal_relay.py --host 0.0.0.0 --port 8765", domain=0, localhost_only=1)
            labels.append((cloud, f"{name}_signal"))
            resource_labels.setdefault(cloud, []).append(f"{name}_signal")
            time.sleep(2)
            machine_signal = f"ws://{target}:8765"
            robot_signal = f"ws://{target}:8765"
            machine_cmd = (
                f"PYTHONUNBUFFERED=1 {machine_py} scripts/gst_webrtc_h264_machine.py --signaling-url {machine_signal} --room {name} "
                f"--profile .webrtc_profile.env --ice-servers stun:stun.l.google.com:19302 "
                f"--video-sink fakesink --video-output ros2 --ros-image-topic /{name}/camera/webrtc/image_raw "
                f"--cmd-rate 0 --duration {DURATION + 8:.0f} --fresh-deadline-ms {DEADLINE_MS} "
                f"--clock-offset-json {clock_path} "
                f"--latency-json {BENCH_REMOTE}/{name}_latency.json"
            )
        else:
            machine_cmd = (
                f"PYTHONUNBUFFERED=1 {machine_py} scripts/gst_webrtc_h264_machine.py --host 0.0.0.0 --port 8765 --room {name} "
                f"--profile .webrtc_profile.env --ice-servers stun:stun.l.google.com:19302 "
                f"--video-sink fakesink --video-output ros2 --ros-image-topic /{name}/camera/webrtc/image_raw "
                f"--cmd-rate 0 --duration {DURATION + 8:.0f} --fresh-deadline-ms {DEADLINE_MS} "
                f"--clock-offset-json {clock_path} "
                f"--latency-json {BENCH_REMOTE}/{name}_latency.json"
            )
            robot_signal = f"ws://{target}:8765"

        start_bg(receiver, f"{name}_machine", machine_cmd, domain=domain, localhost_only=1)
        labels.append((receiver, f"{name}_machine"))
        resource_labels.setdefault(receiver, []).append(f"{name}_machine")
        time.sleep(4)
        fake_cmd = (
            f"python3 scripts/benchmark_camera_ros.py pub --profile raw --topic /benchmark/webrtc_camera "
            f"--width {width} --height {height} --fps {FPS} --duration {DURATION + 3:.0f} "
            f"--qos-depth {QOS_DEPTH} --scene textured --json {BENCH_REMOTE}/{name}_fake_pub.json"
        )
        robot_cmd = (
            f"PYTHONUNBUFFERED=1 {robot_py} scripts/gst_webrtc_h264_robot.py --signaling-url {robot_signal} --room {name} "
            f"--profile .webrtc_profile.env --ice-servers stun:stun.l.google.com:19302 "
            f"--video-source ros2 --ros-image-topic /benchmark/webrtc_camera --ros-image-qos default "
            f"--width {width} --height {height} --fps {FPS} --video-bitrate-kbit 6000 "
            f"--adaptive-bitrate 1 --duration {DURATION:.0f} --ros-cmd-topic '' "
            f"--latency-probe --latency-probe-rate {FPS}"
        )
        start_bg(sender, f"{name}_fake", fake_cmd, domain=domain, localhost_only=1)
        labels.append((sender, f"{name}_fake"))
        resource_labels.setdefault(sender, []).append(f"{name}_fake")
        time.sleep(2)
        start_bg(sender, f"{name}_robot", robot_cmd, domain=domain, localhost_only=1)
        labels.append((sender, f"{name}_robot"))
        resource_labels.setdefault(sender, []).append(f"{name}_robot")
        samplers = start_resource_samplers(name, resource_labels, DURATION + 20)
        time.sleep(DURATION + 15)
        fetch(receiver, f"{BENCH_REMOTE}/{name}_latency.json", f"{name}_latency.json")
        stop_and_fetch_samplers(samplers)
        samplers = []
        clock = finish_clock_file(name, sender, receiver, clock_target, pre_clock, clock_hub=clock_hub)
        apply_webrtc_clock_correction(name, clock)
    finally:
        if samplers:
            stop_and_fetch_samplers(samplers)
        for node, label in reversed(labels):
            stop_label(node, label)
        if network_state:
            clear_network_profile(sender, network_state.get("interface"))


def control_pair(
    name: str,
    sender: str,
    receiver: str,
    domain: int,
    *,
    transport: str,
    target: str | None,
    cloud: str | None = None,
    clock_target: str,
    zenoh_arm: str = "tcp",
    network_profile: str = "unconstrained",
    video_load: tuple[int, int] | None = None,
) -> None:
    print(f"\n=== {name} ===", flush=True)
    labels: list[tuple[str, str]] = []
    resource_labels: dict[str, list[str]] = {}
    samplers: list[tuple[str, str, str]] = []
    network_state: dict | None = None
    try:
        network_state = apply_network_profile(sender, clock_target, network_profile)
        write_local_json(f"{name}_network.json", network_state)
        if transport == "zenoh":
            if zenoh_arm not in ZENOH_ARMS:
                raise ValueError(f"unknown Zenoh arm: {zenoh_arm}")
            if cloud:
                listen_endpoint = zenoh_endpoint(zenoh_arm, "0.0.0.0")
                connect_endpoint = zenoh_endpoint(zenoh_arm, target)
                start_bg(
                    cloud,
                    f"{name}_cloud_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_cloud.json5", listen_endpoint, "router", zenoh_arm, listener=True),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((cloud, f"{name}_cloud_bridge"))
                resource_labels.setdefault(cloud, []).append(f"{name}_cloud_bridge")
                time.sleep(2)
                start_bg(
                    receiver,
                    f"{name}_machine_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_sub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                start_bg(
                    sender,
                    f"{name}_robot_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_pub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                labels.extend([(receiver, f"{name}_machine_bridge"), (sender, f"{name}_robot_bridge")])
                resource_labels.setdefault(receiver, []).append(f"{name}_machine_bridge")
                resource_labels.setdefault(sender, []).append(f"{name}_robot_bridge")
            else:
                listen_endpoint = zenoh_endpoint(zenoh_arm, "0.0.0.0")
                connect_endpoint = zenoh_endpoint(zenoh_arm, target)
                start_bg(
                    receiver,
                    f"{name}_machine_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_sub.json5", listen_endpoint, "router", zenoh_arm, listener=True),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((receiver, f"{name}_machine_bridge"))
                resource_labels.setdefault(receiver, []).append(f"{name}_machine_bridge")
                time.sleep(2)
                start_bg(
                    sender,
                    f"{name}_robot_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_pub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((sender, f"{name}_robot_bridge"))
                resource_labels.setdefault(sender, []).append(f"{name}_robot_bridge")
            time.sleep(5)
            assert_zenoh_transport(labels, zenoh_arm)

        localhost_only = 0 if transport == "dds" else 1
        robot_cmd = (
            f"python3 scripts/benchmark_cmd_vel_ros.py robot --cmd-topic /benchmark/cmd_vel "
            f"--ack-topic /benchmark/cmd_vel_ack --duration {DURATION + 8:.0f} --qos-depth {QOS_DEPTH} "
            f"--json {BENCH_REMOTE}/{name}_robot_ack.json"
        )
        machine_cmd = (
            f"python3 scripts/benchmark_cmd_vel_ros.py machine --cmd-topic /benchmark/cmd_vel "
            f"--ack-topic /benchmark/cmd_vel_ack --rate-hz {CONTROL_RATE_HZ:g} --duration {DURATION:.0f} "
            f"--qos-depth {QOS_DEPTH} --json {BENCH_REMOTE}/{name}_cmd.json "
            f"--samples-json {BENCH_REMOTE}/{name}_cmd.samples.json"
        )
        start_bg(sender, f"{name}_robot_ack", robot_cmd, domain=domain, localhost_only=localhost_only)
        labels.append((sender, f"{name}_robot_ack"))
        resource_labels.setdefault(sender, []).append(f"{name}_robot_ack")
        time.sleep(2)

        if video_load:
            width, height = video_load
            load_sub_cmd = (
                f"python3 scripts/benchmark_camera_ros.py sub --profile compressed --topic /benchmark/camera "
                f"--width {width} --height {height} --fps {FPS} --duration {DURATION + 5:.0f} --qos-depth {QOS_DEPTH} "
                f"--fresh-deadline-ms {DEADLINE_MS} --json {BENCH_REMOTE}/{name}_load_sub.json "
                f"--samples-json {BENCH_REMOTE}/{name}_load_sub.samples.json"
            )
            load_pub_cmd = (
                f"python3 scripts/benchmark_camera_ros.py pub --profile compressed --topic /benchmark/camera "
                f"--width {width} --height {height} --fps {FPS} --duration {DURATION:.0f} --qos-depth {QOS_DEPTH} "
                f"--jpeg-quality 72 --scene textured --json {BENCH_REMOTE}/{name}_load_pub.json"
            )
            start_bg(receiver, f"{name}_load_sub", load_sub_cmd, domain=domain, localhost_only=localhost_only)
            labels.append((receiver, f"{name}_load_sub"))
            resource_labels.setdefault(receiver, []).append(f"{name}_load_sub")
            time.sleep(2)
            start_bg(sender, f"{name}_load_pub", load_pub_cmd, domain=domain, localhost_only=localhost_only)
            labels.append((sender, f"{name}_load_pub"))
            resource_labels.setdefault(sender, []).append(f"{name}_load_pub")

        start_bg(receiver, f"{name}_machine_cmd", machine_cmd, domain=domain, localhost_only=localhost_only)
        labels.append((receiver, f"{name}_machine_cmd"))
        resource_labels.setdefault(receiver, []).append(f"{name}_machine_cmd")
        samplers = start_resource_samplers(name, resource_labels, DURATION + 15)
        time.sleep(DURATION + 10)
        fetch(sender, f"{BENCH_REMOTE}/{name}_robot_ack.json", f"{name}_robot_ack.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_cmd.json", f"{name}_cmd.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_cmd.samples.json", f"{name}_cmd.samples.json")
        if video_load:
            fetch(sender, f"{BENCH_REMOTE}/{name}_load_pub.json", f"{name}_load_pub.json")
            fetch(receiver, f"{BENCH_REMOTE}/{name}_load_sub.json", f"{name}_load_sub.json")
            fetch(receiver, f"{BENCH_REMOTE}/{name}_load_sub.samples.json", f"{name}_load_sub.samples.json")
        stop_and_fetch_samplers(samplers)
        samplers = []
    finally:
        if samplers:
            stop_and_fetch_samplers(samplers)
        for node, label in reversed(labels):
            stop_label(node, label)
        if network_state:
            clear_network_profile(sender, network_state.get("interface"))


def main() -> None:
    BENCH_WIN.mkdir(parents=True, exist_ok=True)
    for artifact in BENCH_WIN.glob("modeb_*"):
        artifact.unlink()
    for artifact in BENCH_WIN.glob("cmd_*"):
        artifact.unlink()
    for artifact in BENCH_WIN.glob("*_resources.csv"):
        artifact.unlink()
    for artifact in BENCH_WIN.glob("*_network.json"):
        artifact.unlink()
    for artifact in BENCH_WIN.glob("*_clock*.json"):
        artifact.unlink()
    selected_resolutions = set(filter(None, os.environ.get("HORUS_BENCH_RESOLUTIONS", ",".join(RESOLUTIONS)).split(",")))
    selected_paths = set(filter(None, os.environ.get("HORUS_BENCH_PATHS", ",".join(PATHS)).split(",")))
    selected_transports = set(filter(None, os.environ.get("HORUS_BENCH_TRANSPORTS", "dds,zenoh,webrtc").split(",")))
    selected_network_profiles = list(
        filter(None, os.environ.get("HORUS_BENCH_NETWORK_PROFILES", "unconstrained").split(","))
    )
    unknown_profiles = [profile for profile in selected_network_profiles if profile not in NETWORK_PROFILES]
    if unknown_profiles:
        raise ValueError(f"unknown network profiles: {', '.join(unknown_profiles)}")
    run_control = os.environ.get("HORUS_BENCH_CONTROL", "0").lower() in {"1", "true", "yes", "on"}
    control_with_video = os.environ.get("HORUS_BENCH_CONTROL_WITH_VIDEO", "0").lower() in {"1", "true", "yes", "on"}
    selected_control_transports = set(filter(None, os.environ.get("HORUS_BENCH_CONTROL_TRANSPORTS", "zenoh").split(",")))
    zenoh_arms = zenoh_arms_from_selection(selected_transports)
    control_zenoh_arms = zenoh_arms_from_selection(selected_control_transports)
    active_nodes = {"wsl"}
    for path, info in PATHS.items():
        if path not in selected_paths:
            continue
        active_nodes.add(info["sender"])
        active_nodes.add(info["receiver"])
        if info.get("hub"):
            active_nodes.add(info["hub"])

    stop_all(active_nodes)
    for node in sorted(active_nodes):
        setup_node(node)

    metadata = {
        "duration_sec": DURATION,
        "repetitions": REPETITIONS,
        "fps": FPS,
        "deadline_ms": DEADLINE_MS,
        "qos_depth": QOS_DEPTH,
        "clock_samples": CLOCK_SAMPLES,
        "clock_drift_limit_ms": CLOCK_DRIFT_LIMIT_MS,
        "zenoh_production_arm": ZENOH_PRODUCTION_ARM,
        "selected_transports": sorted(selected_transports),
        "selected_network_profiles": selected_network_profiles,
        "run_control": run_control,
        "control_with_video": control_with_video,
        "selected_control_transports": sorted(selected_control_transports),
        "nodes": NODES,
        "paths": PATHS,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (BENCH_WIN / "distributed_metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    conditions: list[dict] = []
    domain = 90
    for resolution, (width, height) in RESOLUTIONS.items():
        if resolution not in selected_resolutions:
            continue
        for path, info in PATHS.items():
            if path not in selected_paths:
                continue
            sender = info["sender"]
            receiver = info["receiver"]
            target = info["target"]
            clock_target = info["clock_target"]
            clock_hub = info.get("clock_hub")
            cloud = info.get("hub")
            for network_profile in selected_network_profiles:
                suffix = network_profile_suffix(network_profile)
                if path != "cloud" and "dds" in selected_transports:
                    conditions.append(
                        {
                            "kind": "ros",
                            "name": f"modeb_{resolution}_{path}_dds{suffix}",
                            "sender": sender,
                            "receiver": receiver,
                            "profile": "compressed",
                            "width": width,
                            "height": height,
                            "domain": domain,
                            "transport": "dds",
                            "target": target,
                            "clock_target": clock_target,
                            "clock_hub": clock_hub,
                            "network_profile": network_profile,
                        }
                    )
                    domain += 10
                for transport_label, zenoh_arm in zenoh_arms:
                    conditions.append(
                        {
                            "kind": "ros",
                            "name": f"modeb_{resolution}_{path}_{transport_label}{suffix}",
                            "sender": sender,
                            "receiver": receiver,
                            "profile": "compressed",
                            "width": width,
                            "height": height,
                            "domain": domain,
                            "transport": "zenoh",
                            "target": target,
                            "clock_target": clock_target,
                            "clock_hub": clock_hub,
                            "cloud": cloud,
                            "zenoh_arm": zenoh_arm,
                            "network_profile": network_profile,
                        }
                    )
                    domain += 10
                if "webrtc" in selected_transports:
                    conditions.append(
                        {
                            "kind": "webrtc",
                            "name": f"modeb_{resolution}_{path}_webrtc{suffix}",
                            "sender": sender,
                            "receiver": receiver,
                            "width": width,
                            "height": height,
                            "domain": domain,
                            "target": target,
                            "clock_target": clock_target,
                            "clock_hub": clock_hub,
                            "cloud": cloud,
                            "network_profile": network_profile,
                        }
                    )
                    domain += 10
    if run_control:
        control_resolution = os.environ.get("HORUS_BENCH_CONTROL_LOAD_RESOLUTION", "1080p30")
        if control_resolution not in RESOLUTIONS:
            raise ValueError(f"unknown control load resolution: {control_resolution}")
        video_load = RESOLUTIONS[control_resolution] if control_with_video else None
        for path, info in PATHS.items():
            if path not in selected_paths:
                continue
            sender = info["sender"]
            receiver = info["receiver"]
            target = info["target"]
            clock_target = info["clock_target"]
            cloud = info.get("hub")
            for network_profile in selected_network_profiles:
                suffix = network_profile_suffix(network_profile)
                load_suffix = "_with_video" if control_with_video else ""
                if path != "cloud" and "dds" in selected_control_transports:
                    conditions.append(
                        {
                            "kind": "control",
                            "name": f"cmd_{path}_dds{load_suffix}{suffix}",
                            "sender": sender,
                            "receiver": receiver,
                            "domain": domain,
                            "transport": "dds",
                            "target": target,
                            "clock_target": clock_target,
                            "network_profile": network_profile,
                            "video_load": video_load,
                        }
                    )
                    domain += 10
                for transport_label, zenoh_arm in control_zenoh_arms:
                    conditions.append(
                        {
                            "kind": "control",
                            "name": f"cmd_{path}_{transport_label}{load_suffix}{suffix}",
                            "sender": sender,
                            "receiver": receiver,
                            "domain": domain,
                            "transport": "zenoh",
                            "target": target,
                            "cloud": cloud,
                            "clock_target": clock_target,
                            "network_profile": network_profile,
                            "zenoh_arm": zenoh_arm,
                            "video_load": video_load,
                        }
                    )
                    domain += 10
    seed = int(os.environ.get("HORUS_BENCH_SHUFFLE_SEED", "20260705"))
    try:
        for rep in range(1, REPETITIONS + 1):
            ordered = list(conditions)
            random.Random(seed + rep).shuffle(ordered)
            for condition in ordered:
                name = condition["name"] if REPETITIONS == 1 else f"{condition['name']}_rep{rep:02d}"
                if condition["kind"] == "ros":
                    ros_pair(
                        name,
                        condition["sender"],
                        condition["receiver"],
                        condition["profile"],
                        condition["width"],
                        condition["height"],
                        condition["domain"] + rep,
                        transport=condition["transport"],
                        target=condition.get("target"),
                        cloud=condition.get("cloud"),
                        clock_target=condition["clock_target"],
                        clock_hub=condition.get("clock_hub"),
                        zenoh_arm=condition.get("zenoh_arm", "tcp"),
                        network_profile=condition.get("network_profile", "unconstrained"),
                    )
                elif condition["kind"] == "webrtc":
                    webrtc_pair(
                        name,
                        condition["sender"],
                        condition["receiver"],
                        condition["width"],
                        condition["height"],
                        condition["domain"] + rep,
                        target=condition["target"],
                        clock_target=condition["clock_target"],
                        clock_hub=condition.get("clock_hub"),
                        cloud=condition.get("cloud"),
                        network_profile=condition.get("network_profile", "unconstrained"),
                    )
                else:
                    control_pair(
                        name,
                        condition["sender"],
                        condition["receiver"],
                        condition["domain"] + rep,
                        transport=condition["transport"],
                        target=condition.get("target"),
                        cloud=condition.get("cloud"),
                        clock_target=condition["clock_target"],
                        zenoh_arm=condition.get("zenoh_arm", "tcp"),
                        network_profile=condition.get("network_profile", "unconstrained"),
                        video_load=condition.get("video_load"),
                    )
    finally:
        stop_all(active_nodes)
    print(f"\nBenchmark artifacts: {BENCH_WIN}", flush=True)


if __name__ == "__main__":
    main()
