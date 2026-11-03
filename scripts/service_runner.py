#!/usr/bin/env python3
"""Supervise only connector-owned children, with bounded retries and logs."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from media_health import write_status


def process_start_time(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def stop_owned_group(state_path, owner_pid, owner_start):
    try:
        state = json.loads(Path(state_path).read_text())
        if not isinstance(state, dict):
            return False
        child_pid = state.get("child_pid")
        child_start = state.get("child_start")
        if (state.get("supervisor_pid") != owner_pid or not owner_start
                or process_start_time(owner_pid) != owner_start or not child_start
                or not isinstance(child_pid, int) or child_pid <= 1
                or process_start_time(child_pid) != child_start or os.getpgid(child_pid) != child_pid):
            return False
        os.killpg(child_pid, signal.SIGKILL)
        return True
    except (OSError, ValueError, TypeError):
        return False


class RotatingOutput:
    def __init__(self, path, max_bytes=10 * 1024 * 1024, backups=3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("ab", buffering=0)

    def write(self, data):
        if self.stream.tell() + len(data) > self.max_bytes:
            self.stream.close()
            for index in range(self.backups, 0, -1):
                source = self.path if index == 1 else self.path.with_name(f"{self.path.name}.{index - 1}")
                target = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    source.replace(target)
            self.stream = self.path.open("wb", buffering=0)
        self.stream.write(data)

    def close(self):
        self.stream.close()


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.stopping = False
        self.child = None
        self.child_start = None
        self.restarts = 0
        self.recent_failures = []

    def request_stop(self, *_args):
        self.stopping = True
        if self.child is not None:
            self.signal_group(signal.SIGTERM)

    def signal_group(self, sig):
        if self.child is not None:
            try:
                os.killpg(self.child.pid, sig)
            except ProcessLookupError:
                pass

    def finish_group(self):
        self.signal_group(signal.SIGTERM)
        deadline = time.monotonic() + min(self.args.stop_grace, 1.0)
        while time.monotonic() < deadline:
            try:
                os.killpg(self.child.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.signal_group(signal.SIGKILL)

    def status(self, phase, **fields):
        write_status(self.args.state, {
            "phase": phase, "supervisor_pid": os.getpid(),
            "child_pid": self.child.pid if self.child else None,
            "child_start": self.child_start,
            "restarts": self.restarts, **fields,
        })

    def run(self):
        output = RotatingOutput(self.args.log, self.args.log_max_bytes)
        try:
            while not self.stopping:
                env = dict(os.environ, PYTHONUNBUFFERED="1")
                try:
                    self.child = subprocess.Popen(self.args.command, env=env, stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                  start_new_session=True)
                    self.child_start = process_start_time(self.child.pid)
                except OSError as exc:
                    self.status("failed", reason=str(exc))
                    return 1
                started = time.monotonic()
                stopping_at = None
                last_status = started
                self.status("starting")
                with selectors.DefaultSelector() as selector:
                    selector.register(self.child.stdout, selectors.EVENT_READ)
                    while True:
                        for key, _mask in selector.select(0.1):
                            data = os.read(key.fd, 65536)
                            if data:
                                output.write(data)
                            else:
                                selector.unregister(key.fileobj)
                        if self.stopping:
                            if stopping_at is None:
                                stopping_at = time.monotonic()
                                self.signal_group(signal.SIGTERM)
                                self.status("stopping")
                            elif time.monotonic() - stopping_at >= self.args.stop_grace:
                                self.signal_group(signal.SIGKILL)
                        elif time.monotonic() - started >= self.args.startup_grace and time.monotonic() - last_status >= 1:
                            self.status("running")
                            last_status = time.monotonic()
                        code = self.child.poll()
                        if code is not None:
                            # A child may leave subprocesses holding stdout; never wait on those forever.
                            self.signal_group(signal.SIGTERM)
                            break
                self.child.stdout.close()
                code = self.child.wait()
                self.finish_group()
                if self.stopping or code == 0 and not self.args.restart_on_clean_exit:
                    self.status("stopped", exit_code=code)
                    return 0 if self.stopping else code
                if code in self.args.terminal_exit_codes or (time.monotonic() - started < self.args.startup_grace and not self.args.retry_startup_errors):
                    self.status("failed", exit_code=code)
                    return code
                now = time.monotonic()
                self.recent_failures = [stamp for stamp in self.recent_failures if now - stamp < self.args.restart_window]
                self.recent_failures.append(now)
                if len(self.recent_failures) > self.args.max_restarts:
                    self.status("failed", exit_code=code)
                    return code
                self.restarts += 1
                delay = min(30, 2 ** (len(self.recent_failures) - 1))
                self.status("recovering", exit_code=code, retry_in_sec=delay)
                deadline = now + delay
                while not self.stopping and time.monotonic() < deadline:
                    time.sleep(0.1)
            self.status("stopped")
            return 0
        finally:
            if self.child is not None and self.child.poll() is None:
                self.signal_group(signal.SIGKILL)
                self.child.wait(timeout=5)
            output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log")
    parser.add_argument("--state", required=True)
    parser.add_argument("--stop-owned-group", action="store_true")
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--owner-start")
    parser.add_argument("--startup-grace", type=float, default=2)
    parser.add_argument("--stop-grace", type=float, default=8)
    parser.add_argument("--max-restarts", type=int, default=5)
    parser.add_argument("--restart-window", type=float, default=300)
    parser.add_argument("--restart-on-clean-exit", action="store_true")
    parser.add_argument("--retry-startup-errors", action="store_true")
    parser.add_argument("--terminal-exit-codes", type=lambda value: {int(code) for code in value.split(",") if code}, default={64, 126, 127})
    parser.add_argument("--log-max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.stop_owned_group:
        if args.owner_pid is None or not args.owner_start:
            parser.error("owned group cleanup requires the recorded supervisor identity")
        raise SystemExit(0 if stop_owned_group(args.state, args.owner_pid, args.owner_start) else 1)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.log or not args.command or args.stop_grace <= 0 or args.startup_grace <= 0 or args.max_restarts < 0 or args.restart_window <= 0:
        parser.error("a command and positive grace periods are required")
    supervisor = Supervisor(args)
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
