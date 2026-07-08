import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def codes(findings):
    return {finding.code for finding in findings}


def test_dds_preflight_classifies_domain_mismatch():
    import dds_preflight

    findings = dds_preflight.classify_environment(
        {
            "HORUS_TERMINAL_ROS_DOMAIN_ID": "90",
            "ROS_DOMAIN_ID": "0",
            "ROS_DISCOVERY_SERVER": "",
        },
        "robot",
        "binary",
        "host",
    )

    assert "DDS_DOMAIN_MISMATCH" in codes(findings)


def test_dds_preflight_classifies_discovery_server():
    import dds_preflight

    findings = dds_preflight.classify_environment(
        {
            "HORUS_TERMINAL_ROS_DOMAIN_ID": "",
            "ROS_DOMAIN_ID": "90",
            "ROS_DISCOVERY_SERVER": "127.0.0.1:11811",
        },
        "robot",
        "binary",
        "host",
    )

    assert "DISCOVERY_SERVER_UNSUPPORTED" in codes(findings)


def test_dds_preflight_classifies_docker_non_host_network():
    import dds_preflight

    findings = dds_preflight.classify_environment(
        {
            "HORUS_TERMINAL_ROS_DOMAIN_ID": "",
            "ROS_DOMAIN_ID": "90",
            "ROS_DISCOVERY_SERVER": "",
        },
        "robot",
        "docker",
        "bridge",
    )

    assert "DOCKER_NETWORK_NOT_HOST" in codes(findings)


def test_dds_preflight_warns_when_only_infrastructure_topics_are_visible():
    import dds_preflight

    findings = dds_preflight.classify_topics(
        "robot",
        ["/parameter_events", "/rosout"],
        {"publishers": ["^/.*$"]},
        "config/zenoh_robot.json5",
    )

    assert "ONLY_INFRASTRUCTURE_TOPICS" in codes(findings)


def test_dds_preflight_classifies_horus_topic_filter():
    import dds_preflight

    findings = dds_preflight.classify_topics(
        "robot",
        ["/zed_front/zed_node/rgb/image_rect_color/compressed", "/tf"],
        {"publishers": ["^/tf$"]},
        "config/zenoh_robot.json5",
    )

    assert "HORUS_FILTERED_TOPIC" in codes(findings)
    assert any(finding.severity == "warning" for finding in findings)


def test_dds_preflight_errors_when_all_substantive_topics_are_filtered():
    import dds_preflight

    findings = dds_preflight.classify_topics(
        "robot",
        ["/zed_front/zed_node/rgb/image_rect_color/compressed"],
        {"publishers": ["^/tf$"]},
        "config/zenoh_robot.json5",
    )

    filtered = [finding for finding in findings if finding.code == "HORUS_FILTERED_TOPIC"]
    assert filtered
    assert filtered[0].severity == "error"


def test_dds_preflight_does_not_filter_machine_local_topics():
    import dds_preflight

    findings = dds_preflight.classify_topics(
        "machine",
        ["/robot_a/camera/webrtc/image_raw"],
        {"publishers": ["^/cmd_vel$"]},
        "config/zenoh_machine.json5",
    )

    assert "HORUS_FILTERED_TOPIC" not in codes(findings)


def test_dds_preflight_extracts_zenoh_allow_patterns():
    import dds_preflight

    patterns = dds_preflight.load_allow_patterns(ROOT / "config" / "zenoh_teammate.json5")

    assert "^/tf$" in patterns["publishers"]
    assert "^/[^/]+/guidance/request$" in patterns["subscribers"]


def test_dds_preflight_parses_bridge_log_routes(tmp_path):
    import dds_preflight

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "zenoh.log").write_text(
        "\n".join(
            [
                "Discovered ROS Node /camera_node",
                "Route Publisher (ROS:/tf -> Zenoh:tf) created",
                "Route Subscriber (ROS:/cmd_vel -> Zenoh:cmd_vel) created",
            ]
        ),
        encoding="utf-8",
    )

    bridge_log = dds_preflight.parse_bridge_log(run_dir)

    assert bridge_log["nodes"] == ["/camera_node"]
    assert bridge_log["publisher_routes"] == ["/tf"]
    assert bridge_log["subscriber_routes"] == ["/cmd_vel"]


def test_dds_preflight_classifies_missing_bridge_route(tmp_path):
    import dds_preflight

    bridge_log = {
        "available": True,
        "path": str(tmp_path / "zenoh.log"),
        "publisher_routes": ["/tf"],
        "subscriber_routes": [],
        "nodes": [],
    }

    findings = dds_preflight.classify_bridge_log("robot", ["/tf", "/scan"], bridge_log)

    assert "BRIDGE_ROUTE_MISSING" in codes(findings)
