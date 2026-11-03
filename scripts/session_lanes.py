"""Render independent real-time, sensing and bulk Zenoh sessions."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

import json5

from link_profile import endpoint

CLASSES = ("realtime", "sensing", "bulk")


def parse_dscp(value):
    try:
        codes = tuple(int(item) for item in value.split(","))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("DSCP requires realtime,sensing,bulk integers") from error
    if len(codes) != len(CLASSES) or any(not 0 <= code <= 63 for code in codes):
        raise argparse.ArgumentTypeError("DSCP requires three integers in 0..63")
    return dict(zip(CLASSES, codes))


def configure(base, *, target="", bind="0.0.0.0", port=7447, send_buffer=65536, dscp=None):
    if not 1 <= port <= 65533:
        raise ValueError("Three independent sessions require base port 1..65533")
    if dscp is not None and set(dscp) != set(CLASSES):
        raise ValueError("DSCP requires all three traffic classes")
    result = {}
    for offset, name in enumerate(CLASSES):
        config = copy.deepcopy(base)
        plugin = config.setdefault("plugins", {}).setdefault("ros2dds", {})
        plugin["traffic_class"] = name
        plugin["nodename"] = "zenoh_bridge_"+name
        config.pop("id", None)
        transport = config.setdefault("transport", {})
        unicast = transport.setdefault("unicast", {})
        if unicast.get("lowlatency"):
            raise ValueError("Session lanes require normal Zenoh QoS")
        unicast.update(max_links=1, qos=dict(enabled=True), compression=dict(enabled=True))
        transport.setdefault("link", {}).setdefault("tx", {}).setdefault("queue", {})["batching"] = dict(enabled=True, time_limit=1)
        config.setdefault("scouting", {})["multicast"] = dict(enabled=False)
        config["scouting"]["gossip"] = dict(enabled=False)
        code = dscp[name] if dscp is not None else None
        config["listen"] = dict(endpoints=[] if target else [endpoint(bind, port+offset, send_buffer, dscp=code)])
        config.setdefault("connect", {})["endpoints"] = [endpoint(target, port+offset, send_buffer, dscp=code)] if target else []
        result[name] = config
    return result


def check_binary(binary):
    receipt = json.loads(Path(str(binary)+".source.json").read_text())
    with Path(binary).open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    if receipt.get("binary_sha256") != sha or "traffic_class_sessions" not in receipt.get("features", []):
        raise ValueError("Session isolation requires the verified HORUS priority bridge build")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", default="")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7447)
    parser.add_argument("--send-buffer", type=int, default=65536)
    parser.add_argument("--dscp", type=parse_dscp, help="Optional realtime,sensing,bulk codepoints, e.g. 46,18,8")
    args = parser.parse_args()
    check_binary(args.binary)
    profiles = configure(json5.loads(args.base.read_text()), target=args.target, bind=args.bind,
                         port=args.port, send_buffer=args.send_buffer, dscp=args.dscp)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, config in profiles.items():
        (args.output / (name+".json")).write_text(json.dumps(config, indent=2)+"\n")


if __name__ == "__main__":
    main()
