#!/usr/bin/env python3
"""Verify HORUS return routes and three static writers across isolated ROS domains."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
REPLIES = ["/horus/registration_ack", "/horus/heartbeat", "/horus/teleop/runtime_state",
           "/horus/robot_description/request", "/horus/multi_operator/sdk_registration_replay_request",
           "/fleet/horus/registration_ack"]


def worker(role, output, bulk=False, duration=0):
    import rclpy
    from rclpy.qos import QoSProfile, DurabilityPolicy
    from std_msgs.msg import String, UInt8MultiArray
    from tf2_msgs.msg import TFMessage
    from geometry_msgs.msg import TransformStamped
    from rclpy.executors import SingleThreadedExecutor
    rclpy.init()
    node = rclpy.create_node(f"connector_{role}_acceptance")
    if role == "hmsh":
        qos = QoSProfile(depth=64, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        publisher = node.create_publisher(UInt8MultiArray, "/acceptance/mesh/indexed", qos)
        deadline = time.monotonic() + 8
        while publisher.get_subscription_count() == 0:
            assert time.monotonic() < deadline, "HMSH receiver was not discovered"
            rclpy.spin_once(node, timeout_sec=.05)
        expected = {}
        vertices = b"".join(struct.pack("<fffBBBB", index*.001, index*.002, index*.003,
                             index % 256, 127, 255, 255) for index in range(1023))
        indices = struct.pack("<1023I", *range(1023))
        for revision in range(1, 9):
            for chunk in range(1, 33):
                header = struct.pack("<IIIiiiQQBBHII", 0x484D5348, 2, chunk, chunk, 0, 0,
                                     7, revision, 0, 1, 0, 1023, 1023)
                payload = header + vertices + indices
                expected[str(chunk)] = hashlib.sha256(payload).hexdigest()
                publisher.publish(UInt8MultiArray(data=payload))
                time.sleep(.005)
        output.write_text(json.dumps(expected))
        started = time.monotonic()
        while time.monotonic() - started < 30:
            rclpy.spin_once(node, timeout_sec=.1)
        node.destroy_node()
        rclpy.shutdown()
        return
    if role == "integrity":
        qos = QoSProfile(depth=64, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        publisher = node.create_publisher(UInt8MultiArray, "/acceptance/mesh_integrity", qos)
        expected = {}
        started = time.monotonic()
        time.sleep(1)
        for index in range(32):
            payload = index.to_bytes(4, "little") + os.urandom(32 * 1024)
            expected[str(index)] = hashlib.sha256(payload).hexdigest()
            publisher.publish(UInt8MultiArray(data=payload))
            time.sleep(.05)
        output.write_text(json.dumps(expected))
        while time.monotonic() - started < 25:
            rclpy.spin_once(node, timeout_sec=.1)
        node.destroy_node()
        rclpy.shutdown()
        return
    if role == "bulk":
        publisher = node.create_publisher(UInt8MultiArray, "/acceptance/mesh", 1)
        payload = UInt8MultiArray(data=os.urandom(512 * 1024))
        started = time.monotonic()
        count = 0
        while time.monotonic() - started < (duration or 25):
            publisher.publish(payload)
            count += 1
            time.sleep(.05)
        output.write_text(json.dumps({"published": count}))
        node.destroy_node()
        rclpy.shutdown()
        return
    static_nodes = []
    dynamic_seen, dynamic_latencies = {}, {}
    seen = {"topics": {}, "static_frames": [], "route_latency_ms": {}, "steady_route_latency_ms": {},
            "source_warmup_sec": 3, "slow_samples": {}, "bulk_integrity": {}, "hmsh_latest": {},
            "hmsh_revisions": {}, "hmsh_late_revisions": []}
    latencies = {}
    steady_latencies = {}
    def receive(msg, topic):
        seen["topics"][topic] = seen["topics"].get(topic, 0) + 1
        source = json.loads(msg.data)
        latency_ms = (time.monotonic_ns() - source["send_ns"]) / 1e6
        latencies.setdefault(topic, []).append(latency_ms)
        if source["source_elapsed_sec"] >= seen["source_warmup_sec"]:
            steady_latencies.setdefault(topic, []).append(latency_ms)
        if latency_ms >= 100:
            seen["slow_samples"].setdefault(topic, []).append({"elapsed_sec": time.monotonic() - started,
                                                               "latency_ms": latency_ms})
    def static(msg):
        for transform in msg.transforms:
            if transform.child_frame_id not in seen["static_frames"]:
                seen["static_frames"].append(transform.child_frame_id)
    def dynamic(msg):
        for transform in msg.transforms:
            key = transform.child_frame_id
            dynamic_seen[key] = dynamic_seen.get(key, 0) + 1
            latency = (node.get_clock().now().nanoseconds -
                       (transform.header.stamp.sec * 1_000_000_000 + transform.header.stamp.nanosec)) / 1e6
            if time.monotonic() - started > 3:
                dynamic_latencies.setdefault(key, []).append(latency)
    publishers = []
    if role == "robot":
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        for index in range(3):
            static_node = rclpy.create_node(f"static_acceptance_{index}")
            pub = static_node.create_publisher(TFMessage, "/tf_static", qos)
            static_nodes.append((static_node, pub, index))
            moving = static_node.create_publisher(TFMessage, "/tf", 10)
            def publish_pose(publisher=moving, child=f"dynamic_robot_{index}"):
                transform = TransformStamped()
                transform.header.frame_id, transform.child_frame_id = "map", child
                transform.transform.rotation.w = 1.
                transform.header.stamp = node.get_clock().now().to_msg()
                publisher.publish(TFMessage(transforms=[transform]))
            static_node.create_timer(.025 if index < 2 else .1, publish_pose)
        for topic in REPLIES:
            node.create_subscription(String, topic, lambda msg, topic=topic: receive(msg, topic), 10)
        for topic in ("/horus/registration", "/fleet/horus/registration", "/robot/odom"):
            publishers.append(node.create_publisher(String, topic, 10))
    else:
        node.create_subscription(TFMessage, "/tf_static", static, QoSProfile(depth=10, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        node.create_subscription(TFMessage, "/tf", dynamic, 100)
        for topic in ("/horus/registration", "/fleet/horus/registration", "/robot/odom"):
            node.create_subscription(String, topic, lambda msg, topic=topic: receive(msg, topic), 10)
        publishers = [node.create_publisher(String, topic, 10) for topic in REPLIES]
    started = time.monotonic()
    static_sent = False
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    for static_node, _pub, _index in static_nodes:
        executor.add_node(static_node)
    timer = node.create_timer(.1, lambda: [pub.publish(String(data=json.dumps(
        {"send_ns": time.monotonic_ns(), "source_elapsed_sec": time.monotonic() - started}))) for pub in publishers])
    bulk_timer = None
    if bulk and role == "machine":
        node.create_subscription(UInt8MultiArray, "/acceptance/mesh",
                                 lambda _: seen["topics"].__setitem__("/acceptance/mesh", seen["topics"].get("/acceptance/mesh", 0) + 1), 1)
        def integrity(message):
            payload = bytes(message.data)
            seen["bulk_integrity"][str(int.from_bytes(payload[:4], "little"))] = hashlib.sha256(payload).hexdigest()
        node.create_subscription(UInt8MultiArray, "/acceptance/mesh_integrity", integrity,
                                 QoSProfile(depth=64, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        def hmsh(message):
            payload = bytes(message.data)
            chunk = str(struct.unpack_from("<I", payload, 8)[0])
            epoch, revision = struct.unpack_from("<QQ", payload, 24)
            version = [epoch, revision]
            previous = seen["hmsh_revisions"].get(chunk)
            # HMSH consumers reject older cached revisions, regardless of
            # arrival order. Still report them rather than hiding reordering.
            if previous is not None and version < previous:
                seen["hmsh_late_revisions"].append(dict(chunk=chunk, received=version, retained=previous))
                return
            seen["hmsh_revisions"][chunk] = version
            seen["hmsh_latest"][chunk] = hashlib.sha256(payload).hexdigest()
        node.create_subscription(UInt8MultiArray, "/acceptance/mesh/indexed", hmsh,
                                 QoSProfile(depth=64, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    while time.monotonic() - started < (duration or (21 if role == "robot" else 15)):
        if static_nodes and not static_sent and time.monotonic() - started > 1:
            for _static_node, pub, index in static_nodes:
                transform = TransformStamped()
                transform.header.frame_id = "map"
                transform.child_frame_id = f"static_robot_{index}"
                transform.transform.rotation.w = 1.
                pub.publish(TFMessage(transforms=[transform]))
            static_sent = True
        executor.spin_once(timeout_sec=.01)
    for key, samples in (("route_latency_ms", latencies), ("steady_route_latency_ms", steady_latencies)):
        for topic, values in samples.items():
            ordered = sorted(values)
            seen[key][topic] = {"samples": len(values), "p50": ordered[len(ordered) // 2],
                               "p95": ordered[int((len(ordered) - 1) * .95)], "max": ordered[-1]}
    seen["dynamic_frames"] = dynamic_seen
    seen["dynamic_latency_ms"] = {
        key: {"samples": len(values), "p50": sorted(values)[len(values)//2],
              "p95": sorted(values)[int((len(values)-1)*.95)], "max": max(values)}
        for key, values in dynamic_latencies.items()}
    output.write_text(json.dumps(seen, indent=2))
    node.destroy_timer(timer)
    if bulk_timer is not None:
        node.destroy_timer(bulk_timer)
    executor.shutdown()
    node.destroy_node()
    for static_node, _pub, _index in static_nodes:
        static_node.destroy_node()
    rclpy.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bridge-binary", type=Path, default=ROOT / "zenoh-bridge-ros2dds",
                        help="Standalone bridge to verify, including a saved previous release")
    parser.add_argument("--worker", choices=("robot", "machine", "bulk", "integrity", "hmsh"))
    parser.add_argument("--worker-duration", type=float, default=0)
    parser.add_argument("--bulk", action="store_true", help="Also publish 512 KiB mesh messages at 20 Hz on localhost")
    parser.add_argument("--hub", action="store_true", help="Route both ROS endpoints through an isolated local cloud router")
    parser.add_argument("--custom-routes", action="store_true", help="Also compile and exercise capability-derived command routes")
    parser.add_argument("--priority-links", action="store_true", help="Verify native priority-isolated TCP links and lossless compression")
    parser.add_argument("--session-lanes", action="store_true", help="Verify independent real-time, sensing and bulk sessions")
    parser.add_argument("--constrained-mbps", type=float, default=0,
                        help="Test-only shared TCP proxy capacity; no host network changes")
    parser.add_argument("--bulk-budget-bps", type=int, default=0,
                        help="Exercise the native bulk-only governor with an atomic, refreshed budget file")
    args = parser.parse_args()
    if args.session_lanes and (args.priority_links or args.hub):
        parser.error("session-lanes is a separate direct-session acceptance mode")
    if args.custom_routes:
        REPLIES.extend(["/fmu/in/trajectory_setpoint", "/navigation/request_waypoints"])
    if args.worker:
        worker(args.worker, args.output, args.bulk, args.worker_duration)
        return
    args.output.mkdir(parents=True, exist_ok=True)
    if args.bulk_budget_bps and not 1024 <= args.bulk_budget_bps <= 1073741824:
        parser.error("bulk-budget-bps must be 1024..1073741824")
    budget_path = (args.output / "bulk-budget.txt").resolve()
    budget_stop = threading.Event()
    def refresh_budget():
        while not budget_stop.is_set():
            temporary = budget_path.with_suffix(".new")
            temporary.write_text(str(args.bulk_budget_bps) + "\n")
            temporary.replace(budget_path)
            budget_stop.wait(.2)
    budget_thread = threading.Thread(target=refresh_budget) if args.bulk_budget_bps else None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    processes, logs = [], []
    proxy = None
    if args.constrained_mbps:
        from constrained_tcp_proxy import ConstrainedProxy
        base_port = int(endpoint.rsplit(":", 1)[1])
        proxy = ConstrainedProxy(("127.0.0.1", base_port), args.constrained_mbps,
            destinations=[("127.0.0.1", base_port+i) for i in range(3)] if args.session_lanes else None).start()
    connect_endpoint = f"tcp/127.0.0.1:{proxy.port}" if proxy else endpoint
    def start(name, command, domain):
        log = (args.output / f"{name}.log").open("w")
        logs.append(log)
        env = dict(os.environ, ROS_DOMAIN_ID=str(domain), ROS_LOCALHOST_ONLY="1", ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST")
        if name.startswith("robot-bridge") and args.bulk_budget_bps:
            env["HORUS_ZENOH_BULK_BUDGET_FILE"] = str(budget_path)
        env.setdefault("RMW_IMPLEMENTATION", "rmw_cyclonedds_cpp")
        if env["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp":
            # Keep test discovery on loopback without changing host interfaces.
            env.setdefault("CYCLONEDDS_URI", '<CycloneDDS><Domain><General><Interfaces>'
                           '<NetworkInterface address="127.0.0.1" multicast="true"/>'
                           '</Interfaces></General><Discovery><ParticipantIndex>auto</ParticipantIndex>'
                           '<MaxAutoParticipantIndex>1000</MaxAutoParticipantIndex></Discovery></Domain></CycloneDDS>')
            if name.endswith("bridge") and env.get("ROS_DISTRO") in {"foxy", "galactic", "humble"}:
                env.pop("CYCLONEDDS_URI", None)
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(proc)
        return proc
    binary = str(args.bridge_binary.resolve())
    configs = {role: str(ROOT / f"config/zenoh_{role}.json5") for role in ("robot", "machine")}
    if args.custom_routes:
        sys.path.insert(0, str(ROOT / "scripts"))
        from transport_policy import resolve
        contract = {"registrations": [{"control": {"teleop": {"command_topic": REPLIES[-2]},
                     "tasks": {"waypoint": {"path_topic": REPLIES[-1]}}}}]}
        for role in configs:
            rendered = args.output / f"{role}-routes.json"
            streams = [{"input_topic": "/camera/owned/image_raw", "transport": "webrtc_only"}] if role == "robot" else []
            rendered.write_text(json.dumps(resolve(configs[role], contract, streams, role)))
            configs[role] = str(rendered.resolve())
    if args.priority_links:
        sys.path.insert(0, str(ROOT / "scripts"))
        from link_profile import configure
        import json5
        port = int(connect_endpoint.rsplit(":", 1)[1])
        for role in configs:
            result = configure(json5.loads(Path(configs[role]).read_text()),
                               target="127.0.0.1" if role == "robot" or args.hub else "", port=port)
            rendered = args.output / f"{role}-priority-links.json"
            rendered.write_text(json.dumps(result))
            configs[role] = str(rendered.resolve())
    try:
        if budget_thread:
            budget_thread.start()
        robot_endpoints = [] if args.priority_links else ["-e", connect_endpoint]
        source_mode = "peer" if args.priority_links else "client"
        session_configs = {}
        if args.session_lanes:
            sys.path.insert(0, str(ROOT / "scripts"))
            import json5
            from session_lanes import configure as sessions
            base_port = int(endpoint.rsplit(":", 1)[1])
            for role in configs:
                profiles = sessions(json5.loads(Path(configs[role]).read_text()),
                    target="127.0.0.1" if role == "robot" else "", port=base_port)
                for index, (name, config) in enumerate(profiles.items()):
                    if role == "robot" and proxy:
                        config["connect"]["endpoints"] = [f"tcp/127.0.0.1:{proxy.ports[index]}#so_sndbuf=65536"]
                    path = args.output / f"{role}-{name}.json"
                    path.write_text(json.dumps(config))
                    session_configs[role, name] = str(path.resolve())
            for name in ("realtime", "sensing", "bulk"):
                start("robot-bridge-"+name, [binary, "-c", session_configs["robot", name],
                    "--no-multicast-scouting", "peer"], 194)
        else:
            start("robot-bridge", [binary, "-c", configs["robot"], *robot_endpoints, "--no-multicast-scouting", source_mode], 194)
        time.sleep(1)
        worker_args = ["--bulk"] if args.bulk else []
        if args.custom_routes:
            worker_args.append("--custom-routes")
        if proxy:
            worker_args.extend(["--worker-duration", "35"])
        robot = start("robot", [sys.executable, __file__, "--worker", "robot", "--output", str(args.output / "robot.json"), *worker_args], 194)
        time.sleep(4)
        if args.hub:
            cloud_config = "config/zenoh_cloud.json5"
            if args.priority_links:
                cloud_path = args.output / "cloud-priority-links.json"
                cloud_path.write_text(json.dumps(configure(json5.loads((ROOT / cloud_config).read_text()))))
                cloud_config = str(cloud_path.resolve())
            hub = start("cloud-bridge", [binary, "-c", cloud_config, "-l", endpoint,
                                         "--no-multicast-scouting", "router"], 193)
            time.sleep(1)
            if hub.poll() is not None:
                raise RuntimeError("Native router rejected cloud configuration; inspect cloud-bridge.log")
        endpoint_args = [*robot_endpoints, "--no-multicast-scouting", source_mode] if args.hub else ["-l", endpoint, "--no-multicast-scouting", "router"]
        if args.session_lanes:
            for name in ("realtime", "sensing", "bulk"):
                bridge = start("machine-bridge-"+name, [binary, "-c", session_configs["machine", name],
                    "--no-multicast-scouting", "router"], 195)
        else:
            bridge = start("machine-bridge", [binary, "-c", configs["machine"], *endpoint_args], 195)
        time.sleep(1)
        if bridge.poll() is not None:
            raise RuntimeError("Native bridge rejected configuration; inspect machine-bridge.log")
        machine = start("machine", [sys.executable, __file__, "--worker", "machine", "--output", str(args.output / "machine.json"), *worker_args], 195)
        if args.priority_links:
            client_pid = processes[0].pid
            deadline = time.monotonic() + 8
            while True:
                sockets = subprocess.check_output(["ss", "-Htnp"], text=True)
                links = [line for line in sockets.splitlines() if f"pid={client_pid}," in line and "ESTAB" in line]
                if len(links) == 3 or time.monotonic() >= deadline:
                    break
                time.sleep(.1)
            (args.output / "native-links.txt").write_text("\n".join(links) + "\n")
            assert len(links) == 3, links
        if args.bulk:
            start("bulk", [sys.executable, __file__, "--worker", "bulk", "--output", str(args.output / "bulk.json")], 194)
            start("integrity", [sys.executable, __file__, "--worker", "integrity", "--output", str(args.output / "integrity.json")], 194)
            start("hmsh", [sys.executable, __file__, "--worker", "hmsh", "--output", str(args.output / "hmsh.json")], 194)
        assert machine.wait(timeout=45 if proxy else 25) == 0
        assert robot.wait(timeout=10) == 0
        robot_data = json.loads((args.output / "robot.json").read_text())
        machine_data = json.loads((args.output / "machine.json").read_text())
        assert all(robot_data["topics"].get(topic, 0) >= 100 for topic in REPLIES), robot_data
        assert all(machine_data["topics"].get(topic, 0) >= 100 for topic in ("/horus/registration", "/fleet/horus/registration", "/robot/odom")), machine_data
        assert sorted(machine_data["static_frames"]) == [f"static_robot_{index}" for index in range(3)], machine_data
        assert all(machine_data["dynamic_frames"].get(f"dynamic_robot_{index}", 0) >= minimum
                   for index, minimum in enumerate((300, 300, 80))), machine_data
        if args.bulk:
            assert machine_data["bulk_integrity"] == json.loads((args.output / "integrity.json").read_text()), "Bulk payloads missing or corrupted"
            assert machine_data["hmsh_latest"] == json.loads((args.output / "hmsh.json").read_text()), "Latest complete mesh geometry/colours missing or corrupted"
            assert machine_data["topics"].get("/acceptance/mesh", 0) >= (5 if proxy else 100), machine_data
            assert all(robot_data["steady_route_latency_ms"].get(topic, {}).get("samples", 0) >= 100 for topic in REPLIES), robot_data
            assert all(stats["p95"] < 100 for stats in robot_data["steady_route_latency_ms"].values()), robot_data
            assert all(stats["p95"] < 100 for stats in machine_data["dynamic_latency_ms"].values()), machine_data
        print("ZENOH_ROUTE_ACCEPTANCE " + json.dumps({"robot": robot_data, "machine": machine_data}))
    finally:
        budget_stop.set()
        if budget_thread and budget_thread.is_alive():
            budget_thread.join(timeout=2)
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()
        if proxy:
            proxy.stop()


if __name__ == "__main__":
    main()
