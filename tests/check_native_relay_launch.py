"""Exercise both managed native launchers without hardware or production ports."""

import argparse
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
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="horus-native-launch-") as temporary:
        directory = Path(temporary)
        streams = directory / "streams.json"
        streams.write_text('{"version":2,"streams":[]}')
        config = directory / "relay.json"
        config.write_text(json.dumps({"streams": [{"id": "fixture", "input_topic": "/native_check/rgb",
            "output_topic": "/native_check/h264", "bitrate_kbps": 1500, "framerate": 30}]}))
        bridge = directory / "bridge.yaml"
        bridge.write_text('/**:\n  ros__parameters:\n    tcp_ip: "127.0.0.1"\n    tcp_port: 13990\n'
                          '    horuslink_bulk_port: 13991\n    transport_protocol: "horuslink"\n')
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
                env = dict(os.environ, HORUS_ENV=str(settings), HORUS_RUN_DIR=str(directory / role),
                    HORUS_WEBRTC_PYTHON=os.sys.executable)
                environments.append(env)
                result = subprocess.run([str(ROOT / "horus"), "launch", role, "--no-monitor"], env=env,
                    capture_output=True, text=True, timeout=45)
                assert result.returncode == 0, result.stdout + result.stderr
                name = "source-media" if role == "robot" else "media-relay"
                health = json.loads((directory / role / f"{name}.media.json").read_text())
                assert health["engine"].startswith("native_"), health
                print(role, health["engine"], "initialized; no frame-delivery claim")
        finally:
            for env in reversed(environments):
                result = subprocess.run([str(ROOT / "horus"), "stop"], env=env,
                    capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    main()
