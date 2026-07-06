#!/usr/bin/env python3
"""Sample process resource usage for benchmark runs."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
import subprocess
import time


def parse_pid_label(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected LABEL=PID_FILE")
    label, path = raw.split("=", 1)
    label = label.strip()
    if not label:
        raise argparse.ArgumentTypeError("empty label")
    return label, Path(path)


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip().splitlines()[0]
        return int(raw)
    except (OSError, IndexError, ValueError):
        return None


def sample_pid(pid: int) -> dict[str, str] | None:
    if not Path(f"/proc/{pid}").exists():
        return None
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid=", "-o", "pcpu=", "-o", "pmem=", "-o", "rss=", "-o", "etime=", "-o", "comm="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    line = proc.stdout.strip()
    if not line:
        return None
    parts = line.split(None, 5)
    if len(parts) < 6:
        return None
    return {
        "pid": parts[0],
        "cpu_percent": parts[1],
        "mem_percent": parts[2],
        "rss_kb": parts[3],
        "etime": parts[4],
        "command": parts[5],
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--pid-label", action="append", type=parse_pid_label, default=[])
    return parser.parse_args()


def main():
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    fields = (
        "timestamp_ns",
        "monotonic_sec",
        "label",
        "pid",
        "cpu_percent",
        "mem_percent",
        "rss_kb",
        "etime",
        "command",
    )
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        while time.monotonic() < deadline:
            now_ns = time.time_ns()
            now_mono = time.monotonic()
            for label, pid_path in args.pid_label:
                pid = read_pid(pid_path)
                if pid is None:
                    continue
                row = sample_pid(pid)
                if row is None:
                    continue
                row.update({"timestamp_ns": now_ns, "monotonic_sec": f"{now_mono:.6f}", "label": label})
                writer.writerow(row)
            handle.flush()
            time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    main()
