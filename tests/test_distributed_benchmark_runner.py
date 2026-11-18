import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_lan_requires_explicit_site_addresses():
    import run_distributed_benchmark as runner

    assert runner.PATHS["lan"]["sender"] == "arancina"
    assert runner.PATHS["lan"]["receiver"] == "poke"
    assert runner.PATHS["lan"]["target"] == runner.NODES["poke"]["ip"]


def test_zenoh_benchmark_configs_include_camera_and_control_priorities():
    import run_distributed_benchmark as runner

    assert "^/benchmark/camera$" in runner.ZENOH_PUB_CONFIG
    assert "^/benchmark/cmd_vel$" in runner.ZENOH_PUB_CONFIG
    assert "^/benchmark/cmd_vel_ack$" in runner.ZENOH_PUB_CONFIG
    assert ".*/benchmark/cmd_vel$=1:express" in runner.ZENOH_SUB_CONFIG
    assert ".*/benchmark/cmd_vel_ack$=2:express" in runner.ZENOH_PUB_CONFIG


def test_network_profiles_are_explicit_and_name_safe():
    import run_distributed_benchmark as runner

    assert runner.NETWORK_PROFILES["bw40"]["bandwidth_mbps"] == 40.0
    assert runner.NETWORK_PROFILES["bw2"]["bandwidth_mbps"] == 2.0
    assert runner.NETWORK_PROFILES["loss3"]["loss_percent"] == 3.0
    assert runner.network_profile_suffix("unconstrained") == ""
    assert runner.network_profile_suffix("bw20") == "_bw20"
    assert runner.profile_needs_shaping("unconstrained") is False
    assert runner.profile_needs_shaping("loss1") is True
