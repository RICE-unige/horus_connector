"""Opt-in local DDS delivery checks; no robot command publisher is created."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HORUS_FIELD_ROS_TEST") != "1", reason="enable the isolated native ROS gate")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("latched", [False, True])
def test_probe_receives_data_not_only_discovery(latched, monkeypatch):
    import rclpy
    from rclpy.qos import QoSProfile, DurabilityPolicy
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String
    monkeypatch.setenv("ROS_DOMAIN_ID", "198")
    context = rclpy.context.Context()
    rclpy.init(context=context)
    node = rclpy.create_node("connector_probe_acceptance", context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    topic = f"/acceptance/dds_probe_{os.getpid()}"
    publisher = node.create_publisher(String, topic, QoSProfile(depth=1,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE))
    publisher.publish(String(data="sample content is not echoed"))
    child = subprocess.Popen([sys.executable, str(ROOT / "scripts/dds_data_probe.py"), "--topic", topic,
                              "--timeout", "4"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while child.poll() is None and time.monotonic() < deadline:
            if not latched:
                publisher.publish(String(data="sample content is not echoed"))
            executor.spin_once(timeout_sec=.05)
        stdout, stderr = child.communicate(timeout=1)
        assert child.returncode == 0, stderr + stdout
        result = json.loads(stdout.splitlines()[-1])
        assert result["received"] and result["type"] == "std_msgs/msg/String"
        assert "sample content" not in stdout
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        executor.shutdown()
        node.destroy_node()
        context.shutdown()
