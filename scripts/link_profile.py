"""Native Zenoh priority links: keep bulk TCP loss/queues off real-time traffic."""

import argparse
import copy
import json
from pathlib import Path

import json5

PRIORITY_RANGES = ("0-1", "2-5", "6-7")


def endpoint(host, port, send_buffer, priorities=None, dscp=None):
    if not host or any(char in host for char in "?#/; \t\r\n"):
        raise ValueError("invalid Zenoh endpoint host")
    if not 1 <= port <= 65535 or not 8192 <= send_buffer <= 4 * 1024 * 1024:
        raise ValueError("invalid Zenoh port or TCP send buffer")
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    metadata = f"?prio={priorities}" if priorities else ""
    options = f"so_sndbuf={send_buffer}"
    if dscp is not None:
        if type(dscp) is not int or not 0 <= dscp <= 63:
            raise ValueError("DSCP must be a six-bit integer (0..63)")
        # Zenoh 1.10's endpoint option accepts the full IP TOS/TCLASS byte.
        options += f";dscp={dscp << 2}"
    return f"tcp/{address}:{port}{metadata}#{options}"


def configure(base, *, target="", port=7447, send_buffer=65536):
    result = copy.deepcopy(base)
    transport = result.setdefault("transport", {})
    unicast = transport.setdefault("unicast", {})
    if unicast.get("lowlatency") or unicast.get("qos", {}).get("enabled") is False:
        raise ValueError("priority links require Zenoh QoS; lowlatency transport is incompatible")
    unicast["qos"] = {**unicast.get("qos", {}), "enabled": True}
    unicast["max_links"] = max(3, unicast.get("max_links", 1))
    unicast["compression"] = {"enabled": True}
    queue = transport.setdefault("link", {}).setdefault("tx", {}).setdefault("queue", {})
    # Zenoh uses milliseconds here, unlike wait_before_drop (microseconds).
    queue["batching"] = {"enabled": True, "time_limit": 1}
    # Peer routing needs topology bookkeeping, including gateway de-duplication.
    # Exchange it only on explicit links; never auto-connect discovered peers.
    gossip = result.setdefault("scouting", {}).setdefault("gossip", {})
    gossip.update(enabled=True, autoconnect=[], multihop=False)
    if target:
        # Zenoh 1.10 client recovery reconnects only one link. An explicit peer
        # retains every priority link; no listener or discovery is introduced.
        result.setdefault("listen", {}).setdefault("endpoints", [])
        result.setdefault("connect", {})["endpoints"] = [{
            "strategy": "allOf",
            "locators": [endpoint(target, port, send_buffer, value) for value in PRIORITY_RANGES],
        }]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target", default="")
    parser.add_argument("--port", type=int, default=7447)
    parser.add_argument("--send-buffer", type=int, default=65536)
    args = parser.parse_args()
    endpoint(args.target or "0.0.0.0", args.port, args.send_buffer)
    result = configure(json5.loads(args.base.read_text()), target=args.target,
                       port=args.port, send_buffer=args.send_buffer)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
