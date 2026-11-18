import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from session_lanes import check_binary, configure, parse_dscp


@pytest.mark.parametrize("base,expected", [("27447", [27447, 27448, 27449]),
                                           ("07447", [7447, 7448, 7449])])
def test_doctor_probes_every_configured_tcp_session(base, expected):
    script = (ROOT / "horus").read_text()
    functions = "\n".join(re.search(rf"^{name}\(\) \{{.*?^\}}", script, re.M | re.S).group()
                          for name in ("zenoh_session_ports", "doctor_zenoh_target"))
    env = dict(os.environ, HORUS_ZENOH_SESSION_ISOLATION="1", ZENOH_PORT=base)
    result = subprocess.run(["bash", "-eu", "-c", functions +
        '\ntcp_probe() { printf "%s:%s\\n" "$1" "$2"; return 1; }\ndoctor_zenoh_target 127.0.0.1'],
        env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert [int(line.rsplit(":", 1)[1]) for line in result.stdout.splitlines()] == expected


def test_status_lists_all_isolated_session_ports_and_processes(tmp_path):
    config = tmp_path / "horus.env"
    config.write_text("HORUS_ROLE=cloud\nHORUS_TOPOLOGY=hub\nHORUS_ZENOH_SESSION_ISOLATION=1\n"
                      "HORUS_CLOUD_IP=127.0.0.1\nZENOH_TRANSPORT=tcp\nZENOH_PORT=27447\n")
    env = dict(os.environ, HORUS_ENV=str(config), HORUS_RUN_DIR=str(tmp_path / "run"))
    result = subprocess.run([str(ROOT / "horus"), "status"], env=env, cwd=ROOT, timeout=15,
                            capture_output=True, text=True, check=True)
    assert all(f":{port}" in result.stdout for port in (27447, 27448, 27449))
    assert all(name in result.stdout for name in ("zenoh-sensing", "zenoh-bulk"))


@pytest.mark.parametrize("topology,target,expected", [
    ("direct", "100.1.2.3", "peer"),
    ("hub", "203.0.113.2", "client"),
    ("hub", "", "router"),
    ("direct", "", "router"),
    (None, "100.1.2.3", "client"),
])
def test_session_mode_matches_explicit_hub_without_gossip(topology, target, expected):
    script = (ROOT / "horus").read_text()
    function = re.search(r"^zenoh_session_mode\(\) \{.*?^\}", script, re.M | re.S)
    assert function is not None
    env = dict(os.environ)
    env.pop("HORUS_TOPOLOGY", None)
    if topology is not None:
        env["HORUS_TOPOLOGY"] = topology
    result = subprocess.run(["bash", "-eu", "-c", function.group()+
        '\nzenoh_session_mode "$1"', "test", target], env=env,
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected


def test_single_session_priority_reconnect_keeps_existing_peer_mode():
    script = (ROOT / "horus").read_text()
    function = re.search(r"^launch_zenoh_client\(\) \{.*?^\}", script, re.M | re.S)
    assert function is not None
    assert "local transport_mode=client" in function.group()
    assert "zenoh_priority_links && transport_mode=peer" in function.group()
    assert "$(zenoh_session_mode" not in function.group()


def test_three_independent_sessions_preserve_route_contract_and_priorities():
    base = dict(id="1234", plugins=dict(ros2dds=dict(allow=dict(publishers=["^/tf$"], subscribers=["^/goal$"]),
        pub_priorities=[".*/tf$=1:express", ".*/points$=6"])),
        connect=dict(endpoints=["tcp/old:7447"], retry=dict(period_init_ms=1000)))
    original = copy.deepcopy(base)
    profiles = configure(base, target="100.1.2.3", port=7447)
    assert base == original
    assert set(profiles) == {"realtime", "sensing", "bulk"}
    for index, (name, profile) in enumerate(profiles.items()):
        assert profile["plugins"]["ros2dds"]["traffic_class"] == name
        assert profile["plugins"]["ros2dds"]["allow"] == base["plugins"]["ros2dds"]["allow"]
        assert profile["plugins"]["ros2dds"]["pub_priorities"] == base["plugins"]["ros2dds"]["pub_priorities"]
        assert profile["connect"]["endpoints"] == [f"tcp/100.1.2.3:{7447+index}#so_sndbuf=65536"]
        assert profile["connect"]["retry"] == base["connect"]["retry"]
        assert profile["listen"]["endpoints"] == []
        assert "id" not in profile
        assert profile["scouting"]["gossip"]["enabled"] is False
        assert profile["transport"]["unicast"]["max_links"] == 1


def test_server_profiles_do_not_autoconnect_or_overlap_ports():
    profiles = configure({}, bind="127.0.0.1", port=9000)
    assert [p["listen"]["endpoints"] for p in profiles.values()] == [
        [f"tcp/127.0.0.1:{p}#so_sndbuf=65536"] for p in (9000,9001,9002)]
    assert all(not p["connect"]["endpoints"] for p in profiles.values())
    with pytest.raises(ValueError):
        configure({}, port=65534)


def test_isolation_requires_matching_binary_receipt(tmp_path):
    binary = tmp_path / "bridge"
    binary.write_bytes(b"native test")
    receipt = Path(str(binary)+".source.json")
    receipt.write_text(json.dumps(dict(binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                                      features=["traffic_class_sessions"])))
    check_binary(binary)
    binary.write_bytes(b"different binary")
    with pytest.raises(ValueError):
        check_binary(binary)


def test_public_session_lanes_require_all_mutual_tls_identity_material(tmp_path):
    files = {key: str(tmp_path / key) for key in ("root_ca_certificate", "connect_certificate",
                                                  "connect_private_key")}
    for path in files.values():
        Path(path).touch()
    tls = {**files, "enable_mtls": True, "verify_name_on_connect": True}
    profile = {"transport": {"link": {"tls": tls}}}
    result = configure(profile, target="hub.example", mtls=True)
    assert [config["connect"]["endpoints"][0] for config in result.values()] == [
        f"tls/hub.example:{port}#so_sndbuf=65536" for port in (7447,7448,7449)]
    assert all(config["transport"]["link"]["tls"]["close_link_on_expiration"]
               for config in result.values())
    assert not tls.get("close_link_on_expiration")
    with pytest.raises(ValueError, match="mTLS"):
        configure(profile, mtls=True)  # Server must present its own identity.
    tls["verify_name_on_connect"] = False
    with pytest.raises(ValueError, match="verified names"):
        configure(profile, target="hub.example", mtls=True)


@pytest.mark.parametrize("target", ["", "203.0.113.2"])
def test_optional_dscp_uses_six_bit_codes_without_ecn_bits(target):
    profiles = configure({}, target=target, dscp=parse_dscp("46,18,8"))
    key = "connect" if target else "listen"
    assert [profile[key]["endpoints"][0].rsplit(";", 1)[1] for profile in profiles.values()] == [
        "dscp=184", "dscp=72", "dscp=32"]
    ordinary = configure({}, target=target)
    assert all("dscp=" not in profile[key]["endpoints"][0] for profile in ordinary.values())


@pytest.mark.parametrize("value", ["46,18", "46,18,8,0", "64,0,0", "-1,0,0", "46,x,8"])
def test_dscp_rejects_invalid_codes_before_launch(value):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        parse_dscp(value)


def test_direct_dscp_mapping_and_endpoint_validation():
    from link_profile import endpoint
    for code in (True, -1, 64, "46"):
        with pytest.raises(ValueError, match="DSCP"):
            endpoint("127.0.0.1", 7447, 65536, dscp=code)
    with pytest.raises(ValueError, match="all three"):
        configure({}, dscp={"realtime": 46})
