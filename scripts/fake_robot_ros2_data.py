#!/usr/bin/env python3
"""Publish synthetic ROS 2 robot data for connector integration tests."""

import argparse
import math
import struct
import time
from array import array

try:
    import numpy as np
except Exception:
    np = None

import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import Quaternion, TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState, LaserScan, PointCloud2, PointField
from tf2_msgs.msg import TFMessage


def stamp_from_seconds(value: float) -> Time:
    stamp = Time()
    stamp.sec = int(value)
    stamp.nanosec = int((value - stamp.sec) * 1_000_000_000)
    return stamp


def quaternion_from_yaw(yaw: float) -> Quaternion:
    quat = Quaternion()
    quat.z = math.sin(yaw * 0.5)
    quat.w = math.cos(yaw * 0.5)
    return quat


class FakeRobotPublisher(Node):
    def __init__(self, args):
        super().__init__(f"horus_fake_robot_{args.robot_id}")
        self.args = args
        self.robot_id = args.robot_id.strip("/")
        self.frame_prefix = self.robot_id or "robot"
        self.start = time.monotonic()
        self.image_seq = 0
        self.state_seq = 0
        self.points_seq = 0
        self.image_x = None
        self.image_y = None
        if np is not None:
            self.image_x = np.linspace(0, 255, args.width, dtype=np.uint16)[None, :]
            self.image_y = np.linspace(0, 255, args.height, dtype=np.uint16)[:, None]

        image_qos = qos_profile_sensor_data if args.image_qos == "sensor_data" else 10
        self.image_pub = self.create_publisher(Image, args.image_topic, image_qos)
        self.tf_pub = self.create_publisher(TFMessage, "/tf", 10)
        self.odom_pub = self.create_publisher(Odometry, args.odom_topic, 10)
        self.scan_pub = self.create_publisher(LaserScan, args.scan_topic, qos_profile_sensor_data)
        self.joint_pub = self.create_publisher(JointState, args.joint_topic, 10)
        self.points_pub = self.create_publisher(PointCloud2, args.points_topic, qos_profile_sensor_data)

        self.create_timer(1.0 / args.camera_fps, self.publish_image)
        self.create_timer(1.0 / args.state_rate, self.publish_state)
        self.create_timer(1.0 / args.points_rate, self.publish_points)
        self.get_logger().info(
            f"publishing fake robot={self.robot_id} image={args.image_topic} "
            f"{args.width}x{args.height}@{args.camera_fps}Hz"
        )

    def now_msg(self):
        return self.get_clock().now().to_msg()

    def elapsed(self):
        return time.monotonic() - self.start

    def publish_image(self):
        width = self.args.width
        height = self.args.height
        phase = (self.image_seq * 5 + self.args.color_seed) % 256
        if np is not None and self.image_x is not None and self.image_y is not None:
            red = np.broadcast_to((self.image_x + phase) & 0xFF, (height, width)).astype(np.uint8)
            green = np.broadcast_to((self.image_y + self.args.color_seed) & 0xFF, (height, width)).astype(np.uint8)
            blue = (((self.image_x ^ self.image_y) + phase * 2) & 0xFF).astype(np.uint8)
            data = np.dstack((red, green, blue)).ravel().tobytes()
        else:
            data = bytearray(width * height * 3)
            stride = width * 3
            for y in range(height):
                row_base = y * stride
                gy = (y * 255) // max(1, height - 1)
                for x in range(width):
                    gx = (x * 255) // max(1, width - 1)
                    index = row_base + x * 3
                    data[index] = (gx + phase) & 0xFF
                    data[index + 1] = (gy + self.args.color_seed) & 0xFF
                    data[index + 2] = ((gx ^ gy) + phase * 2) & 0xFF

        msg = Image()
        msg.header.stamp = self.now_msg()
        msg.header.frame_id = f"{self.frame_prefix}/camera"
        msg.height = height
        msg.width = width
        msg.encoding = "rgb8"
        msg.is_bigendian = 0
        msg.step = width * 3
        msg.data = array("B", data)
        self.image_pub.publish(msg)
        self.image_seq += 1

    def publish_state(self):
        t = self.elapsed()
        yaw = math.sin(t * 0.35)
        x = math.cos(t * 0.2) * 2.0
        y = math.sin(t * 0.2) * 2.0
        stamp = self.now_msg()

        transform = TransformStamped()
        transform.header.stamp = stamp
        transform.header.frame_id = "map"
        transform.child_frame_id = f"{self.frame_prefix}/base_link"
        transform.transform.translation.x = x
        transform.transform.translation.y = y
        transform.transform.rotation = quaternion_from_yaw(yaw)
        self.tf_pub.publish(TFMessage(transforms=[transform]))

        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "map"
        odom.child_frame_id = f"{self.frame_prefix}/base_link"
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.orientation = quaternion_from_yaw(yaw)
        odom.twist.twist.linear.x = -math.sin(t * 0.2) * 0.4
        odom.twist.twist.angular.z = math.cos(t * 0.35) * 0.35
        self.odom_pub.publish(odom)

        scan = LaserScan()
        scan.header.stamp = stamp
        scan.header.frame_id = f"{self.frame_prefix}/laser"
        scan.angle_min = -math.pi
        scan.angle_max = math.pi
        scan.angle_increment = math.pi / 180.0
        scan.time_increment = 0.0
        scan.scan_time = 1.0 / self.args.state_rate
        scan.range_min = 0.05
        scan.range_max = 20.0
        scan.ranges = [3.0 + 0.5 * math.sin(t + i * 0.05) for i in range(360)]
        self.scan_pub.publish(scan)

        joints = JointState()
        joints.header.stamp = stamp
        joints.name = [f"{self.frame_prefix}/wheel_left", f"{self.frame_prefix}/wheel_right"]
        joints.position = [math.sin(t), math.cos(t)]
        joints.velocity = [math.cos(t), -math.sin(t)]
        self.joint_pub.publish(joints)
        self.state_seq += 1

    def publish_points(self):
        t = self.elapsed()
        width = self.args.point_count
        points = bytearray(width * 16)
        for i in range(width):
            angle = (i / max(1, width)) * math.tau
            radius = 1.0 + 0.4 * math.sin(t + i * 0.1)
            x = radius * math.cos(angle)
            y = radius * math.sin(angle)
            z = 0.2 * math.sin(t * 0.5 + angle)
            intensity = float((i + self.args.color_seed) % 255)
            struct.pack_into("<ffff", points, i * 16, x, y, z, intensity)

        msg = PointCloud2()
        msg.header.stamp = self.now_msg()
        msg.header.frame_id = f"{self.frame_prefix}/lidar"
        msg.height = 1
        msg.width = width
        msg.fields = [
            PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
            PointField(name="intensity", offset=12, datatype=PointField.FLOAT32, count=1),
        ]
        msg.is_bigendian = False
        msg.point_step = 16
        msg.row_step = msg.point_step * width
        msg.is_dense = True
        msg.data = array("B", points)
        self.points_pub.publish(msg)
        self.points_seq += 1


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--robot-id", default="robot1")
    parser.add_argument("--image-topic", default="/camera/image_raw")
    parser.add_argument("--image-qos", choices=["sensor_data", "default"], default="sensor_data")
    parser.add_argument("--odom-topic", default="/odom")
    parser.add_argument("--scan-topic", default="/scan")
    parser.add_argument("--joint-topic", default="/joint_states")
    parser.add_argument("--points-topic", default="/points")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--camera-fps", type=float, default=10.0)
    parser.add_argument("--state-rate", type=float, default=10.0)
    parser.add_argument("--points-rate", type=float, default=1.0)
    parser.add_argument("--point-count", type=int, default=256)
    parser.add_argument("--color-seed", type=int, default=17)
    return parser.parse_args()


def main():
    rclpy.init()
    node = FakeRobotPublisher(parse_args())
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
