#!/usr/bin/env python3
"""Exercise the real launcher with bundled and isolated two-camera direct sessions."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def wait_for_frames(run_dir, services, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = []
        for service in services:
            try:
                states.append(json.loads((run_dir / f"{service}.media.json").read_text()))
            except (OSError, ValueError):
                break
        if len(states) == len(services) and all(
            state.get("tracks") and all(track.get("decoded_frames", 0) >= 30 and track.get("state") == "flowing"
                                       for track in state["tracks"].values()) for state in states
        ):
            return states
        time.sleep(.2)
    raise AssertionError(f"Camera frames did not flow in {run_dir}; inspect service logs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = {}
    for mode in ("bundled", "isolated"):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = args.output / f"{mode}.json"
        cameras = json.loads((ROOT / "tests/fixtures/webrtc_bundled_testsrc.json").read_text())
        if mode == "isolated":
            cameras["session"]["mode"] = "isolated"
            for stream in cameras["streams"]:
                stream["room"] = f"isolated-{stream['id']}"
        config.write_text(json.dumps(cameras))
        endpoints = {}
        for role in ("machine", "robot"):
            run = args.output / f"{mode}-{role}"
            settings = args.output / f"{mode}-{role}.env"
            settings.write_text("\n".join([
                "HORUS_ZENOH_ENABLED=0", "HORUS_DDS_PREFLIGHT=0", "HORUS_TOPOLOGY=direct",
                "HORUS_MACHINE_IP=127.0.0.1", "HORUS_WEBRTC_SIGNAL_IP=127.0.0.1",
                f"WEBRTC_SIGNAL_PORT={port}", "HORUS_METRICS_PORT=0", "WEBRTC_DURATION=0",
                "WEBRTC_VIDEO_OUTPUT=display", "WEBRTC_VIDEO_SINK=fakesink", "WEBRTC_CONTROL_ENABLED=0",
                "ROS_DOMAIN_ID=197", "ROS_LOCALHOST_ONLY=1", "ROS_DISTRO=jazzy",
                f"HORUS_STREAMS_CONFIG='{config}'", f"HORUS_WEBRTC_SESSION_MODE={mode}",
                "WEBRTC_ADAPTIVE_BITRATE=0", "WEBRTC_ICE_SERVERS=", "HORUS_WEBRTC_RELAY_ALLOW_UNAUTHENTICATED=1",
                "WEBRTC_ENCODER_PREFERENCE=software", "WEBRTC_DECODER_PREFERENCE=software",
                "WEBRTC_VIDEO_WIDTH=320", "WEBRTC_VIDEO_HEIGHT=180", "WEBRTC_VIDEO_FPS=15",
            ]) + "\n")
            env = dict(os.environ, HORUS_RUN_DIR=str(run), HORUS_ENV=str(settings), HORUS_WEBRTC_PYTHON=sys.executable)
            endpoints[role] = (run, env)
        def command(role, *words):
            run, env = endpoints[role]
            result = subprocess.run([str(ROOT / "horus"), *words], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=120)
            run.mkdir(parents=True, exist_ok=True)
            with (run / "acceptance-launcher.log").open("a") as log:
                log.write(result.stdout + result.stderr)
            assert result.returncode == 0, result.stdout + result.stderr
        try:
            command("machine", "launch", "machine", "--no-monitor")
            command("robot", "launch", "robot", "--no-monitor")
            run, _env = endpoints["machine"]
            services = ["webrtc"] if mode == "bundled" else ["webrtc-front", "webrtc-rear"]
            states = wait_for_frames(run, services)
            pids = {name: (run / f"{name}.pid").read_text() for name in services}
            profile_mtime = (run / "webrtc_profile.env").stat().st_mtime_ns
            command("machine", "launch", "machine", "--no-monitor")
            assert pids == {name: (run / f"{name}.pid").read_text() for name in services}
            assert profile_mtime == (run / "webrtc_profile.env").stat().st_mtime_ns
            evidence[mode] = {"states": states, "relaunch_preserved_pids": True, "profile_cache_reused": True}
        finally:
            for role in ("robot", "machine"):
                command(role, "stop")
                run, _env = endpoints[role]
                assert not list(run.glob("*.pid")), list(run.glob("*.pid"))
    (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2))
    print("CONNECTOR_LAUNCH_ACCEPTANCE " + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
