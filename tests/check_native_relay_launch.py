"""Exercise both managed native launchers without hardware or production ports."""

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-binary", type=Path, required=True)
    parser.add_argument("--relay-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Retain logs and receipts in a new directory")
    parser.add_argument("--shared-budget", action="store_true", help="Also initialize the shared egress governor")
    parser.add_argument("--camera-domain", action="store_true", help="Give the native camera subscriber its own ROS domain")
    parser.add_argument("--control-ice", action="store_true", help="Load a private native control ICE profile")
    parser.add_argument("--bulk-limit-kbps", type=int, default=0, help="Verify a new governor's opt-in bulk ceiling")
    args = parser.parse_args()
    if args.bulk_limit_kbps and not (args.shared_budget and 256 <= args.bulk_limit_kbps < 3488):
        parser.error("bulk-limit-kbps needs --shared-budget and a limit in 256..3487")
    if args.output:
        args.output = args.output.resolve()
        args.output.mkdir(parents=True)
    with (nullcontext(args.output) if args.output
          else tempfile.TemporaryDirectory(prefix="horus-native-launch-")) as temporary:
        directory = Path(temporary)
        streams = directory / "streams.json"
        streams.write_text('{"version":2,"streams":[]}')
        config = directory / "relay.json"
        config.write_text(json.dumps({"streams": [{"id": "fixture", "input_topic": "/native_check/rgb",
            "output_topic": "/native_check/h264", "bitrate_kbps": 1500, "framerate": 30}]}))
        bridge = directory / "bridge.yaml"
        bridge.write_text('/**:\n  ros__parameters:\n    tcp_ip: "127.0.0.1"\n    tcp_port: 13990\n'
                          '    horuslink_bulk_port: 13991\n    transport_protocol: "horuslink"\n')
        budget_file = directory / "bulk # budget.txt"
        ice_file = directory / "control ICE # profile.json"
        if args.control_ice:
            ice_file.write_text('{"ice_servers":[],"ice_transport_policy":"all"}')
        environments = []
        try:
            for role in ("robot", "machine"):
                settings = directory / f"{role}.env"
                settings.write_text(f'HORUS_ROLE={role}\nHORUS_TOPOLOGY=direct\nHORUS_MACHINE_IP=127.0.0.1\n'
                    f'ROS_DISTRO=jazzy\nROS_DOMAIN_ID=175\nROS_LOCALHOST_ONLY=1\n'
                    f'HORUS_ZENOH_ENABLED=0\nHORUS_METRICS_PORT=0\nHORUS_STREAMS_CONFIG="{streams}"\n' +
                    (f'HORUS_SOURCE_MEDIA_ENABLED=1\nHORUS_SOURCE_MEDIA_BINARY="{args.source_binary.resolve()}"\n'
                     'HORUS_SOURCE_MEDIA_ENCODER=x264enc\nHORUS_SOURCE_CONTROL_MODE=authority\n'
                     if role == "robot" else
                     f'HORUS_MEDIA_RELAY_CONFIG="{config}"\nHORUS_MEDIA_RELAY_ENGINE=native\n'
                     f'HORUS_MEDIA_RELAY_BINARY="{args.relay_binary.resolve()}"\n'
                     f'HORUS_MEDIA_RELAY_BRIDGE_CONFIG="{bridge}"\nHORUS_OPERATOR_CONTROL_MODE=proxy\n'))
                if role == "robot" and args.shared_budget:
                    with settings.open("a") as stream:
                        stream.write(f'HORUS_LINK_BUDGET_KBPS=4000\nHORUS_ZENOH_BULK_BUDGET_FILE="{budget_file}"\n')
                        if args.bulk_limit_kbps:
                            stream.write(f'HORUS_LINK_BULK_LIMIT_KBPS={args.bulk_limit_kbps}\n')
                if role == "robot" and args.camera_domain:
                    with settings.open("a") as stream:
                        stream.write('HORUS_SOURCE_MEDIA_CAMERA_DOMAIN_ID=176\n')
                if args.control_ice:
                    with settings.open("a") as stream:
                        stream.write(f'HORUS_CONTROL_ICE_CONFIG="{ice_file}"\n')
                env = dict(os.environ, HORUS_ENV=str(settings), HORUS_RUN_DIR=str(directory / role),
                    HORUS_WEBRTC_PYTHON=os.sys.executable)
                environments.append(env)
                result = subprocess.run([str(ROOT / "horus"), "launch", role, "--no-monitor"], env=env,
                    capture_output=True, text=True, timeout=45)
                assert result.returncode == 0, result.stdout + result.stderr
                name = "source-media" if role == "robot" else "media-relay"
                health = json.loads((directory / role / f"{name}.media.json").read_text())
                assert health["engine"].startswith("native_"), health
                if role == "robot" and args.shared_budget:
                    assert int(budget_file.read_text()) > 0
                    if args.bulk_limit_kbps:
                        assert int(budget_file.read_text()) == args.bulk_limit_kbps * 125
                        assert health["bulk_limit_kbps"] == args.bulk_limit_kbps
                if role == "robot" and args.camera_domain:
                    assert health["camera_domain_id"] == 176
                print(role, health["engine"], "initialized; no frame-delivery claim")
        finally:
            for env in reversed(environments):
                result = subprocess.run([str(ROOT / "horus"), "stop"], env=env,
                    capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    main()
