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
CLOCK_PORT = int(os.environ.get("HORUS_BENCH_CLOCK_PORT", "8899"))
CLOCK_SAMPLES = int(os.environ.get("HORUS_BENCH_CLOCK_SAMPLES", "80"))
CLOCK_DRIFT_LIMIT_MS = float(os.environ.get("HORUS_BENCH_CLOCK_DRIFT_LIMIT_MS", "5.0"))
ZENOH_PRODUCTION_ARM = os.environ.get("HORUS_BENCH_ZENOH_ARM", "quic_dgram")
ZENOH_ARMS = {
    "tcp": {"endpoint": "tcp/{host}:7447", "protocol": "tcp", "tls": False},
    "tls": {"endpoint": "tls/{host}:7447", "protocol": "tls", "tls": True},
    "quic": {"endpoint": "quic/{host}:7447?multistream=1", "protocol": "quic", "tls": True},
    "quic_dgram": {"endpoint": "quic/{host}:7447?multistream=1;mixed_rel=auto", "protocol": "quic", "tls": True},
}


NODES = {
    "wsl": {"kind": "wsl", "distro": "jazzy", "root": "/home/omotoye/horus_connector", "host": "local", "ip": "100.78.124.117"},
    "arancino": {"kind": "ssh", "alias": "arancino", "distro": "jazzy", "root": "/home/omotoye/horus_connector", "ip": "10.186.13.53"},
    "arancina": {"kind": "ssh", "alias": "arancina", "distro": "jazzy", "root": "/home/rice/horus_connector", "ip": "10.186.13.39"},
    "poke": {"kind": "ssh", "alias": "poke", "distro": "humble", "root": "/home/rice/horus_connector", "ip": "100.73.164.13"},
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
    "lan": {"sender": "arancina", "receiver": "arancino", "target": "10.186.13.53", "clock_target": "10.186.13.53"},
    "vpn": {"sender": "arancina", "receiver": "wsl", "target": "100.78.124.117", "clock_target": "100.78.124.117"},
    "cloud": {"sender": "arancina", "receiver": "wsl", "target": NODES["cloud"]["ip"], "clock_target": NODES["cloud"]["ip"], "clock_hub": "cloud", "hub": "cloud"},
}
if os.environ.get("HORUS_BENCH_VPN_SENDER"):
    PATHS["vpn"]["sender"] = os.environ["HORUS_BENCH_VPN_SENDER"]
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
        publishers: ["^/benchmark/camera$"],
        subscribers: [],
        service_servers: [],
        service_clients: [],
        action_servers: [],
        action_clients: [],
      },
      pub_max_frequencies: [".*/benchmark/camera$=30"],
      pub_priorities: [".*/benchmark/camera$=3:express"],
      reliable_routes_blocking: false,
      transient_local_cache_multiplier: 1,
    },
  },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    unicast: { qos: { enabled: true } },
    link: { tx: { queue: { batching: { enabled: false }, congestion_control: { drop: { wait_before_drop: 0 } } } } },
  },
}
"""

ZENOH_SUB_CONFIG = """{
  plugins: {
    ros2dds: {
      ros_localhost_only: true,
      ros_automatic_discovery_range: "LOCALHOST",
      allow: {
        publishers: [],
        subscribers: ["^/benchmark/camera$"],
        service_servers: [],
        service_clients: [],
        action_servers: [],
        action_clients: [],
      },
      reliable_routes_blocking: false,
      transient_local_cache_multiplier: 1,
    },
  },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    unicast: { qos: { enabled: true } },
    link: { tx: { queue: { batching: { enabled: false }, congestion_control: { drop: { wait_before_drop: 0 } } } } },
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
    unicast: { qos: { enabled: true } },
    link: { tx: { queue: { batching: { enabled: false }, congestion_control: { drop: { wait_before_drop: 0 } } } } },
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
            f"for p in {BENCH_REMOTE}/*.pid; do [ -f \"$p\" ] || continue; "
            "pid=$(cat \"$p\"); kill $pid 2>/dev/null || true; rm -f \"$p\"; done; "
            "sleep 0.5; "
            f"for p in {BENCH_REMOTE}/*.pid; do [ -f \"$p\" ] || continue; "
            "pid=$(cat \"$p\"); kill -9 $pid 2>/dev/null || true; rm -f \"$p\"; done; "
            "pkill -f '[b]enchmark_camera_ros.py' 2>/dev/null || true; "
            "pkill -f '[g]st_webrtc_h264_' 2>/dev/null || true; "
            "pkill -f '[w]ebrtc_signal_relay.py' 2>/dev/null || true; "
            "pkill -f '[z]enoh-bridge-ros2dds' 2>/dev/null || true; "
            "sleep 0.5; "
            "pkill -9 -f '[b]enchmark_camera_ros.py' 2>/dev/null || true; "
            "pkill -9 -f '[g]st_webrtc_h264_' 2>/dev/null || true; "
            "pkill -9 -f '[w]ebrtc_signal_relay.py' 2>/dev/null || true; "
            "pkill -9 -f '[z]enoh-bridge-ros2dds' 2>/dev/null || true"
        )
        try:
            node_cmd(
                node,
                f"timeout 12s bash -lc {shlex.quote(cleanup)}",
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            print(f"warning: cleanup timed out on {node}", flush=True)


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
) -> None:
    print(f"\n=== {name} ===", flush=True)
    labels: list[tuple[str, str]] = []
    pre_clock, clock_path = prepare_clock_file(name, sender, receiver, clock_target, clock_hub=clock_hub)
    try:
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
                time.sleep(2)
                start_bg(
                    sender,
                    f"{name}_pub_bridge",
                    zenoh_bridge_command(".run/bench/zenoh_pub.json5", connect_endpoint, "client", zenoh_arm, listener=False),
                    domain=domain,
                    localhost_only=1,
                )
                labels.append((sender, f"{name}_pub_bridge"))
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
        time.sleep(3)
        start_bg(sender, f"{name}_pub", pub_cmd, domain=domain, localhost_only=localhost_only)
        labels.append((sender, f"{name}_pub"))
        time.sleep(DURATION + 10)
        fetch(sender, f"{BENCH_REMOTE}/{name}_pub.json", f"{name}_pub.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_sub.json", f"{name}_sub.json")
        fetch(receiver, f"{BENCH_REMOTE}/{name}_sub.samples.json", f"{name}_sub.samples.json")
        clock = finish_clock_file(name, sender, receiver, clock_target, pre_clock, clock_hub=clock_hub)
        apply_ros_clock_correction(name, clock)
    finally:
        for node, label in reversed(labels):
            stop_label(node, label)


def webrtc_pair(name: str, sender: str, receiver: str, width: int, height: int, domain: int, *, target: str, clock_target: str, clock_hub: str | None = None, cloud: str | None = None) -> None:
    print(f"\n=== {name} ===", flush=True)
    labels: list[tuple[str, str]] = []
    pre_clock, clock_path = prepare_clock_file(name, sender, receiver, clock_target, clock_hub=clock_hub)
    signal_py = "python3"
    venv_nodes = {"wsl", "arancino"}
    machine_py = ".venv-webrtc/bin/python" if receiver in venv_nodes else "python3"
    robot_py = ".venv-webrtc/bin/python" if sender in venv_nodes else "python3"
    try:
        if cloud:
            start_bg(cloud, f"{name}_signal", f"{signal_py} scripts/webrtc_signal_relay.py --host 0.0.0.0 --port 8765", domain=0, localhost_only=1)
            labels.append((cloud, f"{name}_signal"))
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
        time.sleep(2)
        start_bg(sender, f"{name}_robot", robot_cmd, domain=domain, localhost_only=1)
        labels.append((sender, f"{name}_robot"))
        time.sleep(DURATION + 15)
        fetch(receiver, f"{BENCH_REMOTE}/{name}_latency.json", f"{name}_latency.json")
        clock = finish_clock_file(name, sender, receiver, clock_target, pre_clock, clock_hub=clock_hub)
        apply_webrtc_clock_correction(name, clock)
    finally:
        for node, label in reversed(labels):
            stop_label(node, label)


def main() -> None:
    BENCH_WIN.mkdir(parents=True, exist_ok=True)
    for artifact in BENCH_WIN.glob("modeb_*"):
        artifact.unlink()
    for artifact in BENCH_WIN.glob("*_clock*.json"):
        artifact.unlink()
    selected_resolutions = set(filter(None, os.environ.get("HORUS_BENCH_RESOLUTIONS", ",".join(RESOLUTIONS)).split(",")))
    selected_paths = set(filter(None, os.environ.get("HORUS_BENCH_PATHS", ",".join(PATHS)).split(",")))
    selected_transports = set(filter(None, os.environ.get("HORUS_BENCH_TRANSPORTS", "dds,zenoh,webrtc").split(",")))
    zenoh_arms = zenoh_arms_from_selection(selected_transports)
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
            if path != "cloud" and "dds" in selected_transports:
                conditions.append(
                    {
                        "kind": "ros",
                        "name": f"modeb_{resolution}_{path}_dds",
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
                    }
                )
                domain += 10
            cloud = info.get("hub")
            for transport_label, zenoh_arm in zenoh_arms:
                conditions.append(
                    {
                        "kind": "ros",
                        "name": f"modeb_{resolution}_{path}_{transport_label}",
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
                    }
                )
                domain += 10
            if "webrtc" in selected_transports:
                conditions.append(
                    {
                        "kind": "webrtc",
                        "name": f"modeb_{resolution}_{path}_webrtc",
                        "sender": sender,
                        "receiver": receiver,
                        "width": width,
                        "height": height,
                        "domain": domain,
                        "target": target,
                        "clock_target": clock_target,
                        "clock_hub": clock_hub,
                        "cloud": cloud,
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
                    )
                else:
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
                    )
    finally:
        stop_all(active_nodes)
    print(f"\nBenchmark artifacts: {BENCH_WIN}", flush=True)


if __name__ == "__main__":
    main()
