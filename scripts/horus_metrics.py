#!/usr/bin/env python3
"""Small HTTP metrics exporter for HORUS Connector."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict

from horus_monitor import build_services, extract_state, status_snapshot


def metric_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def render_prometheus(root: Path, env: Path) -> str:
    state = extract_state(root, env, {}, True)
    snapshot = status_snapshot(state)
    processes: Dict[str, Dict[str, object]] = snapshot["processes"]  # type: ignore[assignment]
    services = build_services(state)
    lines = [
        "# HELP horus_service_up 1 when a local service process is running.",
        "# TYPE horus_service_up gauge",
    ]
    for name, process in processes.items():
        lines.append(f'horus_service_up{{service="{name}"}} {1 if process["running"] else 0}')
        for field, metric in (("cpu_percent", "horus_service_cpu_percent"), ("mem_percent", "horus_service_mem_percent")):
            try:
                value = float(process[field])
            except Exception:
                continue
            lines.append(f'{metric}{{service="{name}"}} {value}')

    lines.extend(
        [
            "# HELP horus_ros_topic_count Number of visible local ROS 2 topics.",
            "# TYPE horus_ros_topic_count gauge",
            f"horus_ros_topic_count {len(snapshot.get('topics', []))}",
            "# HELP horus_service_state Service state by title and status.",
            "# TYPE horus_service_state gauge",
        ]
    )
    for service in services:
        state_value = 1 if service.level == "ok" else 0
        lines.append(
            f'horus_service_state{{title="{metric_name(service.title)}",status="{metric_name(service.status)}"}} {state_value}'
        )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    root: Path
    env: Path

    def do_GET(self):  # noqa: N802
        if self.path in {"/", "/metrics"}:
            payload = render_prometheus(self.root, self.env).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/status.json":
            state = extract_state(self.root, self.env, {}, True)
            payload = json.dumps(status_snapshot(state), indent=2, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args):  # noqa: A003
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HORUS Connector metrics exporter")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9418)
    return parser.parse_args()


def main():
    args = parse_args()
    Handler.root = Path(args.root).resolve()
    Handler.env = Path(args.env).resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HORUS metrics listening on http://{args.host}:{args.port}/metrics", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
