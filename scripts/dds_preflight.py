#!/usr/bin/env python3
"""DDS preflight diagnostics for HORUS Connector."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ENV_KEYS = [
    "ROS_DOMAIN_ID",
    "ROS_DISTRO",
    "ROS_SETUP_PATH",
    "RMW_IMPLEMENTATION",
    "ROS_LOCALHOST_ONLY",
    "ROS_AUTOMATIC_DISCOVERY_RANGE",
    "ROS_STATIC_PEERS",
    "ROS_DISCOVERY_SERVER",
    "CYCLONEDDS_URI",
    "FASTRTPS_DEFAULT_PROFILES_FILE",
]

INFRA_TOPICS = {"/rosout", "/parameter_events"}


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        data = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.detail:
            data["detail"] = self.detail
        return data


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_snapshot() -> dict[str, str]:
    snapshot = {key: os.environ.get(key, "") for key in ENV_KEYS}
    for key in ENV_KEYS:
        snapshot[f"HORUS_TERMINAL_{key}"] = os.environ.get(f"HORUS_TERMINAL_{key}", "")
    return snapshot


def run_command(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)


def load_network_snapshot(target: str = "") -> dict[str, Any]:
    interfaces: list[dict[str, Any]] = []
    code, out, err = run_command(["ip", "-j", "addr"], timeout=3.0)
    if code == 0:
        try:
            for item in json.loads(out):
                interfaces.append(
                    {
                        "name": item.get("ifname", ""),
                        "state": item.get("operstate", ""),
                        "addresses": [
                            addr.get("local", "")
                            for addr in item.get("addr_info", [])
                            if addr.get("family") == "inet" and addr.get("local")
                        ],
                    }
                )
        except json.JSONDecodeError:
            interfaces.append({"error": "failed to parse ip -j addr output"})
    else:
        interfaces.append({"error": err.strip() or "ip command unavailable"})

    route_target = target or "1.1.1.1"
    route: dict[str, str] = {"target": route_target}
    code, out, err = run_command(["ip", "route", "get", route_target], timeout=3.0)
    if code == 0:
        route["raw"] = out.strip()
        match = re.search(r"\bdev\s+(\S+)", out)
        if match:
            route["interface"] = match.group(1)
        src = re.search(r"\bsrc\s+(\S+)", out)
        if src:
            route["source"] = src.group(1)
    else:
        route["error"] = err.strip() or "route lookup failed"
    return {"hostname": socket.gethostname(), "interfaces": interfaces, "route": route}


def find_ros_setup(root: Path, env: dict[str, str]) -> str:
    configured = env.get("ROS_SETUP_PATH", "")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path)
        return ""
    distro = env.get("ROS_DISTRO") or "jazzy"
    for candidate in [Path(f"/opt/ros/{distro}/setup.bash"), Path(f"/opt/ros/{distro}/local_setup.bash")]:
        if candidate.exists():
            return str(candidate)
    return ""


def ros_topic_list(root: Path, role: str, timeout_sec: float = 8.0) -> dict[str, Any]:
    if role == "cloud":
        return {"available": False, "skipped": True, "topics": [], "reason": "cloud role does not need a local ROS graph"}
    env = os.environ.copy()
    setup = find_ros_setup(root, env)
    if setup:
        command = f"source {shlex_quote(setup)} && ros2 topic list"
        args = ["bash", "-lc", command]
    elif shutil.which("ros2"):
        args = ["ros2", "topic", "list"]
    else:
        return {"available": False, "topics": [], "error": "ros2 command not found and no ROS setup file was detected"}
    code, out, err = run_command(args, timeout=timeout_sec)
    topics = sorted({line.strip() for line in out.splitlines() if line.strip().startswith("/")})
    return {
        "available": code == 0,
        "topics": topics,
        "setup_path": setup,
        "returncode": code,
        "error": "" if code == 0 else (err.strip() or out.strip()),
    }


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def strip_json5_line_comments(text: str) -> str:
    output: list[str] = []
    in_string = False
    quote = ""
    escape = False
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            index += 1
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def extract_balanced_object(text: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*\{{", text)
    if not match:
        return ""
    start = text.find("{", match.start())
    depth = 0
    in_string = False
    quote = ""
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return ""


def extract_string_array(text: str, key: str) -> list[str] | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*\[", text)
    if not match:
        return None
    start = text.find("[", match.start())
    depth = 0
    in_string = False
    quote = ""
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                body = text[start + 1:index]
                return [m.group(2) for m in re.finditer(r"(['\"])((?:\\.|(?!\1).)*)\1", body)]
    return None


def load_allow_patterns(config_path: Path) -> dict[str, list[str] | None]:
    if not config_path.exists():
        return {}
    text = strip_json5_line_comments(config_path.read_text(encoding="utf-8"))
    allow_body = extract_balanced_object(text, "allow")
    if not allow_body:
        return {}
    result: dict[str, list[str] | None] = {}
    for key in ["publishers", "subscribers", "service_servers", "service_clients", "action_servers", "action_clients"]:
        result[key] = extract_string_array(allow_body, key)
    return result


def topic_allowed(topic: str, patterns: list[str] | None) -> bool:
    if patterns is None:
        return True
    if not patterns:
        return False
    return any(re.fullmatch(pattern, topic) or re.match(pattern, topic) for pattern in patterns)


def classify_environment(snapshot: dict[str, str], role: str, runtime: str, planned_docker_network: str) -> list[Finding]:
    findings: list[Finding] = []
    terminal_domain = snapshot.get("HORUS_TERMINAL_ROS_DOMAIN_ID", "")
    active_domain = snapshot.get("ROS_DOMAIN_ID", "")
    if terminal_domain and active_domain and terminal_domain != active_domain:
        findings.append(
            Finding(
                "error",
                "DDS_DOMAIN_MISMATCH",
                f"Launch terminal ROS_DOMAIN_ID={terminal_domain}, but connector config uses ROS_DOMAIN_ID={active_domain}.",
                "Run ./horus setup or export the same ROS_DOMAIN_ID before launch.",
            )
        )
    discovery_server = snapshot.get("ROS_DISCOVERY_SERVER", "")
    if discovery_server and role != "cloud":
        findings.append(
            Finding(
                "error",
                "DISCOVERY_SERVER_UNSUPPORTED",
                "ROS_DISCOVERY_SERVER is set; zenoh-bridge-ros2dds cannot join a FastDDS discovery-server graph.",
                "Run the robot ROS graph with normal DDS discovery for the bridge, or expose a separate ROS graph that the bridge can discover.",
            )
        )
    terminal_discovery_server = snapshot.get("HORUS_TERMINAL_ROS_DISCOVERY_SERVER", "")
    if terminal_discovery_server and terminal_discovery_server != discovery_server and role != "cloud":
        findings.append(
            Finding(
                "warning",
                "TERMINAL_DISCOVERY_SERVER_CHANGED",
                "The launch terminal had ROS_DISCOVERY_SERVER set before .env was loaded, but the active connector environment differs.",
                f"terminal={terminal_discovery_server!r}, active={discovery_server!r}",
            )
        )
    if runtime == "docker" and planned_docker_network != "host":
        findings.append(
            Finding(
                "error",
                "DOCKER_NETWORK_NOT_HOST",
                "Zenoh bridge Docker runtime must use host networking for DDS discovery.",
                f"planned network mode: {planned_docker_network}",
            )
        )
    profile = snapshot.get("FASTRTPS_DEFAULT_PROFILES_FILE", "")
    rmw = snapshot.get("RMW_IMPLEMENTATION", "")
    if profile:
        profile_text = ""
        path = Path(profile).expanduser()
        if path.exists() and path.is_file():
            try:
                profile_text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                profile_text = ""
        if "SHM" in profile_text.upper() or "SHARED" in profile_text.upper() or "fastrtps" in rmw.lower():
            findings.append(
                Finding(
                    "warning",
                    "FASTDDS_SHM_CONTAINER_WARNING",
                    "FastDDS profile/runtime detected. If robot nodes are split across containers, shared-memory transport can make topics visible while data does not flow.",
                    "Use host networking plus --ipc=host, or disable FastDDS SHM in the robot profile when crossing container boundaries.",
                )
            )
    return findings


def classify_topics(
    role: str,
    topics: list[str],
    allow_patterns: dict[str, list[str] | None],
    zenoh_config: str,
) -> list[Finding]:
    findings: list[Finding] = []
    if role == "cloud":
        return findings
    substantive = sorted(topic for topic in topics if topic not in INFRA_TOPICS)
    if topics and not substantive:
        findings.append(
            Finding(
                "warning",
                "ONLY_INFRASTRUCTURE_TOPICS",
                "Only ROS infrastructure topics are visible in the launch environment.",
                "This often means the robot graph is on another domain, another DDS discovery mechanism, or another network interface.",
            )
        )
    if not topics:
        findings.append(
            Finding(
                "warning",
                "NO_TERMINAL_TOPICS",
                "No ROS topics were visible from the launch environment.",
                "If the robot is already running, check ROS_DOMAIN_ID, ROS setup sourcing, discovery server settings, and DDS interface selection.",
            )
        )
        return findings
    if not allow_patterns:
        return findings
    if role not in {"robot", "teammate"}:
        return findings
    interface = "publishers"
    patterns = allow_patterns.get(interface)
    if patterns is None:
        return findings
    filtered = [topic for topic in substantive if not topic_allowed(topic, patterns)]
    if not filtered:
        return findings
    severity = "error" if len(filtered) == len(substantive) and substantive else "warning"
    sample = ", ".join(filtered[:8])
    if len(filtered) > 8:
        sample += f", ... ({len(filtered)} total)"
    pattern_sample = ", ".join(patterns[:6]) if patterns else "<none>"
    if patterns and len(patterns) > 6:
        pattern_sample += f", ... ({len(patterns)} total)"
    findings.append(
        Finding(
            severity,
            "HORUS_FILTERED_TOPIC",
            f"{len(filtered)} visible topic(s) are blocked by {Path(zenoh_config).name} allow.{interface}.",
            f"blocked: {sample}; allowed patterns: {pattern_sample}",
        )
    )
    return findings


def parse_bridge_log(run_dir: Path) -> dict[str, Any]:
    log_path = run_dir / "zenoh.log"
    if not log_path.exists():
        return {"available": False, "path": str(log_path), "publisher_routes": [], "subscriber_routes": [], "nodes": []}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return {"available": False, "path": str(log_path), "error": str(exc), "publisher_routes": [], "subscriber_routes": [], "nodes": []}
    publisher_routes: set[str] = set()
    subscriber_routes: set[str] = set()
    nodes: set[str] = set()
    for line in lines[-2000:]:
        node = re.search(r"Discovered ROS Node\s+([/\w.-]+)", line)
        if node:
            nodes.add(node.group(1))
        pub = re.search(r"Route Publisher\s+\(ROS:([^\s,)]+)", line)
        if pub:
            publisher_routes.add(pub.group(1))
        sub = re.search(r"Route Subscriber\s+\(ROS:([^\s,)]+)", line)
        if sub:
            subscriber_routes.add(sub.group(1))
    stat = log_path.stat()
    return {
        "available": True,
        "path": str(log_path),
        "mtime": stat.st_mtime,
        "publisher_routes": sorted(publisher_routes),
        "subscriber_routes": sorted(subscriber_routes),
        "nodes": sorted(nodes),
    }


def classify_bridge_log(role: str, topics: list[str], bridge_log: dict[str, Any]) -> list[Finding]:
    if not bridge_log.get("available"):
        return []
    findings: list[Finding] = []
    substantive = sorted(topic for topic in topics if topic not in INFRA_TOPICS)
    if role in {"robot", "teammate"}:
        routes = set(bridge_log.get("publisher_routes") or [])
        missing = [topic for topic in substantive if topic not in routes]
        if substantive and missing:
            sample = ", ".join(missing[:8])
            if len(missing) > 8:
                sample += f", ... ({len(missing)} total)"
            findings.append(
                Finding(
                    "warning",
                    "BRIDGE_ROUTE_MISSING",
                    f"{len(missing)} terminal-visible topic(s) do not appear in current bridge publisher routes.",
                    f"missing: {sample}; log: {bridge_log.get('path', '')}",
                )
            )
    elif role == "machine":
        routes = bridge_log.get("subscriber_routes") or []
        if not routes:
            findings.append(
                Finding(
                    "warning",
                    "MACHINE_IMPORT_ROUTES_NOT_SEEN",
                    "No bridge subscriber/import routes were found in the current machine zenoh log.",
                    "If robot peers are connected and topics are still absent, check machine allow.subscribers, namespace, and Zenoh peer connectivity.",
                )
            )
    return findings


def infer_interface(snapshot: dict[str, str], network: dict[str, Any]) -> tuple[str, bool]:
    explicit = os.environ.get("HORUS_DDS_INTERFACE", "").strip()
    if explicit:
        return explicit, True
    localhost_only = snapshot.get("ROS_LOCALHOST_ONLY", "")
    discovery_range = snapshot.get("ROS_AUTOMATIC_DISCOVERY_RANGE", "")
    if truthy(localhost_only) or discovery_range.upper() == "LOCALHOST":
        return "lo", True
    route = network.get("route", {}) if isinstance(network.get("route"), dict) else {}
    interface = str(route.get("interface") or "")
    return interface, bool(interface)


def render_cyclonedds_template(root: Path, run_dir: Path, snapshot: dict[str, str], network: dict[str, Any]) -> str:
    template = root / "config" / "cyclonedds_connector.xml.template"
    out = run_dir / "cyclonedds_connector.xml"
    run_dir.mkdir(parents=True, exist_ok=True)
    interface, has_interface = infer_interface(snapshot, network)
    if has_interface:
        interfaces = (
            "<Interfaces>\n"
            f'        <NetworkInterface name="{xml_escape(interface)}" priority="default" multicast="default" />\n'
            "      </Interfaces>"
        )
    else:
        interfaces = "<!-- No interface override selected. CycloneDDS will use its default interface selection. -->"
    allow_multicast = "true"
    text = template.read_text(encoding="utf-8")
    text = text.replace("{{INTERFACES}}", interfaces)
    text = text.replace("{{ALLOW_MULTICAST}}", allow_multicast)
    out.write_text(text, encoding="utf-8")
    return str(out)


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_verdict(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    snapshot = env_snapshot()
    network = load_network_snapshot(args.target)
    cyclonedds = render_cyclonedds_template(root, run_dir, snapshot, network)
    zenoh_config = Path(args.zenoh_config).expanduser().resolve() if args.zenoh_config else Path()
    allow_patterns = load_allow_patterns(zenoh_config) if zenoh_config else {}

    findings = []
    if args.mode == "render":
        topic_probe = {"available": False, "skipped": True, "topics": [], "reason": "render mode"}
        bridge_log = parse_bridge_log(run_dir)
    else:
        topic_probe = ros_topic_list(root, args.role, args.topic_timeout)
        bridge_log = parse_bridge_log(run_dir)
        findings.extend(classify_environment(snapshot, args.role, args.runtime, args.docker_network))
        findings.extend(classify_topics(args.role, topic_probe.get("topics", []), allow_patterns, str(zenoh_config)))
        if args.mode == "doctor":
            findings.extend(classify_bridge_log(args.role, topic_probe.get("topics", []), bridge_log))
        if not topic_probe.get("available", False) and args.role != "cloud":
            findings.append(
                Finding(
                    "warning",
                    "ROS_TOPIC_PROBE_UNAVAILABLE",
                    "Could not run 'ros2 topic list' from the connector launch environment.",
                    str(topic_probe.get("error") or topic_probe.get("reason") or ""),
                )
            )

    return {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role": args.role,
        "mode": args.mode,
        "runtime": args.runtime,
        "topology": args.topology,
        "room": os.environ.get("HORUS_ROOM", "default"),
        "zenoh_config": str(zenoh_config) if zenoh_config else "",
        "cyclonedds_config": cyclonedds,
        "environment": snapshot,
        "network": network,
        "topic_probe": topic_probe,
        "bridge_log": bridge_log,
        "allow_patterns": allow_patterns,
        "findings": [finding.as_dict() for finding in findings],
    }


def print_pretty(verdict: dict[str, Any]) -> None:
    findings = verdict.get("findings", [])
    topic_probe = verdict.get("topic_probe", {})
    topics = topic_probe.get("topics", [])
    print("DDS preflight")
    print(f"  role       {verdict.get('role')}")
    print(f"  runtime    {verdict.get('runtime')}")
    print(f"  domain     {verdict.get('environment', {}).get('ROS_DOMAIN_ID') or '0'}")
    print(f"  cyclonedds {verdict.get('cyclonedds_config')}")
    if topic_probe.get("skipped"):
        print("  ros graph   skipped")
    elif topic_probe.get("available"):
        print(f"  ros graph   {len(topics)} topic(s) visible")
    else:
        print("  ros graph   unavailable")
    if not findings:
        print("  verdict    ok")
        return
    print("  findings")
    for finding in findings:
        print(f"    {finding['severity'].upper()} {finding['code']}: {finding['message']}")
        if finding.get("detail"):
            print(f"      {finding['detail']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--role", default=os.environ.get("HORUS_ROLE", "robot"))
    parser.add_argument("--mode", choices=["launch", "doctor", "render"], default="doctor")
    parser.add_argument("--runtime", default=os.environ.get("ZENOH_BRIDGE_RUNTIME", "binary"))
    parser.add_argument("--topology", default=os.environ.get("HORUS_TOPOLOGY", "hub"))
    parser.add_argument("--target", default="")
    parser.add_argument("--zenoh-config", default="")
    parser.add_argument("--docker-network", default="host")
    parser.add_argument("--topic-timeout", type=float, default=float(os.environ.get("HORUS_DDS_PREFLIGHT_TOPIC_TIMEOUT", "5.0")))
    parser.add_argument("--output-json", default="")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verdict = build_verdict(args)
    out = Path(args.output_json).expanduser() if args.output_json else Path(args.run_dir).expanduser() / "dds_preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(verdict, indent=2, sort_keys=True), encoding="utf-8")

    env_out = Path(args.run_dir).expanduser() / "dds_env.json"
    env_out.write_text(
        json.dumps(
            {
                "created_at": verdict["created_at"],
                "role": verdict["role"],
                "runtime": verdict["runtime"],
                "environment": verdict["environment"],
                "network": verdict["network"],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if args.pretty:
        print_pretty(verdict)
    if args.mode == "render":
        print(verdict["cyclonedds_config"])
        return 0
    has_errors = any(finding.get("severity") == "error" for finding in verdict.get("findings", []))
    if has_errors and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
