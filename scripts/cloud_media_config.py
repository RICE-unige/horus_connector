#!/usr/bin/env python3
"""Validate the cloud media relay's signaling-only DDS routes."""

import argparse
from pathlib import Path
import json5


FLEET = "/horus/remote/webrtc/"
OPERATORS = "/horus/cloud/webrtc/"


def validate(config, domain):
    plugin = config["plugins"]["ros2dds"]
    if type(domain) is not int or not 1 <= domain <= 232 or plugin["domain"] != domain:
        raise ValueError("Cloud signaling client and media node require the same dedicated DDS domain (1..232)")
    allow = plugin["allow"]
    expected = {
        "publishers": {f"^{FLEET}client_signal$", f"^{OPERATORS}server_signal$"},
        "subscribers": {f"^{FLEET}server_signal$", f"^{OPERATORS}client_signal$"},
    }
    if any(set(allow.get(role, [])) != topics for role, topics in expected.items()) or any(
        routes for role, routes in allow.items() if role not in expected
    ) or "deny" in plugin:
        raise ValueError("Cloud media DDS may route only the four native signaling topics")
    connection = config["connect"]["endpoints"]
    tls = config["transport"]["link"]["tls"]
    if (config.get("listen", {}).get("endpoints") != [] or len(connection) != 1
            or not connection[0].startswith("tls/") or tls.get("enable_mtls") is not True
            or tls.get("verify_name_on_connect", True) is not True):
        raise ValueError("Cloud media requires a single mTLS client connection and no listener")
    for key in ("root_ca_certificate", "connect_private_key", "connect_certificate"):
        path = Path(tls[key])
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"Cloud media mTLS requires a valid absolute {key} path")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("domain", type=int)
    args = parser.parse_args()
    try:
        validate(json5.loads(args.config.read_text()), args.domain)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Invalid cloud media profile: {error}\n")


if __name__ == "__main__":
    main()
