#!/usr/bin/env python3
"""Generate per-endpoint credentials for a HORUS WebRTC room."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from webrtc_session import generate_client_token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", required=True)
    parser.add_argument("--robot-id", default="")
    parser.add_argument("--machine-id", default="")
    parser.add_argument("--voice", action="store_true", help="generate operator/field-teammate voice credentials")
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--field-teammate-id", default="")
    parser.add_argument("--output", default="config/webrtc_credentials.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    room = args.room.strip()
    output = Path(args.output).expanduser()
    if output.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {output}; pass --force to rotate credentials")

    if args.voice:
        first_id = args.operator_id.strip() or f"operator:{room}"
        second_id = args.field_teammate_id.strip() or f"field_teammate:{room}"
        first_role = "operator"
        second_role = "field_teammate"
        first_label = "Operator endpoint"
        second_label = "Field-teammate endpoint"
        capabilities = ["communicative"]
    else:
        first_id = args.robot_id.strip() or f"robot:{room}"
        second_id = args.machine_id.strip() or f"machine:{room}"
        first_role = "robot"
        second_role = "machine"
        first_label = "Robot endpoint"
        second_label = "Machine endpoint"
        capabilities = []

    first_token = generate_client_token()
    second_token = generate_client_token()
    payload = {
        "version": 1,
        "clients": {
            first_id: {
                "token": first_token,
                "roles": [first_role],
                "rooms": [room],
                **({"capabilities": capabilities} if capabilities else {}),
            },
            second_id: {
                "token": second_token,
                "roles": [second_role],
                "rooms": [room],
                **({"capabilities": capabilities} if capabilities else {}),
            },
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"Relay credentials: {output}")
    print(f"{first_label}:")
    print(f"  HORUS_WEBRTC_CLIENT_ID={first_id}")
    print(f"  HORUS_WEBRTC_CLIENT_TOKEN={first_token}")
    print(f"{second_label}:")
    print(f"  HORUS_WEBRTC_CLIENT_ID={second_id}")
    print(f"  HORUS_WEBRTC_CLIENT_TOKEN={second_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
