"""Structured, explicit additions to Zenoh routing without editing source profiles."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import json5


def excluding_topics(topics):
    trie = {}
    for topic in topics:
        node = trie
        for char in topic:
            node = node.setdefault(char, {})
        node[None] = {}

    def expression(node):
        children = [char for char in node if char is not None]
        alternatives = [] if None in node else [""]
        alternatives.append("[^" + re.escape("".join(children)) + "].*" if children else ".+")
        alternatives.extend(re.escape(char) + expression(node[char]) for char in children)
        return "(?:" + "|".join(alternatives) + ")"

    return "^" + expression(trie) + "$"


def registration_routes(registrations, role):
    if role not in {"robot", "machine"}:
        raise ValueError("registration routing applies to robot or machine roles")
    result = {"publishers": [], "subscribers": []}
    for registration in registrations:
        control = registration.get("control", {})
        commands = [control.get("drive_topic")]
        teleop = control.get("teleop", {})
        commands.extend(teleop.get(key) for key in ("command_topic", "raw_input_topic", "head_pose_topic"))
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"command_topic", "goal_topic", "cancel_topic", "request_topic", "path_topic"} and isinstance(child, str):
                        commands.append(child)
                    elif isinstance(child, (list, dict)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(registration.get("tasks", {}))
        collect(control.get("tasks", {}))
        category = "publishers" if role == "machine" else "subscribers"
        result[category].extend(topic for topic in commands if topic)
    return result


def resolve(base, additions, streams=(), role="machine"):
    result = json5.loads(Path(base).read_text())
    plugin = result.setdefault("plugins", {}).setdefault("ros2dds", {})
    allowed = plugin.setdefault("allow", {})
    if "registrations" in additions:
        derived = registration_routes(additions["registrations"], role)
        priorities = {topic: "1:express" for topics in derived.values() for topic in topics}
        additions = {**additions, "priorities": {**priorities, **additions.get("priorities", {})},
                     **{key: [*additions.get(key, []), *value] for key, value in derived.items()}}
    for category in ("publishers", "subscribers", "service_servers", "service_clients", "action_servers", "action_clients"):
        topics = additions.get(category, [])
        if not isinstance(topics, list) or any(not isinstance(topic, str) or not re.fullmatch(r"/[A-Za-z0-9_/]+", topic) for topic in topics):
            raise ValueError(f"{category} must be a list of absolute ROS interface names")
        for topic in topics:
            pattern = "^" + re.escape(topic) + "$"
            if pattern not in allowed.setdefault(category, []):
                allowed[category].append(pattern)
    rates = additions.get("max_frequencies", {})
    if not isinstance(rates, dict):
        raise ValueError("max_frequencies must be an object")
    for topic, rate in rates.items():
        if topic.endswith(("/tf", "/tf_static")):
            raise ValueError("TF messages cannot be safely throttled as whole messages")
        if not re.fullmatch(r"/[A-Za-z0-9_/]+", topic) or type(rate) not in (int, float) or not 0 < rate <= 10000:
            raise ValueError("invalid explicit topic frequency")
        plugin.setdefault("pub_max_frequencies", []).insert(0, f"^{re.escape(topic)}$={rate}")
    priorities = additions.get("priorities", {})
    if not isinstance(priorities, dict):
        raise ValueError("priorities must map absolute ROS topic names to priorities")
    for topic, priority in priorities.items():
        if not re.fullmatch(r"/[A-Za-z0-9_/]+", topic) or not re.fullmatch(r"[1-7](?::express)?", str(priority)):
            raise ValueError("invalid explicit topic priority")
        plugin.setdefault("pub_priorities", []).insert(0, f"^{re.escape(topic)}$={priority}")
    # Suppression is explicit per camera; default routing remains compatible.
    excluded = [s["input_topic"] for s in streams if s.get("transport") == "webrtc_only"]
    direct_topics = additions.get("webrtc_only_topics", [])
    if not isinstance(direct_topics, list) or any(
            not isinstance(topic, str) or not re.fullmatch(r"/[A-Za-z0-9_/]+", topic)
            for topic in direct_topics):
        raise ValueError("webrtc_only_topics must list absolute camera topic names")
    excluded.extend(direct_topics)
    if excluded and role == "robot":
        patterns = allowed.get("publishers", [])
        if patterns != ["^/.*$"]:
            raise ValueError("webrtc_only automatic routing requires the standard robot publish profile; use explicit topic routing for custom profiles")
        allowed["publishers"] = [excluding_topics(excluded)]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--routes", default="")
    parser.add_argument("--streams", default="")
    parser.add_argument("--role", choices=("robot", "machine"), default="machine")
    args = parser.parse_args()
    additions = json.loads(Path(args.routes).read_text()) if args.routes else {}
    streams = json.loads(args.streams) if args.streams else []
    if isinstance(streams, dict):
        streams = streams["streams"]
    payload = resolve(args.base, additions, streams, args.role)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output.parent, prefix=output.name + ".")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
