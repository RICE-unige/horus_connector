#!/usr/bin/env python3
"""Interactive HORUS Connector environment setup."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path


ROLE_HELP = {
    "robot": "Robot-side system. Sends camera and state, receives velocity commands.",
    "machine": "Operator/local machine. Receives camera and robot state.",
    "teammate": "Field teammate relay. Publishes HoloLens pose/FPV and receives guidance.",
    "cloud": "Shared hub. Runs Zenoh routing and WebRTC signaling only.",
}

TOPOLOGY_HELP = {
    "hub": "Use one cloud hub. Best when machines are on different networks.",
    "direct": "Use VPN, Tailscale, or LAN. No cloud is needed.",
}

ZENOH_TRANSPORT_HELP = {
    "auto": "Use QUIC when TLS is configured, with TCP fallback. Recommended.",
    "tcp": "Use TCP only. Most conservative.",
    "quic": "Use QUIC only. Requires UDP access and Zenoh TLS certs.",
}

VIDEO_PRESETS = {
    "standard": (1280, 720, 30, 6000),
    "light": (960, 540, 30, 1600),
    "high": (1920, 1080, 30, 8000),
}

TEAMMATE_VIDEO_PROFILES = {
    "fast60": "640x360@60, low-latency first-person view. Recommended.",
    "balanced": "640x360@30, lower CPU/network load.",
    "hd720": "1280x720@30, higher detail.",
    "app": "Keep the HoloLens app's current video profile.",
}


def supports_color(no_color: bool) -> bool:
    return not no_color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class Theme:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.reset = "\033[0m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.cyan = "\033[38;5;51m" if enabled else ""
        self.blue = "\033[38;5;39m" if enabled else ""
        self.green = "\033[38;5;48m" if enabled else ""
        self.yellow = "\033[38;5;214m" if enabled else ""
        self.red = "\033[38;5;203m" if enabled else ""

    def paint(self, value: str, color: str) -> str:
        return f"{getattr(self, color)}{value}{self.reset}"


class Wizard:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root = Path(args.root).resolve()
        self.env_path = Path(args.env).resolve()
        self.template_path = Path(args.template).resolve() if args.template else self.root / ".env.example"
        self.color = Theme(supports_color(args.no_color))
        self.interactive = sys.stdin.isatty() and not args.yes
        self.existing = read_env(self.env_path)
        self.values: dict[str, str] = {}
        self.streams: list[dict[str, object]] = []
        self.step_counter = 0

    def header(self) -> None:
        c = self.color
        print()
        print(c.paint("HORUS Connector setup", "cyan"))
        print(c.paint("Configure this machine without editing .env by hand.", "dim"))
        print()

    def section(self, index: int, total: int, title: str) -> None:
        c = self.color
        _ = (index, total)
        self.step_counter += 1
        print(c.paint(f"Step {self.step_counter}: {title}", "blue"))

    def prompt_text(
        self,
        label: str,
        default: str = "",
        required: bool = False,
        validator=None,
        hint: str | None = None,
    ) -> str:
        c = self.color
        while True:
            if not self.interactive:
                value = default
            else:
                suffix = f" [{default}]" if default else ""
                print(f"{c.paint('?', 'cyan')} {label}{c.paint(suffix, 'dim')}")
                if hint:
                    print(f"  {c.paint(hint, 'dim')}")
                value = input("  > ").strip() or default
            if required and not value:
                if not self.interactive:
                    raise SystemExit(f"{label} is required")
                print(c.paint("  This value is required.", "yellow"))
                continue
            if validator and value and not validator(value):
                if not self.interactive:
                    raise SystemExit(f"Invalid value for {label}: {value}")
                print(c.paint("  That value does not look valid.", "yellow"))
                continue
            print()
            return value

    def prompt_choice(
        self,
        label: str,
        choices: list[str],
        default: str,
        descriptions: dict[str, str] | None = None,
    ) -> str:
        c = self.color
        if not self.interactive:
            return default if default in choices else choices[0]
        while True:
            print(f"{c.paint('?', 'cyan')} {label}")
            for idx, choice in enumerate(choices, start=1):
                marker = c.paint("*", "green") if choice == default else " "
                description = descriptions.get(choice, "") if descriptions else ""
                extra = c.paint(f" - {description}", "dim") if description else ""
                print(f"  {marker} {idx}. {choice}{extra}")
            answer = input(f"  > [{default}] ").strip().lower()
            if not answer:
                print()
                return default
            if answer.isdigit():
                index = int(answer) - 1
                if 0 <= index < len(choices):
                    print()
                    return choices[index]
            matches = [choice for choice in choices if choice.startswith(answer)]
            if len(matches) == 1:
                print()
                return matches[0]
            print(c.paint("  Choose one of the listed options.", "yellow"))

    def prompt_yes_no(self, label: str, default: bool, hint: str | None = None) -> bool:
        if not self.interactive:
            return default
        c = self.color
        suffix = "Y/n" if default else "y/N"
        while True:
            print(f"{c.paint('?', 'cyan')} {label} {c.paint(f'[{suffix}]', 'dim')}")
            if hint:
                print(f"  {c.paint(hint, 'dim')}")
            answer = input("  > ").strip().lower()
            print()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            print(c.paint("  Answer yes or no.", "yellow"))

    def run(self) -> None:
        self.header()
        if self.env_path.exists() and not self.args.force:
            if not self.prompt_yes_no(
                f"{self.env_path} already exists. Update it?",
                True,
                "A timestamped backup is written before changes are applied.",
            ):
                print("No changes made.")
                return

        total = 6
        self.section(1, total, "Machine role")
        default_role = self.args.role or valid_or(self.existing.get("HORUS_ROLE"), ROLE_HELP, "robot")
        while True:
            role = self.prompt_choice("What role should this computer run?", list(ROLE_HELP), default_role, ROLE_HELP)
            if not self.interactive or role == default_role:
                break
            if self.prompt_yes_no(
                f"You changed the role from {default_role} to {role}. Continue?",
                True,
                "Choose no if this is accidental; the role controls which services run here.",
            ):
                break
        print(f"Selected role: {self.color.paint(role, 'green')} - {ROLE_HELP[role]}\n")

        self.section(2, total, "Connection topology")
        if role == "cloud":
            topology = "hub"
            print(f"Cloud role uses topology: {self.color.paint('hub', 'green')}\n")
        else:
            default_topology = self.args.topology or valid_or(self.existing.get("HORUS_TOPOLOGY"), TOPOLOGY_HELP, "hub")
            topology = self.prompt_choice("How should this machine connect?", list(TOPOLOGY_HELP), default_topology, TOPOLOGY_HELP)

        hostname = sanitize_name(socket.gethostname()) or "robot-a"
        default_room = self.args.room or self.existing.get("HORUS_ROOM") or ("default" if role == "cloud" else hostname)
        if role == "cloud":
            room = "default"
            print(f"Cloud room uses: {self.color.paint(room, 'green')}\n")
        else:
            identity_title = "Teammate identity" if role == "teammate" else "Robot identity"
            identity_label = "Teammate name" if role == "teammate" else "Room / robot name"
            identity_hint = (
                "Use one stable name for this field teammate, for example teammate-a."
                if role == "teammate"
                else "Use one stable name per robot, for example robot-a or mobile-base-01."
            )
            self.section(3, total, identity_title)
            room = self.prompt_text(
                identity_label,
                default_room,
                required=True,
                validator=is_name,
                hint=identity_hint,
            )

        if role == "cloud":
            self.section(3, total, "Cloud address")
        else:
            self.section(4, total, "Network address")
        cloud_default = self.args.cloud_ip or self.existing.get("HORUS_CLOUD_IP", "")
        machine_default = self.args.machine_ip or self.existing.get("HORUS_MACHINE_IP", "")
        if topology == "hub":
            cloud_ip = self.prompt_text(
                "Cloud public IP or DNS name",
                cloud_default,
                required=True,
                validator=is_host,
                hint="Endpoints use this to reach the hub.",
            )
            machine_ip = ""
        else:
            cloud_ip = self.args.cloud_ip or ""
            if role in {"robot", "teammate"}:
                machine_ip = self.prompt_text(
                    "Operator machine VPN/LAN IP or DNS name",
                    machine_default,
                    required=True,
                    validator=is_host,
                    hint=f"You selected {role}. In direct mode, this endpoint connects to the operator machine at this address.",
                )
            else:
                machine_ip = self.prompt_text(
                    "This machine's VPN/LAN IP or DNS name",
                    machine_default or detect_local_ip(),
                    required=False,
                    validator=is_host,
                    hint="You selected machine. This is mainly shown so you can give the address to robot-side users.",
                )

        self.section(5, total, "ROS profile")
        ros_distro = self.resolve_ros_distro()
        ros_domain = self.resolve_ros_domain_id()
        ros_setup_path = self.resolve_ros_setup_path(ros_distro, role)

        namespace_default = self.args.namespace
        if namespace_default is None:
            namespace_default = f"/{ros_safe_name(room)}" if role == "robot" else ""
        namespace = namespace_default
        print(f"Zenoh namespace: {self.color.paint(namespace or '-', 'green')}")
        print("  Robots use /robot_name. Machines, teammates, and cloud usually stay empty.\n")
        zenoh_transport = self.prompt_choice(
            "Zenoh transport",
            list(ZENOH_TRANSPORT_HELP),
            self.args.zenoh_transport or valid_or(self.existing.get("ZENOH_TRANSPORT"), ZENOH_TRANSPORT_HELP, "auto"),
            ZENOH_TRANSPORT_HELP,
        )
        existing_quic = self.existing.get("ZENOH_AUTO_ENABLE_QUIC", "0").lower() in {"1", "true", "yes", "on"}
        if zenoh_transport == "tcp":
            auto_enable_quic = False
        elif self.args.enable_quic or zenoh_transport == "quic":
            auto_enable_quic = True
        elif zenoh_transport == "auto" and self.interactive:
            auto_enable_quic = self.prompt_yes_no(
                "Enable Zenoh QUIC fast path when TLS certs are available?",
                existing_quic,
                "HORUS will still keep TCP as fallback in auto mode.",
            )
        else:
            auto_enable_quic = existing_quic

        self.section(6, total, "Camera and runtime")
        existing_role = self.existing.get("HORUS_ROLE")
        existing_teammate_name = self.existing.get("FIELD_TEAMMATE_NAME") if existing_role == "teammate" else ""
        teammate_name = self.args.teammate_name or existing_teammate_name or room
        hololens_host = (
            self.args.hololens_host
            or self.existing.get("FIELD_TEAMMATE_HOLOLENS_HOST")
            or self.existing.get("HOLOLENS_HOST", "")
        )
        teammate_video_profile = self.args.teammate_video_profile or valid_or(
            self.existing.get("FIELD_TEAMMATE_VIDEO_PROFILE"),
            TEAMMATE_VIDEO_PROFILES,
            "fast60",
        )
        teammate_raw_image = self.args.teammate_raw_image or (
            self.existing.get("FIELD_TEAMMATE_RAW_IMAGE", "0").lower() in {"1", "true", "yes", "on"}
        )
        if role in {"robot", "machine"}:
            camera_default = self.args.camera_topic or self.existing.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC") or "/camera/image_raw"
            output_default = self.camera_output_default(role, room)
            camera_topic = self.prompt_text("Robot camera input topic", camera_default, required=True, validator=is_topic)
            output_topic = self.prompt_text("Decoded camera output topic on machine", output_default, required=True, validator=is_topic)
            preset = self.args.video_preset or "standard"
            if preset == "custom" or self.args.width or self.args.height or self.args.fps or self.args.bitrate:
                width = int(self.args.width or self.existing.get("WEBRTC_VIDEO_WIDTH") or 1280)
                height = int(self.args.height or self.existing.get("WEBRTC_VIDEO_HEIGHT") or 720)
                fps = int(self.args.fps or self.existing.get("WEBRTC_VIDEO_FPS") or 30)
                bitrate = int(self.args.bitrate or self.existing.get("VIDEO_BITRATE_KBIT") or 6000)
            else:
                preset = self.prompt_choice(
                    "Video profile",
                    list(VIDEO_PRESETS) + ["custom"],
                    preset if preset in VIDEO_PRESETS else "standard",
                    {
                        "standard": "720p30, good quality, current default.",
                        "light": "540p30, safer on weak links.",
                        "high": "1080p30, higher bandwidth.",
                        "custom": "Enter width, height, fps, and bitrate.",
                    },
                )
                if preset == "custom":
                    width = int(self.prompt_text("Video width", self.existing.get("WEBRTC_VIDEO_WIDTH") or "1280", True, is_uint))
                    height = int(self.prompt_text("Video height", self.existing.get("WEBRTC_VIDEO_HEIGHT") or "720", True, is_uint))
                    fps = int(self.prompt_text("Video FPS", self.existing.get("WEBRTC_VIDEO_FPS") or "30", True, is_uint))
                    bitrate = int(self.prompt_text("Video bitrate kbit/s", self.existing.get("VIDEO_BITRATE_KBIT") or "6000", True, is_uint))
                else:
                    width, height, fps, bitrate = VIDEO_PRESETS[preset]
            encoder_preference = self.args.encoder_preference or valid_or(
                self.existing.get("WEBRTC_ENCODER_PREFERENCE"),
                {"stable": "", "hardware": "", "software": ""},
                "stable",
            )
            self.streams = self.configure_webrtc_streams(
                role,
                room,
                camera_topic,
                output_topic,
                width,
                height,
                fps,
                bitrate,
            )
        elif role == "teammate":
            teammate_name = self.prompt_text(
                "Teammate ROS name",
                teammate_name,
                required=True,
                validator=is_name,
                hint="This becomes the ROS topic prefix, for example /teammate-a/fpv/image_raw/compressed.",
            )
            hololens_host = self.prompt_text(
                "HoloLens IP or DNS name",
                hololens_host,
                required=True,
                validator=is_host,
                hint="Use the address shown by the HORUS Lenses status panel.",
            )
            teammate_video_profile = self.prompt_choice(
                "HoloLens video profile",
                list(TEAMMATE_VIDEO_PROFILES),
                teammate_video_profile,
                TEAMMATE_VIDEO_PROFILES,
            )
            teammate_raw_image = self.prompt_yes_no(
                "Also publish decoded raw image locally?",
                teammate_raw_image,
                "Compressed FPV is transported by default. Raw image is only for local viewers/debugging.",
            )
            safe_name = ros_safe_name(teammate_name)
            camera_topic = f"/{safe_name}/fpv/image_raw/compressed"
            output_topic = camera_topic
            if teammate_video_profile == "hd720":
                width, height, fps = 1280, 720, 30
            elif teammate_video_profile == "balanced":
                width, height, fps = 640, 360, 30
            else:
                width, height, fps = 640, 360, 60
            bitrate = int(self.existing.get("VIDEO_BITRATE_KBIT", "6000"))
            encoder_preference = "stable"
            self.streams = []
        else:
            camera_topic = self.existing.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "/camera/image_raw")
            output_topic = self.existing.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC", "/camera/webrtc/image_raw")
            width = int(self.existing.get("WEBRTC_VIDEO_WIDTH", "1280"))
            height = int(self.existing.get("WEBRTC_VIDEO_HEIGHT", "720"))
            fps = int(self.existing.get("WEBRTC_VIDEO_FPS", "30"))
            bitrate = int(self.existing.get("VIDEO_BITRATE_KBIT", "6000"))
            encoder_preference = "stable"
            self.streams = []
            print("Cloud role skips media encoding and decoding.\n")

        existing_turn = (
            self.existing.get("HORUS_CLOUD_RUN_TURN") == "1"
            or "turn:" in self.existing.get("WEBRTC_ICE_SERVERS", "")
            or "turns:" in self.existing.get("WEBRTC_ICE_SERVERS", "")
        )
        use_turn = self.args.turn or existing_turn
        if self.interactive and topology == "hub":
            use_turn = self.prompt_yes_no(
                "Enable TURN fallback?",
                use_turn,
                "TURN relays media through the cloud only when direct WebRTC cannot connect.",
            )
        turn_user = self.args.turn_user or self.existing.get("TURN_USER", "horus")
        existing_turn_password = self.existing.get("TURN_PASSWORD", "")
        generated_turn_password = generate_turn_password()
        if self.args.turn_password:
            turn_password = self.args.turn_password
        elif role == "cloud" and (not existing_turn_password or existing_turn_password == "change-me"):
            turn_password = generated_turn_password
        else:
            turn_password = existing_turn_password
        turn_port = self.args.turn_port or self.existing.get("TURN_PORT", "3478")
        turn_min_port = normalize_turn_min_port(self.args.turn_min_port or self.existing.get("TURN_MIN_PORT", ""))
        turn_max_port = normalize_turn_max_port(self.args.turn_max_port or self.existing.get("TURN_MAX_PORT", ""))
        turn_realm = self.args.turn_realm or self.existing.get("TURN_REALM", "horus")
        if use_turn:
            turn_user = self.prompt_text("TURN username", turn_user, True, is_name)
            turn_password = self.prompt_text(
                "TURN password",
                turn_password,
                True,
                is_turn_secret,
                "Use the same value on the cloud, robot, and machine. Allowed: letters, numbers, dot, underscore, dash, tilde.",
            )

        ice_servers = "stun:stun.l.google.com:19302"
        if use_turn and cloud_ip:
            ice_servers = f"{ice_servers},turn://{turn_user}:{turn_password}@{cloud_ip}:{turn_port}"

        self.values = {
            "HORUS_ROLE": role,
            "HORUS_ROOM": room,
            "HORUS_TOPOLOGY": topology,
            "HORUS_CLOUD_IP": cloud_ip,
            "HORUS_MACHINE_IP": machine_ip,
            "HORUS_WEBRTC_SIGNAL_IP": self.args.signal_ip or self.existing.get("HORUS_WEBRTC_SIGNAL_IP", ""),
            "ROS_DISTRO": ros_distro,
            "ROS_DOMAIN_ID": ros_domain,
            "ROS_SETUP_PATH": ros_setup_path,
            "ROS_LOCALHOST_ONLY": "1",
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
            "ROS_CMD_TOPIC": self.existing.get("ROS_CMD_TOPIC", "/cmd_vel"),
            "ZENOH_NAMESPACE": namespace,
            "ZENOH_CONFIG": "auto",
            "ZENOH_TRANSPORT": zenoh_transport,
            "ZENOH_AUTO_ENABLE_QUIC": "1" if auto_enable_quic else "0",
            "ZENOH_QUIC_PARAMS": self.existing.get("ZENOH_QUIC_PARAMS", "multistream=1;mixed_rel=auto"),
            "ZENOH_TLS_DIR": self.existing.get("ZENOH_TLS_DIR", ""),
            "ZENOH_TLS_ROOT_CA": self.existing.get("ZENOH_TLS_ROOT_CA", ""),
            "ZENOH_TLS_LISTEN_KEY": self.existing.get("ZENOH_TLS_LISTEN_KEY", ""),
            "ZENOH_TLS_LISTEN_CERT": self.existing.get("ZENOH_TLS_LISTEN_CERT", ""),
            "ZENOH_TLS_VERIFY_NAME": self.existing.get("ZENOH_TLS_VERIFY_NAME", "0"),
            "HORUS_ZENOH_ENABLED": "1",
            "WEBRTC_MEDIA_MODE": "h264",
            "HORUS_ALLOW_LEGACY_JPEG": "0",
            "WEBRTC_ICE_SERVERS": ice_servers,
            "WEBRTC_ICE_TRANSPORT_POLICY": self.existing.get("WEBRTC_ICE_TRANSPORT_POLICY", "all"),
            "HORUS_CLOUD_RUN_TURN": "1" if role == "cloud" and use_turn else "0",
            "TURN_PORT": turn_port,
            "TURN_MIN_PORT": turn_min_port,
            "TURN_MAX_PORT": turn_max_port,
            "TURN_REALM": turn_realm,
            "TURN_USER": turn_user,
            "TURN_PASSWORD": turn_password,
            "WEBRTC_VIDEO_WIDTH": str(width),
            "WEBRTC_VIDEO_HEIGHT": str(height),
            "WEBRTC_VIDEO_FPS": str(fps),
            "WEBRTC_VIDEO_SOURCE": "ros2",
            "WEBRTC_VIDEO_OUTPUT": "ros2",
            "WEBRTC_VIDEO_SINK": "fakesink",
            "WEBRTC_ROS_IMAGE_INPUT_TOPIC": camera_topic,
            "WEBRTC_ROS_IMAGE_OUTPUT_TOPIC": output_topic,
            "WEBRTC_ROS_IMAGE_QOS": "auto",
            "HORUS_STREAMS_CONFIG": "config/webrtc_streams.json",
            "VIDEO_BITRATE_KBIT": str(bitrate),
            "WEBRTC_MIN_BITRATE_KBIT": "1000",
            "WEBRTC_MAX_BITRATE_KBIT": str(bitrate),
            "WEBRTC_ENCODER_PREFERENCE": encoder_preference,
            "WEBRTC_DECODER_PREFERENCE": encoder_preference,
            "WEBRTC_CONTROL_ENABLED": "0",
            "WEBRTC_ENABLE_CONTROL": "0",
            "FIELD_TEAMMATE_NAME": teammate_name,
            "FIELD_TEAMMATE_HOLOLENS_HOST": hololens_host,
            "FIELD_TEAMMATE_PV_PORT": self.existing.get("FIELD_TEAMMATE_PV_PORT", "3810"),
            "FIELD_TEAMMATE_SPATIAL_INPUT_PORT": self.existing.get("FIELD_TEAMMATE_SPATIAL_INPUT_PORT", "3814"),
            "FIELD_TEAMMATE_UMQ_PORT": self.existing.get("FIELD_TEAMMATE_UMQ_PORT", "3816"),
            "FIELD_TEAMMATE_MAP_FRAME": self.existing.get("FIELD_TEAMMATE_MAP_FRAME", "map"),
            "FIELD_TEAMMATE_PROFILE_HEIGHT": self.existing.get("FIELD_TEAMMATE_PROFILE_HEIGHT", "1.75"),
            "FIELD_TEAMMATE_CAMERA_HEIGHT": self.existing.get("FIELD_TEAMMATE_CAMERA_HEIGHT", "0.0"),
            "FIELD_TEAMMATE_FLOOR_HEIGHT": self.existing.get("FIELD_TEAMMATE_FLOOR_HEIGHT", "0.0"),
            "FIELD_TEAMMATE_POSE_ORIGIN": self.existing.get("FIELD_TEAMMATE_POSE_ORIGIN", "camera"),
            "FIELD_TEAMMATE_CONNECT_TIMEOUT": self.existing.get("FIELD_TEAMMATE_CONNECT_TIMEOUT", "2.0"),
            "FIELD_TEAMMATE_RECONNECT_DELAY": self.existing.get("FIELD_TEAMMATE_RECONNECT_DELAY", "1.0"),
            "FIELD_TEAMMATE_VIDEO_PROFILE": teammate_video_profile,
            "FIELD_TEAMMATE_RAW_IMAGE": "1" if teammate_raw_image else "0",
            "FIELD_TEAMMATE_VIDEO_MODE": self.existing.get("FIELD_TEAMMATE_VIDEO_MODE", ""),
            "FIELD_TEAMMATE_VIDEO_WIDTH": self.existing.get("FIELD_TEAMMATE_VIDEO_WIDTH", ""),
            "FIELD_TEAMMATE_VIDEO_HEIGHT": self.existing.get("FIELD_TEAMMATE_VIDEO_HEIGHT", ""),
            "FIELD_TEAMMATE_VIDEO_FPS": self.existing.get("FIELD_TEAMMATE_VIDEO_FPS", ""),
            "FIELD_TEAMMATE_VIDEO_QUALITY": self.existing.get("FIELD_TEAMMATE_VIDEO_QUALITY", ""),
            "FAKE_DATA_ROBOT_ID": room if role != "cloud" else "robot-a",
            "FAKE_ROS_IMAGE_TOPIC": camera_topic,
            "FAKE_ROS_IMAGE_QOS": "default",
            "FAKE_CAMERA_WIDTH": str(width),
            "FAKE_CAMERA_HEIGHT": str(height),
            "FAKE_CAMERA_FPS": str(fps),
            "FAKE_FRAME_CACHE": "30",
            "CMD_RATE": "0",
            "CMD_LINEAR_X": "0.0",
            "CMD_ANGULAR_Z": "0.0",
        }

        self.write()
        self.write_streams_config()
        self.summary()

    def resolve_ros_distro(self) -> str:
        installed = detect_ros_distros()
        env_distro = os.environ.get("ROS_DISTRO")
        shell_distro = read_shell_default("ROS_DISTRO")
        if self.args.ros_distro:
            ros_distro = self.args.ros_distro
        elif env_distro:
            ros_distro = env_distro
        elif shell_distro:
            ros_distro = shell_distro
        elif len(installed) == 1:
            ros_distro = installed[0]
        elif self.existing.get("ROS_DISTRO") in installed:
            ros_distro = self.existing["ROS_DISTRO"]
        elif installed:
            ros_distro = installed[-1]
        else:
            ros_distro = self.existing.get("ROS_DISTRO") or "jazzy"

        if self.interactive and len(installed) > 1 and not self.args.ros_distro:
            ros_distro = self.prompt_choice(
                "Multiple ROS 2 distros were found. Which one should HORUS use?",
                installed,
                ros_distro,
            )
        else:
            source = "detected" if ros_distro in installed else "default"
            print(f"ROS 2 distro: {self.color.paint(ros_distro, 'green')} ({source})")
            if not installed:
                print("  ROS 2 was not found under /opt/ros. Install ROS 2 before bootstrap/launch on robot, machine, or teammate roles.")
            print()
        return ros_distro

    def resolve_ros_domain_id(self) -> str:
        terminal_domain = os.environ.get("ROS_DOMAIN_ID")
        shell_domain = read_shell_default("ROS_DOMAIN_ID")
        existing_domain = self.existing.get("ROS_DOMAIN_ID")
        domain = self.args.ros_domain_id or terminal_domain or shell_domain or existing_domain or "0"
        if not is_uint(str(domain)):
            domain = "0"
        hint = (
            f"terminal={terminal_domain or '-'}  "
            f"shell={shell_domain or '-'}  "
            f".env={existing_domain or '-'}"
        )
        return self.prompt_text(
            "ROS_DOMAIN_ID",
            str(domain),
            required=True,
            validator=is_uint,
            hint=f"Default source: {hint}. Change this if the robot/machine ROS graph uses another domain.",
        )

    def resolve_ros_setup_path(self, ros_distro: str, role: str) -> str:
        if role == "cloud":
            print("ROS setup file: - (cloud does not need local ROS 2 nodes)\n")
            return ""

        candidates = detect_ros_setup_candidates(ros_distro)
        existing_path = self.existing.get("ROS_SETUP_PATH", "")
        if self.args.ros_setup_path:
            default = self.args.ros_setup_path
        elif existing_path and Path(os.path.expanduser(existing_path)).exists():
            default = existing_path
        elif candidates:
            default = candidates[0]
        elif existing_path:
            default = existing_path
        else:
            default = f"/opt/ros/{ros_distro}/setup.bash"

        if Path(os.path.expanduser(default)).exists():
            default = expand_path(default)
            print(f"ROS setup file: {self.color.paint(default, 'green')}")
            print("  This file will be sourced before HORUS starts ROS-related services.\n")
            return default

        setup_path = self.prompt_text(
            "ROS setup file path",
            default,
            required=True,
            validator=is_existing_file,
            hint="ROS was not found at the expected path. Enter the setup.bash/local_setup.bash path from the ROS install or workspace.",
        )
        return expand_path(setup_path)

    def camera_output_default(self, role: str, room: str) -> str:
        if self.args.camera_output_topic:
            return self.args.camera_output_topic
        if role == "machine":
            expected = f"/{ros_safe_name(room)}/camera/webrtc/image_raw"
            existing_room = self.existing.get("HORUS_ROOM", "")
            existing_output = self.existing.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC", "")
            if existing_room == room and existing_output:
                return existing_output
            return expected
        return self.existing.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC") or "/camera/webrtc/image_raw"

    def streams_config_path(self) -> Path:
        configured = self.values.get("HORUS_STREAMS_CONFIG") or self.existing.get("HORUS_STREAMS_CONFIG") or "config/webrtc_streams.json"
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if path.name == "machine_streams.json" and not path.exists():
            path = self.root / "config" / "webrtc_streams.json"
        return path

    def existing_streams(self) -> list[dict[str, object]]:
        path = self.streams_config_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        streams = data.get("streams", []) if isinstance(data, dict) else data
        if not isinstance(streams, list):
            return []
        return [stream for stream in streams if isinstance(stream, dict)]

    def configure_webrtc_streams(
        self,
        role: str,
        room: str,
        camera_topic: str,
        output_topic: str,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
    ) -> list[dict[str, object]]:
        existing = self.existing_streams()
        default_count = len(existing) if existing else 1
        if self.args.stream_count and not is_positive_uint(str(self.args.stream_count)):
            raise SystemExit("--stream-count must be a positive integer")
        requested_count = int(self.args.stream_count or default_count)
        if self.interactive:
            requested_count = int(
                self.prompt_text(
                    "WebRTC camera streams",
                    str(requested_count),
                    required=True,
                    validator=is_positive_uint,
                    hint="Use 1 for the common case. Use more when this endpoint sends or receives multiple cameras.",
                )
            )

        streams: list[dict[str, object]] = []
        for index in range(requested_count):
            existing_stream = existing[index] if index < len(existing) else {}
            default_id = str(existing_stream.get("id") or ("primary" if index == 0 else f"camera-{index + 1}"))
            default_room = str(existing_stream.get("room") or (room if index == 0 else f"{room}-{default_id}"))
            identifier = default_id
            stream_room = default_room
            input_default = str(existing_stream.get("input_topic") or (camera_topic if index == 0 else f"/camera/{ros_safe_name(default_id)}/image_raw"))
            output_default = str(existing_stream.get("output_topic") or (output_topic if index == 0 else self.stream_output_default(default_room, default_id)))
            if self.interactive and requested_count > 1:
                print(self.color.paint(f"Camera stream {index + 1}", "blue"))
                identifier = self.prompt_text("Stream ID", default_id, required=True, validator=is_name)
                stream_room = self.prompt_text(
                    "WebRTC room",
                    stream_room,
                    required=True,
                    validator=is_name,
                    hint="Sender and receiver must use the same room for this camera stream.",
                )
                if role == "robot":
                    input_default = self.prompt_text("ROS image input topic", input_default, required=True, validator=is_topic)
                    output_default = self.stream_output_default(stream_room, identifier)
                else:
                    output_default = self.prompt_text("Decoded ROS image output topic", self.stream_output_default(stream_room, identifier), required=True, validator=is_topic)
                    input_default = str(existing_stream.get("input_topic") or camera_topic)

            streams.append(
                {
                    "id": sanitize_name(identifier) or "primary",
                    "label": str(existing_stream.get("label") or identifier),
                    "room": stream_room,
                    "enabled": bool(existing_stream.get("enabled", True)),
                    "input_topic": input_default,
                    "output_topic": output_default,
                    "width": int(existing_stream.get("width") or width),
                    "height": int(existing_stream.get("height") or height),
                    "fps": int(existing_stream.get("fps") or fps),
                    "bitrate_kbit": int(existing_stream.get("bitrate_kbit") or bitrate),
                    "video_source": str(existing_stream.get("video_source") or "ros2"),
                    "source_pipeline": str(existing_stream.get("source_pipeline") or ""),
                    "ros_image_qos": str(existing_stream.get("ros_image_qos") or "auto"),
                    "frame_id": str(existing_stream.get("frame_id") or f"{ros_safe_name(stream_room)}_{ros_safe_name(identifier)}_webrtc_camera"),
                }
            )
        return streams

    def stream_output_default(self, room: str, identifier: str) -> str:
        room_token = ros_safe_name(room)
        stream_token = ros_safe_name(identifier)
        if stream_token == "primary":
            return f"/{room_token}/camera/webrtc/image_raw"
        return f"/{room_token}/camera/{stream_token}/webrtc/image_raw"

    def write(self) -> None:
        content = self.template_path.read_text(encoding="utf-8")
        merged = dict(read_env(self.template_path))
        merged.update(self.existing)
        merged.update(self.values)
        rendered = update_env_text(content, merged)
        if self.args.dry_run:
            print(rendered)
            return
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        if self.env_path.exists():
            backup = self.env_path.with_name(f".env.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.env_path, backup)
            print(f"Backup written: {backup}")
        self.env_path.write_text(rendered, encoding="utf-8")
        print(f"Configuration written: {self.env_path}\n")

    def write_streams_config(self) -> None:
        if self.args.dry_run or not self.streams:
            return
        path = self.streams_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(f"{path.name}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
            print(f"Stream config backup written: {backup}")
        payload = {"version": 1, "streams": self.streams}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Stream config written: {path}\n")

    def summary(self) -> None:
        if self.args.dry_run:
            return
        c = self.color
        role = self.values["HORUS_ROLE"]
        topology = self.values["HORUS_TOPOLOGY"]
        print(c.paint("Ready", "green"))
        print(f"  role       {role}")
        print(f"  topology   {topology}")
        print(f"  room       {self.values['HORUS_ROOM']}")
        print(f"  cloud      {self.values['HORUS_CLOUD_IP'] or '-'}")
        print(f"  machine    {self.values['HORUS_MACHINE_IP'] or '-'}")
        print(f"  ros        {self.values['ROS_DISTRO']} domain {self.values['ROS_DOMAIN_ID']}")
        if self.values.get("ROS_SETUP_PATH"):
            print(f"  ros setup  {self.values['ROS_SETUP_PATH']}")
        print(f"  namespace  {self.values['ZENOH_NAMESPACE'] or '-'}")
        print(f"  transport  {self.values['ZENOH_TRANSPORT']}")
        print(f"  quic       {'enabled' if self.values['ZENOH_AUTO_ENABLE_QUIC'] == '1' else 'disabled'}")
        if role == "cloud":
            turn_status = "enabled" if self.values["HORUS_CLOUD_RUN_TURN"] == "1" else "disabled"
            print(f"  turn       {turn_status}")
        elif "turn:" in self.values.get("WEBRTC_ICE_SERVERS", ""):
            print("  turn       fallback configured")
        if role == "teammate":
            print(f"  teammate  {self.values['FIELD_TEAMMATE_NAME']}")
            print(f"  hololens  {self.values['FIELD_TEAMMATE_HOLOLENS_HOST']}")
            print(f"  fpv        {self.values['FIELD_TEAMMATE_VIDEO_PROFILE']} -> {self.values['WEBRTC_ROS_IMAGE_OUTPUT_TOPIC']}")
        elif role != "cloud":
            print(
                "  camera     "
                f"{self.values['WEBRTC_VIDEO_WIDTH']}x{self.values['WEBRTC_VIDEO_HEIGHT']}"
                f"@{self.values['WEBRTC_VIDEO_FPS']} -> {self.values['WEBRTC_ROS_IMAGE_OUTPUT_TOPIC']}"
            )
            if self.streams:
                stream_names = ", ".join(str(stream["id"]) for stream in self.streams)
                print(f"  streams    {len(self.streams)} ({stream_names})")
        print()
        print(c.paint("Next commands", "cyan"))
        print(f"  ./horus bootstrap {role}")
        print(f"  ./horus launch {role}")
        print("  ./horus status")
        print()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def read_shell_default(name: str) -> str:
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(name)}=(.+)$")
    for file_name in (".bashrc", ".bash_profile", ".profile"):
        path = Path.home() / file_name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = pattern.match(stripped)
            if match:
                return match.group(1).strip().strip('"').strip("'")
    return ""


def update_env_text(text: str, values: dict[str, str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            key = match.group(1)
            if key in values:
                lines.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        lines.append(line)
    missing = [key for key in values if key not in seen]
    if missing:
        lines.append("")
        lines.append("# Added by setup wizard.")
        for key in missing:
            lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def detect_ros_distros() -> list[str]:
    root = Path("/opt/ros")
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if (path / "setup.bash").exists())


def detect_ros_setup_candidates(ros_distro: str) -> list[str]:
    candidates: list[str] = []
    for value in (
        os.environ.get("ROS_SETUP_PATH", ""),
        read_shell_default("ROS_SETUP_PATH"),
        f"/opt/ros/{ros_distro}/setup.bash",
        f"/opt/ros/{ros_distro}/local_setup.bash",
    ):
        add_setup_candidate(candidates, value)

    for env_name in ("AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH", "CMAKE_PREFIX_PATH"):
        for prefix in os.environ.get(env_name, "").split(os.pathsep):
            if not prefix:
                continue
            for file_name in ("setup.bash", "local_setup.bash", "setup.sh", "local_setup.sh"):
                add_setup_candidate(candidates, str(Path(prefix) / file_name))
    return candidates


def add_setup_candidate(candidates: list[str], value: str) -> None:
    if not value:
        return
    expanded = expand_path(value)
    if expanded not in candidates and Path(expanded).exists():
        candidates.append(expanded)


def expand_path(value: str) -> str:
    expanded = os.path.expanduser(value)
    path = Path(expanded)
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return expanded


def detect_ros_distro() -> str:
    distros = detect_ros_distros()
    if "jazzy" in distros:
        return "jazzy"
    if "humble" in distros:
        return "humble"
    return distros[-1] if distros else "jazzy"


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def unique(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def valid_or(value: str | None, choices: dict[str, str], default: str) -> str:
    return value if value in choices else default


def sanitize_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    return value.strip("-")


def ros_safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "robot"


def is_name(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_.-]+$", value))


def is_host(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_.:-]+$", value))


def is_uint(value: str) -> bool:
    return value.isdigit()


def is_positive_uint(value: str) -> bool:
    return value.isdigit() and int(value) > 0


def is_turn_secret(value: str) -> bool:
    return value != "change-me" and bool(re.match(r"^[A-Za-z0-9_.~-]{8,128}$", value))


def is_topic(value: str) -> bool:
    return bool(re.match(r"^/[A-Za-z0-9_/]+$", value))


def is_namespace(value: str) -> bool:
    return value == "" or is_topic(value)


def is_existing_file(value: str) -> bool:
    return Path(os.path.expanduser(value)).is_file()


def generate_turn_password() -> str:
    return secrets.token_urlsafe(24).rstrip("=")


def normalize_turn_min_port(value: str) -> str:
    return "49152" if value in {"", "49160"} else value


def normalize_turn_max_port(value: str) -> str:
    return "65535" if value in {"", "49200"} else value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure HORUS Connector .env")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env", default=None)
    parser.add_argument("--template", default=None)
    parser.add_argument("--role", choices=list(ROLE_HELP))
    parser.add_argument("--topology", choices=list(TOPOLOGY_HELP))
    parser.add_argument("--room")
    parser.add_argument("--cloud-ip")
    parser.add_argument("--machine-ip")
    parser.add_argument("--signal-ip")
    parser.add_argument("--namespace")
    parser.add_argument("--zenoh-transport", choices=list(ZENOH_TRANSPORT_HELP))
    parser.add_argument("--enable-quic", action="store_true")
    parser.add_argument("--ros-distro")
    parser.add_argument("--ros-domain-id")
    parser.add_argument("--ros-setup-path")
    parser.add_argument("--camera-topic")
    parser.add_argument("--camera-output-topic")
    parser.add_argument("--stream-count")
    parser.add_argument("--teammate-name")
    parser.add_argument("--hololens-host")
    parser.add_argument("--teammate-video-profile", choices=list(TEAMMATE_VIDEO_PROFILES))
    parser.add_argument("--teammate-raw-image", action="store_true")
    parser.add_argument("--video-preset", choices=list(VIDEO_PRESETS) + ["custom"])
    parser.add_argument("--width")
    parser.add_argument("--height")
    parser.add_argument("--fps")
    parser.add_argument("--bitrate")
    parser.add_argument("--encoder-preference", choices=["stable", "hardware", "software"])
    parser.add_argument("--turn", action="store_true")
    parser.add_argument("--turn-user")
    parser.add_argument("--turn-password")
    parser.add_argument("--turn-port", default=None)
    parser.add_argument("--turn-min-port", default=None)
    parser.add_argument("--turn-max-port", default=None)
    parser.add_argument("--turn-realm", default=None)
    parser.add_argument("--yes", action="store_true", help="Accept defaults and provided flags")
    parser.add_argument("--force", action="store_true", help="Overwrite without asking")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.env is None:
        args.env = str(root / ".env")
    if args.template is None:
        args.template = str(root / ".env.example")
    return args


def main() -> None:
    args = parse_args()
    try:
        Wizard(args).run()
    except KeyboardInterrupt:
        print("\nSetup cancelled. No changes were written.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
