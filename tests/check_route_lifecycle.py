#!/usr/bin/env python3
"""Detect ghost DDS readers after robot publisher and operator subscriber churn."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def exercise(output, rounds):
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    contexts, nodes, executors = [], [], []
    for domain, name in ((182, "churn_source"), (183, "churn_receiver")):
        context = Context()
        rclpy.init(context=context, domain_id=domain)
        contexts.append(context)
        nodes.append(rclpy.create_node(name, context=context))
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(nodes[-1])
        executors.append(executor)
    source, receiver = nodes

    def spin(seconds):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            for executor in executors:
                executor.spin_once(timeout_sec=.001)

    result = []
    try:
        for generation in range(rounds):
            received = []
            publisher = source.create_publisher(String, "/acceptance/churn", 32)
            subscriber = receiver.create_subscription(String, "/acceptance/churn",
                                                       lambda msg: received.append(msg.data), 128)
            spin(2.5)
            for index in range(30):
                publisher.publish(String(data=f"{generation}:{index}"))
                spin(.025)
            spin(1)
            counts = Counter(received)
            expected = {f"{generation}:{i}" for i in range(30)}
            result.append(dict(generation=generation, messages=len(received), unique=len(counts),
                duplicates=sum(max(0, n-1) for n in counts.values()), missing=sorted(expected-counts.keys())))
            receiver.destroy_subscription(subscriber)
            source.destroy_publisher(publisher)
            spin(2)
        output.write_text(json.dumps(result, indent=2)+"\n")
        assert all(not r["duplicates"] and not r["missing"] for r in result), result
    finally:
        for executor in executors:
            executor.shutdown()
        for node, context in zip(nodes, contexts):
            node.destroy_node()
            rclpy.shutdown(context=context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    processes, logs = [], []
    base = dict(os.environ, RMW_IMPLEMENTATION="rmw_cyclonedds_cpp", ROS_LOCALHOST_ONLY="1",
        ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST", CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces>'
        '<NetworkInterface name="lo" multicast="true"/></Interfaces></General></Domain></CycloneDDS>')
    try:
        for role, domain, option in (("machine", 183, "-l"), ("robot", 182, "-e")):
            config = args.output / (role+".json")
            config.write_text(json.dumps(dict(plugins=dict(ros2dds=dict(ros_localhost_only=True)),
                scouting=dict(multicast=dict(enabled=False), gossip=dict(enabled=False)))))
            log = (args.output / (role+".log")).open("x")
            logs.append(log)
            processes.append(subprocess.Popen([str(args.bridge.resolve()), "-c", str(config.resolve()),
                option, endpoint, "--no-multicast-scouting", "router" if role == "machine" else "peer"],
                env=dict(base, ROS_DOMAIN_ID=str(domain)), stdout=log, stderr=subprocess.STDOUT))
            time.sleep(1)
        os.environ.update(base)
        exercise(args.output / "result.json", args.rounds)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
