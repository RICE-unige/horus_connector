#!/usr/bin/env python3
"""Check two local operator rooms and reconnect one without interrupting the other."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tracks", type=int, choices=(1, 2, 4, 8), default=2)
    parser.add_argument("--operators", type=int, choices=(2, 3), default=2)
    parser.add_argument("--soak-seconds", type=float, default=0)
    args = parser.parse_args()
    if args.soak_seconds < 0:
        parser.error("soak duration must be nonnegative")
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    fixture_path = args.output / "streams.json"
    configuration = json.loads((ROOT / "tests/fixtures/webrtc_bundled_testsrc.json").read_text())
    prototype = configuration["streams"][0]
    tracks = [f"camera_{index}" for index in range(args.tracks)]
    configuration["streams"] = [{**prototype, "id": track, "output_topic": f"/acceptance/{track}/image_raw"}
                                for track in tracks]
    fixture_path.write_text(json.dumps(configuration, indent=2))
    fixture = str(fixture_path.resolve())
    rooms = [f"operator-{index}" for index in range(args.operators)]
    processes, logs = [], []
    evidence = {}

    def start(role, room):
        env = dict(os.environ, HORUS_WEBRTC_SESSION_ROOM=room, WEBRTC_CONTROL_ENABLED="0",
                   HORUS_WEBRTC_CLIENT_ID=f"{role}:{room}",
                   HORUS_MEDIA_STATUS_FILE=str(args.output / f"{role}-{room}.media.json"))
        if role == "relay":
            # Only the disposable loopback relay uses this flag; deployment policy is unchanged.
            command = [sys.executable, str(ROOT / "scripts/webrtc_signal_relay.py"),
                       "--host", "127.0.0.1", "--port", str(port), "--allow-unauthenticated"]
        else:
            command = [sys.executable, str(ROOT / f"scripts/gst_webrtc_h264_{role}.py"),
                       "--streams-config", fixture, "--signaling-url", url, "--duration", "0",
                       "--profile", str(ROOT / "tests/fixtures/webrtc_cpu_test.env")]
            if role == "machine":
                command.extend(["--video-output", "display", "--video-sink", "fakesink"])
            else:
                command.extend(["--adaptive-bitrate", "0"])
        log = (args.output / f"{role}-{room}-{len(processes)}.log").open("w")
        logs.append(log)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def state(role, room):
        try:
            return json.loads((args.output / f"{role}-{room}.media.json").read_text())
        except (OSError, ValueError):
            return {}

    def flowing(room, minimum=30):
        health = state("machine", room)
        if health.get("state") != "running" or time.time() - health.get("updated_wall_sec", 0) > 3:
            return False
        return all(health.get("tracks", {}).get(track, {}).get("decoded_frames", 0) >= minimum
                   and health["tracks"][track].get("frame_age_sec", 999) < 3 for track in tracks)

    def wait_until(check, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(.25)
        raise AssertionError("Operator media did not meet the freshness/count deadline")

    try:
        start("relay", "test")
        time.sleep(.5)
        machine_a = None
        for room in rooms:
            start("robot", room)
            machine = start("machine", room)
            if machine_a is None:
                machine_a = machine
        wait_until(lambda: all(flowing(room) for room in rooms), 45)
        initial_b = state("machine", rooms[1])
        sender_b_generation = state("robot", rooms[1])["generation"]
        evidence["initial"] = {room: state("machine", room) for room in rooms}
        stop(machine_a)
        wait_until(lambda: flowing(rooms[1]) and all(
            state("machine", rooms[1])["tracks"][track]["decoded_frames"] >=
            initial_b["tracks"][track]["decoded_frames"] + 30 for track in tracks), 10)
        start("machine", rooms[0])
        wait_until(lambda: flowing(rooms[0]), 45)
        final_b = state("machine", rooms[1])
        assert final_b["generation"] == initial_b["generation"], final_b
        assert state("robot", rooms[1])["generation"] == sender_b_generation
        assert flowing(rooms[1])
        evidence["after_operator_a_rejoin"] = {room: state("machine", room) for room in rooms}
        soak_started = time.monotonic()
        evidence["soak"] = []
        while time.monotonic() - soak_started < args.soak_seconds:
            snapshot = {room: state("machine", room) for room in rooms}
            assert all(flowing(room) for room in rooms), snapshot
            evidence["soak"].append({"elapsed_sec": time.monotonic() - soak_started, "receivers": snapshot})
            time.sleep(min(5, args.soak_seconds))
        evidence["other_room_generation_unchanged"] = True
        evidence["passed"] = True
    finally:
        for process in reversed(processes):
            stop(process)
        for log in logs:
            log.close()
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print("MULTI_OPERATOR_MEDIA_ACCEPTANCE " + json.dumps(evidence))


if __name__ == "__main__":
    main()
