#!/usr/bin/env python3
"""Local fault acceptance: receiver EOS/stall, sender replacement and relay restart."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import weakref
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gst_webrtc_h264_machine import GLib, Gst, H264MachineReceiver, parse_args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stall-only", action="store_true", help="Exercise the no-error decoder stall watchdog only")
    parser.add_argument("--relay-only", action="store_true", help="Exercise five signaling relay restarts")
    parser.add_argument("--track-only", action="store_true", help="Repair one decoder without resetting its healthy sibling")
    args = parser.parse_args()
    if sum((args.stall_only, args.relay_only, args.track_only)) > 1:
        parser.error("choose one focused fault mode")
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    processes, old_peers = [], []
    fixture = str(ROOT / "tests/fixtures/webrtc_bundled_testsrc.json")
    env = dict(os.environ, HORUS_WEBRTC_CLIENT_ID="robot:bundled-test", HORUS_WEBRTC_CLIENT_TOKEN="test-robot-token")
    def start(name):
        command = [sys.executable, str(ROOT / "scripts/webrtc_signal_relay.py"), "--host", "127.0.0.1", "--port", str(port),
                   "--credentials-file", str(ROOT / "tests/fixtures/webrtc_credentials_test.json")] if name == "relay" else [
                   sys.executable, str(ROOT / "scripts/gst_webrtc_h264_robot.py"), "--streams-config", fixture,
                   "--signaling-url", url, "--duration", "0", "--adaptive-bitrate", "0",
                   "--profile", str(ROOT / "tests/fixtures/webrtc_cpu_test.env")]
        with (args.output / f"{name}-{len(processes)}.log").open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def stop(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    def resources():
        descriptors = {}
        for path in Path("/proc/self/fd").iterdir():
            try:
                descriptors[path.name] = os.readlink(path)
            except OSError:
                continue
        return {"fd": len(descriptors), "fd_targets": descriptors,
                "retained_old_peers": sum(peer() is not None for peer in old_peers),
                "threads": len(list(Path("/proc/self/task").iterdir()))}
    receiver = None
    evidence, faults = [], (["track_decoder_error"] if args.track_only else ["decoder_stall"] if args.stall_only else ["relay_restart"] * 5 if args.relay_only else
                           ["receiver_eos"] * 5 + ["sender_restart", "relay_restart", "receiver_eos", "decoder_stall"])
    expected_faults = len(faults)
    baseline, fault_started, current, completed = {}, 0, None, False
    generation_before, observed_generation = 0, None
    try:
        relay = start("relay")
        time.sleep(.5)
        robot = start("robot")
        os.environ["HORUS_WEBRTC_CLIENT_ID"] = "machine:bundled-test"
        os.environ["HORUS_WEBRTC_CLIENT_TOKEN"] = "test-machine-token"
        with patch.object(sys, "argv", ["receiver", "--streams-config", fixture, "--signaling-url", url,
             "--video-output", "display", "--video-sink", "fakesink", "--duration", "240",
             "--latency-json", str(args.output / "receiver.json"), "--profile", str(ROOT / "tests/fixtures/webrtc_cpu_test.env")]):
            receiver = H264MachineReceiver(parse_args())
        def step():
            nonlocal relay, robot, baseline, fault_started, current, completed, generation_before, observed_generation
            now = time.monotonic()
            frames = dict(receiver.video_frames_by_stream)
            if current and observed_generation is None and (receiver.generation > generation_before or current == "track_decoder_error"):
                observed_generation = receiver.generation
                baseline = frames
            healthy = (not current or observed_generation is not None) and all(
                frames.get(track, 0) >= baseline.get(track, 0) + 30 for track in ("front", "rear"))
            if current and healthy:
                evidence.append({"fault": current, "recovery_sec": now - fault_started, "frames": frames,
                                 "generation_before": generation_before, "generation_after": receiver.generation, **resources()})
                current = None
            if current and now - fault_started > 55:
                receiver.stop()
                return False
            if not current and healthy:
                if not faults:
                    completed = True
                    receiver.stop()
                    return False
                if not evidence:
                    evidence.append({"fault": "initial", "frames": frames, **resources()})
                current = faults.pop(0)
                old_peers.append(weakref.ref(receiver.webrtc))
                baseline, fault_started = frames, now
                generation_before, observed_generation = receiver.generation, None
                if current == "receiver_eos":
                    receiver._on_bus_message(receiver.bus, Gst.Message.new_eos(receiver.pipeline))
                elif current == "sender_restart":
                    stop(robot)
                    robot = start("robot")
                elif current == "decoder_stall":
                    iterator = receiver.pipeline.iterate_elements()
                    while True:
                        result, element = iterator.next()
                        if result == Gst.IteratorResult.DONE:
                            break
                        if result != Gst.IteratorResult.OK:
                            raise RuntimeError("Pipeline changed during stall injection")
                        if isinstance(element, Gst.Bin) and element.get_static_pad("sink") is not None:
                            element.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                lambda *_: Gst.PadProbeReturn.DROP)
                elif current == "track_decoder_error":
                    media = receiver.decoder_bins["front"]
                    error = GLib.Error.new_literal(Gst.StreamError.quark(), "Injected decoder error", Gst.StreamError.FAILED)
                    receiver._on_bus_message(receiver.bus, Gst.Message.new_error(media, error, "acceptance fault"))
                else:
                    stop(relay)
                    relay = start("relay")
            return True
        GLib.timeout_add(250, step)
        receiver.start()
    finally:
        if receiver is not None:
            receiver.stop()
        for process in reversed(processes):
            stop(process)
        (args.output / "evidence.json").write_text(json.dumps({"completed": completed, "faults": evidence}, indent=2))
    assert completed and len(evidence) == expected_faults + 1, evidence
    assert all(item["recovery_sec"] < 10 for item in evidence
               if item["fault"] in {"relay_restart", "sender_restart"}), evidence
    assert evidence[-1]["fd"] <= evidence[0]["fd"] + 10, evidence
    assert evidence[-1]["threads"] <= evidence[0]["threads"] + 4, evidence
    if args.track_only:
        assert evidence[-1]["generation_before"] == evidence[-1]["generation_after"], evidence
    print("MEDIA_RECOVERY_ACCEPTANCE " + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
