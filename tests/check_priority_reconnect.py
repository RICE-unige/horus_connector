#!/usr/bin/env python3
"""Require all native priority links to survive two operator bridge restarts."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import json5
from link_profile import configure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    processes, logs, receipts = [], [], []

    def start(name, config, domain, mode):
        log = (args.output / f"{name}.log").open("w")
        logs.append(log)
        env = dict(os.environ, ROS_DOMAIN_ID=str(domain))
        env.pop("CYCLONEDDS_URI", None)
        command = [str(args.bridge_binary.resolve()), "-c", str(config), "--no-multicast-scouting"]
        if mode == "router":
            command.extend(["-l", f"tcp/127.0.0.1:{port}"])
        process = subprocess.Popen([*command, mode], env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    source_config, receiver_config = args.output / "source.json", args.output / "receiver.json"
    source_config.write_text(json.dumps(configure(json5.loads((ROOT / "config/zenoh_robot.json5").read_text()),
                                                target="127.0.0.1", port=port)))
    receiver_config.write_text(json.dumps(configure(json5.loads((ROOT / "config/zenoh_machine.json5").read_text()))))
    try:
        receiver = start("receiver-0", receiver_config, 196, "router")
        source = start("source", source_config, 197, "peer")
        for iteration in range(3):
            deadline = time.monotonic() + 15
            links = []
            while time.monotonic() < deadline:
                assert source.poll() is None and receiver.poll() is None, "bridge exited"
                links = [line for line in subprocess.check_output(["ss", "-Htnp"], text=True).splitlines()
                         if f"pid={source.pid}," in line and "ESTAB" in line]
                if len(links) == 3:
                    break
                time.sleep(.1)
            receipts.append(dict(iteration=iteration, links=links))
            assert len(links) == 3, receipts
            if iteration < 2:
                receiver.terminate()
                receiver.wait(timeout=10)
                time.sleep(1)
                receiver = start(f"receiver-{iteration+1}", receiver_config, 196, "router")
        (args.output / "result.json").write_text(json.dumps(dict(passed=True, checks=receipts), indent=2))
        print("Native priority links recovered after both restarts")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
