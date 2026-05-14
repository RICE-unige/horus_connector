#!/usr/bin/env python3
"""Estimate wall-clock offset between benchmark sender and receiver."""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import time


def run_server(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        print(f"clock offset probe listening on {host}:{port}", flush=True)
        while True:
            conn, _addr = server.accept()
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        _line, buf = buf.split(b"\n", 1)
                        payload = json.dumps({"time_ns": time.time_ns()}, separators=(",", ":")).encode()
                        conn.sendall(payload + b"\n")


def run_client(host: str, port: int, samples: int) -> None:
    rows: list[dict[str, float]] = []
    with socket.create_connection((host, port), timeout=5.0) as sock:
        sock.settimeout(5.0)
        stream = sock.makefile("rwb", buffering=0)
        for _ in range(samples):
            t0 = time.time_ns()
            stream.write(b"t\n")
            line = stream.readline()
            t1 = time.time_ns()
            if not line:
                break
            remote_ns = int(json.loads(line.decode())["time_ns"])
            midpoint_ns = (t0 + t1) / 2.0
            rows.append(
                {
                    "rtt_ms": (t1 - t0) / 1_000_000.0,
                    "offset_ms": (remote_ns - midpoint_ns) / 1_000_000.0,
                }
            )
            time.sleep(0.02)
    if not rows:
        raise RuntimeError("no clock samples collected")
    best = min(rows, key=lambda row: row["rtt_ms"])
    ordered_offsets = sorted(row["offset_ms"] for row in rows)
    payload = {
        "host": host,
        "port": port,
        "samples": len(rows),
        "offset_ms": best["offset_ms"],
        "best_rtt_ms": best["rtt_ms"],
        "median_offset_ms": statistics.median(ordered_offsets),
        "median_rtt_ms": statistics.median(row["rtt_ms"] for row in rows),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--samples", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.server:
        run_server(args.host, args.port)
    else:
        run_client(args.host, args.port, args.samples)


if __name__ == "__main__":
    main()
