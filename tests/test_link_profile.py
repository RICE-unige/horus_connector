import copy
import re
import sys
from pathlib import Path

import json5
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from link_profile import configure, endpoint, PRIORITY_RANGES


def test_native_client_builds_three_disjoint_links_without_mutating_routes():
    base = {"plugins": {"ros2dds": {"allow": {"publishers": ["custom"]}}}}
    original = copy.deepcopy(base)
    result = configure(base, target="100.98.13.9")
    assert base == original
    assert result["plugins"] == original["plugins"]
    group, = result["connect"]["endpoints"]
    assert group["strategy"] == "allOf"
    assert result["listen"]["endpoints"] == []
    assert result["scouting"]["gossip"] == {"enabled": True, "autoconnect": [], "multihop": False}
    assert len(group["locators"]) == 3
    assert [re.search(r"prio=([^#]+)", value).group(1) for value in group["locators"]] == list(PRIORITY_RANGES)
    assert result["transport"]["unicast"]["max_links"] == 3
    assert result["transport"]["unicast"]["compression"]["enabled"]
    assert result["transport"]["link"]["tx"]["queue"]["batching"]["time_limit"] == 1


def test_listener_does_not_invent_a_client_or_remove_other_config():
    result = configure({"metadata": {"site": "test"}})
    assert "connect" not in result and result["metadata"] == {"site": "test"}


@pytest.mark.parametrize("base", [{"transport": {"unicast": {"lowlatency": True}}},
                                 {"transport": {"unicast": {"qos": {"enabled": False}}}}])
def test_incompatible_transport_fails_instead_of_silently_removing_priorities(base):
    with pytest.raises(ValueError):
        configure(base)


@pytest.mark.parametrize("host,port,buffer", [("host/escape", 7447, 65536),
                                            ("host", 0, 65536), ("host", 7447, 1)])
def test_invalid_endpoint_is_rejected(host, port, buffer):
    with pytest.raises(ValueError):
        endpoint(host, port, buffer)


def test_ipv6_endpoint_is_bracketed():
    assert endpoint("::1", 7447, 65536, "0-1") == "tcp/[::1]:7447?prio=0-1#so_sndbuf=65536"


@pytest.mark.parametrize("role", ["robot", "machine"])
def test_transform_and_repair_traffic_precede_bulk_patterns(role):
    from pathlib import Path
    config = json5.loads((Path(__file__).parents[1] / f"config/zenoh_{role}.json5").read_text())
    rules = config["plugins"]["ros2dds"]["pub_priorities"]
    for topic in ("/tf", "/fleet/tf_static", "/horus/map/mesh/indexed_request",
                  "/horus/video/webrtc/client_signal"):
        priority = next(rule.rsplit("=", 1)[1] for rule in rules
                        if re.search(rule.rsplit("=", 1)[0], topic))
        assert priority == "1:express", (topic, priority)
