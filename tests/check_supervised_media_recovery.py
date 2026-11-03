#!/usr/bin/env python3
"""Bound native resource lifetime across eight faults using the actual supervisor."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--url", default="")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = str(ROOT / "tests/fixtures/webrtc_bundled_testsrc.json")
    profile = str(ROOT / "tests/fixtures/webrtc_cpu_test.env")
    if args.worker:
        sys.path.insert(0, str(ROOT / "scripts"))
        from gst_webrtc_h264_machine import GLib, Gst, H264MachineReceiver, parse_args
        with patch.object(sys, "argv", ["receiver", "--streams-config", fixture, "--signaling-url", args.url,
             "--video-output", "display", "--video-sink", "fakesink", "--profile", profile]):
            receiver = H264MachineReceiver(parse_args())
        def fault(*_):
            GLib.idle_add(lambda: receiver._on_bus_message(receiver.bus, Gst.Message.new_eos(receiver.pipeline)))
        signal.signal(signal.SIGUSR1, fault)
        receiver.start()
        raise SystemExit(getattr(receiver, "codec_exit_code", 0))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        url = f"ws://127.0.0.1:{probe.getsockname()[1]}"
        port = probe.getsockname()[1]
    processes, logs, evidence = [], [], []
    environment = dict(os.environ, HORUS_MEDIA_SUPERVISED="1", WEBRTC_MAX_PIPELINE_REBUILDS="4",
                       HORUS_MEDIA_STATUS_FILE=str(args.output / "receiver.media.json"))
    environment.pop("HORUS_WEBRTC_CLIENT_TOKEN", None)
    def start(name, command, env):
        log = (args.output / f"{name}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        processes.append(child)
        return child
    def health():
        try:
            media = json.loads((args.output / "receiver.media.json").read_text())
            state = json.loads((args.output / "receiver.service.json").read_text())
            if media.get("pid") != state.get("child_pid") or media.get("state") != "running":
                return None
            if not 0 <= time.time() - media.get("updated_wall_sec", 0) <= 3:
                return None
            if not all(media.get("tracks", {}).get(track, {}).get("decoded_frames", 0) >= 30 and
                       media["tracks"][track].get("frame_age_sec", 999) < 3 for track in ("front", "rear")):
                return None
            pid = state["child_pid"]
            return {"pid": pid, "generation": media["generation"],
                    "frames": {k: v["decoded_frames"] for k, v in media["tracks"].items()},
                    "fd": len(list(Path(f"/proc/{pid}/fd").iterdir())),
                    "threads": len(list(Path(f"/proc/{pid}/task").iterdir()))}
        except (OSError, ValueError, KeyError, TypeError):
            return None
    def wait_for(check, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = health()
            if state and check(state):
                return state
            assert all(child.poll() is None for child in processes), "Owned test service exited"
            time.sleep(.25)
        raise AssertionError("Supervised receiver did not recover; inspect retained logs")
    try:
        start("relay", [sys.executable, "scripts/webrtc_signal_relay.py", "--host", "127.0.0.1",
                        "--port", str(port), "--allow-unauthenticated"], environment)
        sender_env = dict(environment)
        sender_env.pop("HORUS_MEDIA_STATUS_FILE")
        sender_env.pop("HORUS_MEDIA_SUPERVISED")
        start("sender", [sys.executable, "scripts/gst_webrtc_h264_robot.py", "--streams-config", fixture,
                         "--signaling-url", url, "--profile", profile, "--adaptive-bitrate", "0"], sender_env)
        start("supervisor", [sys.executable, "scripts/service_runner.py", "--log", str(args.output / "receiver.log"),
            "--state", str(args.output / "receiver.service.json"), "--retry-startup-errors", "--restart-on-clean-exit",
            "--max-restarts", "5", "--", sys.executable, str(Path(__file__).resolve()),
            "--worker", "--url", url, "--output", str(args.output.resolve())], environment)
        previous = wait_for(lambda _: True)
        evidence.append(previous)
        for _ in range(8):
            before, started = previous, time.monotonic()
            os.kill(before["pid"], signal.SIGUSR1)
            previous = wait_for(lambda state: (state["pid"], state["generation"]) != (before["pid"], before["generation"]) and
                                (state["pid"] != before["pid"] or min(state["frames"].values()) >= min(before["frames"].values()) + 30))
            evidence.append({**previous, "recovery_sec": time.monotonic() - started})
        identities = list(dict.fromkeys(row["pid"] for row in evidence))
        assert len(identities) >= 3, evidence
        starts = [next(row for row in evidence if row["pid"] == pid) for pid in identities]
        assert max(row["fd"] for row in starts) <= starts[0]["fd"] + 10, starts
        assert max(row["threads"] for row in starts) <= starts[0]["threads"] + 4, starts
        assert all(row["fd"] < starts[0]["fd"] + 64 for row in evidence), evidence
        print("SUPERVISED_MEDIA_RECOVERY_ACCEPTANCE " + json.dumps(evidence))
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
