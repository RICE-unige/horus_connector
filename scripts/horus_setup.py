#!/usr/bin/env python3
"""Interactive HORUS Connector environment setup."""

from __future__ import annotations

import argparse
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
    "cloud": "Shared hub. Runs Zenoh routing and WebRTC signaling only.",
}

TOPOLOGY_HELP = {
    "hub": "Use one cloud hub. Best when machines are on different networks.",
    "direct": "Use VPN, Tailscale, or LAN. No cloud is needed.",
}

VIDEO_PRESETS = {
    "standard": (1280, 720, 30, 6000),
    "light": (960, 540, 30, 1600),
    "high": (1920, 1080, 30, 8000),
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
            self.section(3, total, "Robot identity")
            room = self.prompt_text(
                "Room / robot name",
                default_room,
                required=True,
                validator=is_name,
                hint="Use one stable name per robot, for example robot-a or mobile-base-01.",
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
                hint="Robots and machines use this to reach the hub.",
            )
            machine_ip = ""
        else:
            cloud_ip = self.args.cloud_ip or ""
            if role == "robot":
                machine_ip = self.prompt_text(
                    "Operator machine VPN/LAN IP or DNS name",
                    machine_default,
                    required=True,
                    validator=is_host,
                    hint="You selected robot. In direct mode, the robot connects to the operator machine at this address.",
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
        print("  Robots use /robot_name. Machines and cloud usually stay empty.\n")

        self.section(6, total, "Camera and runtime")
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
        else:
            camera_topic = self.existing.get("WEBRTC_ROS_IMAGE_INPUT_TOPIC", "/camera/image_raw")
            output_topic = self.existing.get("WEBRTC_ROS_IMAGE_OUTPUT_TOPIC", "/camera/webrtc/image_raw")
            width = int(self.existing.get("WEBRTC_VIDEO_WIDTH", "1280"))
            height = int(self.existing.get("WEBRTC_VIDEO_HEIGHT", "720"))
            fps = int(self.existing.get("WEBRTC_VIDEO_FPS", "30"))
            bitrate = int(self.existing.get("VIDEO_BITRATE_KBIT", "6000"))
            encoder_preference = "stable"
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
            "VIDEO_BITRATE_KBIT": str(bitrate),
            "WEBRTC_MIN_BITRATE_KBIT": "1000",
            "WEBRTC_MAX_BITRATE_KBIT": str(bitrate),
            "WEBRTC_ENCODER_PREFERENCE": encoder_preference,
            "WEBRTC_DECODER_PREFERENCE": encoder_preference,
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
                print("  ROS 2 was not found under /opt/ros. Install ROS 2 before bootstrap/launch on robot and machine roles.")
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
        if role == "cloud":
            turn_status = "enabled" if self.values["HORUS_CLOUD_RUN_TURN"] == "1" else "disabled"
            print(f"  turn       {turn_status}")
        elif "turn:" in self.values.get("WEBRTC_ICE_SERVERS", ""):
            print("  turn       fallback configured")
        if role != "cloud":
            print(
                "  camera     "
                f"{self.values['WEBRTC_VIDEO_WIDTH']}x{self.values['WEBRTC_VIDEO_HEIGHT']}"
                f"@{self.values['WEBRTC_VIDEO_FPS']} -> {self.values['WEBRTC_ROS_IMAGE_OUTPUT_TOPIC']}"
            )
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
    parser.add_argument("--ros-distro")
    parser.add_argument("--ros-domain-id")
    parser.add_argument("--ros-setup-path")
    parser.add_argument("--camera-topic")
    parser.add_argument("--camera-output-topic")
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
