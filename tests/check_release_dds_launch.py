#!/usr/bin/env python3
"""Prove the CLI forwards live ROS data across two local domains."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from check_zenoh_routes import unused_loopback_port_block


TOPIC = "/horus/release_tf_probe"
DDS = '<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="lo" multicast="true"/></Interfaces><AllowMulticast>true</AllowMulticast><EnableMulticastLoopback>true</EnableMulticastLoopback></General></Domain></CycloneDDS>'


def talker(domain, data):
    import rclpy
    from std_msgs.msg import String

    os.environ["ROS_DOMAIN_ID"] = str(domain)
    rclpy.init()
    node = rclpy.create_node("horus_release_probe_publisher")
    publisher = node.create_publisher(String, TOPIC, 10)
    node.create_timer(.05, lambda: publisher.publish(String(data=data)))
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def check(output, root, bridge):
    output.mkdir(parents=True)
    port = unused_loopback_port_block(3) if bridge else None
    if port is None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    environment = dict(os.environ)
    environment.pop("ROS_DOMAIN_ID", None)
    environment.pop("ROS_LOCALHOST_ONLY", None)
    environment.update(CYCLONEDDS_URI=DDS, RMW_IMPLEMENTATION="rmw_cyclonedds_cpp",
                       ROS_AUTOMATIC_DISCOVERY_RANGE="SYSTEM_DEFAULT",
                       HORUS_WEBRTC_PYTHON=sys.executable)
    if environment["ROS_DISTRO"] == "humble":
        environment.pop("CYCLONEDDS_URI")
        environment["ROS_LOCALHOST_ONLY"] = "1"
    profiles = {}
    for role, domain in (("robot", 201), ("machine", 202)):
        path = output / f"{role}.env"
        path.write_text(f"""HORUS_ROLE={role}
HORUS_TOPOLOGY=direct
HORUS_MACHINE_IP=127.0.0.1
ROS_DISTRO={environment["ROS_DISTRO"]}
ROS_DOMAIN_ID={domain}
HORUS_METRICS_PORT=0
ZENOH_PORT={port}
ZENOH_BIND=127.0.0.1
ZENOH_TRANSPORT=tcp
ZENOH_NAMESPACE=/horus-release-check
HORUS_ZENOH_SESSION_ISOLATION={int(bridge is not None)}
""" + (f"ZENOH_BRIDGE_BINARY={json.dumps(str(bridge))}\n" if bridge else ""))
        profiles[role] = dict(environment, HORUS_ENV=str(path), HORUS_RUN_DIR=str(output / role))
    test_nodes = []

    def cli(role, *args, extra=None, expected=0):
        result = subprocess.run([str(root / "horus"), *args], cwd=root,
                                env={**profiles[role], **(extra or {})}, capture_output=True, text=True, timeout=45)
        with (output / "launcher.log").open("a") as log:
            log.write(result.stdout + result.stderr)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    try:
        wrong = cli("robot", "launch", "robot", "--zenoh-only", "--no-monitor",
                    extra={"ROS_DOMAIN_ID": "204"}, expected=1)
        assert "ROS_DOMAIN_ID conflict" in wrong.stderr
        assert not list((output / "robot").glob("*.pid"))
        for domain, label in ((201, "right-domain"), (204, "wrong-domain")):
            test_nodes.append(subprocess.Popen([sys.executable, __file__, "--talker", str(domain), label],
                                               env=environment, stdout=subprocess.DEVNULL))
        cli("machine", "launch", "machine", "--zenoh-only", "--no-monitor")
        cli("robot", "launch", "robot", "--zenoh-only", "--no-monitor",
            extra={"HORUS_DDS_PROBE_TOPIC": TOPIC})
        settings = output / "machine.env"
        original = settings.read_text()
        settings.write_text(original.replace("ROS_DOMAIN_ID=202", "ROS_DOMAIN_ID=203"))
        try:
            changed = cli("machine", "launch", "machine", "--zenoh-only", "--no-monitor", expected=1)
            assert "Running services use another or unrecorded ROS domain" in changed.stderr
        finally:
            settings.write_text(original)
        cli("machine", "doctor", "machine", extra={"HORUS_DDS_REQUIRE_SAMPLE": "1", "HORUS_DDS_PROBE_TOPIC": TOPIC})
        verdict = json.loads((output / "machine/dds_preflight.json").read_text())
        assert verdict["data_probe"]["received"]
        received = subprocess.run(["ros2", "topic", "echo", TOPIC, "--once"],
                                  env={**environment, "ROS_DOMAIN_ID": "202"}, timeout=6, check=True,
                                  capture_output=True, text=True).stdout
        assert "right-domain" in received and "wrong-domain" not in received
        print("DDS launch: wrong terminal domain rejected; live DDS passed 201 -> 202 only")
    finally:
        try:
            for role in ("robot", "machine"):
                cli(role, "stop")
        finally:
            for proc in test_nodes:
                proc.send_signal(signal.SIGINT)
            for proc in test_nodes:
                proc.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--talker", nargs=2)
    args = parser.parse_args()
    if args.talker:
        talker(int(args.talker[0]), args.talker[1])
    elif args.output:
        check(args.output.resolve(), args.root.resolve(), args.bridge.resolve() if args.bridge else None)
    else:
        parser.error("--output is required")
