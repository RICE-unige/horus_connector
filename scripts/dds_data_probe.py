"""Receive one real ROS sample without echoing its data or publishing commands."""

import argparse
import json
import time


def probe(topic, timeout):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from rclpy.executors import SingleThreadedExecutor
    from rosidl_runtime_py.utilities import get_message
    context = rclpy.context.Context()
    rclpy.init(context=context)
    node = None
    executor = None
    result = {"topic": topic, "received": False, "scope": "local_DDS_sample_not_end_to_end"}
    try:
        node = rclpy.create_node("horus_connector_sample_probe", context=context, enable_rosout=False)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        deadline = time.monotonic() + timeout
        subscriptions, profiles = [], set()
        def receive(message):
            result["received"] = True
            header = getattr(message, "header", None)
            if header is not None:
                result["frame_id"] = header.frame_id
                result["source_stamp"] = {"sec": header.stamp.sec, "nanosec": header.stamp.nanosec}
        while time.monotonic() < deadline and not result["received"]:
            endpoints = node.get_publishers_info_by_topic(topic)
            for endpoint in endpoints:
                durability = endpoint.qos_profile.durability
                reliability = (endpoint.qos_profile.reliability if durability == DurabilityPolicy.TRANSIENT_LOCAL
                               else ReliabilityPolicy.BEST_EFFORT)
                key = (endpoint.topic_type, durability, reliability)
                if key not in profiles:
                    message_type = get_message(endpoint.topic_type)
                    subscriptions.append(node.create_subscription(message_type, topic, receive,
                        QoSProfile(depth=1, durability=durability, reliability=reliability)))
                    profiles.add(key)
                    result["type"] = endpoint.topic_type
            executor.spin_once(timeout_sec=min(.1, max(0, deadline - time.monotonic())))
        result["publisher_endpoints"] = len(node.get_publishers_info_by_topic(topic))
        return result
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        context.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--timeout", type=float, default=3)
    args = parser.parse_args()
    if not 0 < args.timeout <= 30 or not args.topic.startswith("/"):
        parser.error("choose an absolute topic and timeout in (0, 30]")
    try:
        result = probe(args.topic, args.timeout)
    except Exception as exc:
        result = {"topic": args.topic, "received": False, "error": str(exc)}
    print(json.dumps(result))
    raise SystemExit(0 if result["received"] else 1)
