import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_default_zenoh_profiles_route_goals_and_service_actions():
    import dds_preflight

    machine = dds_preflight.load_allow_patterns(ROOT / "config/zenoh_machine.json5")
    robot = dds_preflight.load_allow_patterns(ROOT / "config/zenoh_robot.json5")
    for topic in ("/goal", "/move_base_simple/goal", "/drone/goal", "/rover/command"):
        assert any(re.fullmatch(pattern, topic) for pattern in machine["publishers"]), topic
        assert any(re.fullmatch(pattern, topic) for pattern in robot["subscribers"]), topic
    assert machine["service_clients"]
    assert machine["action_clients"]


def test_all_zenoh_profiles_allow_bounded_burst_enqueue():
    for role in ("robot", "machine", "cloud"):
        profile = (ROOT / f"config/zenoh_{role}.json5").read_text()
        assert "wait_before_drop: 1000" in profile, role
        assert "batching: { enabled: true, time_limit: 1 }" in profile, role


def test_monitor_bundled_services_and_disabled_config(tmp_path):
    import horus_monitor

    config = tmp_path / "streams.json"
    config.write_text(
        '{"version":2,"session":{"room":"main","mode":"bundled"},'
        '"streams":[{"id":"front"},{"id":"rear"}]}', encoding="utf-8"
    )
    env = {
        "HORUS_STREAMS_CONFIG": str(config),
        "HORUS_ROLE": "robot",
        "HORUS_WEBRTC_ADDITIONAL_ROOMS": "second, third, second",
    }
    assert horus_monitor.configured_webrtc_services(env, ROOT) == [
        "webrtc", "webrtc-operator-1", "webrtc-operator-2"
    ]
    config.write_text('{"version":2,"streams":[{"id":"front","enabled":false}]}', encoding="utf-8")
    assert horus_monitor.configured_webrtc_services(env, ROOT) == []


def test_monitor_reports_expected_bundled_operator_processes(monkeypatch, tmp_path):
    import horus_monitor

    config = tmp_path / "streams.json"
    config.write_text('{"version":2,"streams":[{"id":"front"}]}', encoding="utf-8")
    env = {"HORUS_ROLE": "robot", "HORUS_STREAMS_CONFIG": str(config),
           "HORUS_WEBRTC_ADDITIONAL_ROOMS": "second"}
    state = {
        "env": env, "root": str(ROOT), "role": "robot", "topology": "hub",
        "processes": {
            name: horus_monitor.ProcessState(name, 1, True)
            for name in ("zenoh", "signal", "webrtc", "webrtc-operator-1")
        },
        "logs": {"zenoh": [], "signal": [], "webrtc": [], "webrtc-operator-1": []},
        "listeners": {}, "ports": {"zenoh": 7447, "signal": 8765, "turn": 3478},
        "target_ports": {}, "bridge_members": [], "topics": [], "signal_members": [],
        "target": "", "webrtc_peers": [], "webrtc_clients": [], "webrtc_registrations": [],
    }
    monkeypatch.setattr(horus_monitor, "named_webrtc_members", lambda *_args: [])
    services = horus_monitor.build_services(state)
    media = next(service for service in services if service.title == "WebRTC")
    assert media.status == "WAIT"
    state["media_health"] = {name: {"status_age_sec": 0, "tracks": {"front": {"state": "flowing"}}}
                             for name in ("webrtc", "webrtc-operator-1")}
    media = next(service for service in horus_monitor.build_services(state) if service.title == "WebRTC")
    assert media.status == "VIDEO"
    state["media_health"]["webrtc"]["tracks"]["front"]["state"] = "stale"
    media = next(service for service in horus_monitor.build_services(state) if service.title == "WebRTC")
    assert media.status == "STALE"


def test_monitor_rejects_reused_pid(tmp_path):
    import horus_monitor

    pid = os.getpid()
    (tmp_path / "webrtc.pid").write_text(str(pid), encoding="utf-8")
    (tmp_path / "webrtc.pid.start").write_text("not-this-process", encoding="utf-8")
    assert not horus_monitor.process_state(tmp_path, "webrtc").running
    (tmp_path / "webrtc.pid.start").write_text(horus_monitor.process_start_time(pid), encoding="utf-8")
    assert horus_monitor.process_state(tmp_path, "webrtc").running


def test_log_tail_reads_only_requested_lines(tmp_path):
    import horus_monitor

    log = tmp_path / "service.log"
    log.write_text("".join(f"old {index}\n" for index in range(1000)) + "last é\n", encoding="utf-8")
    assert horus_monitor.tail(log, 2) == ["old 999", "last é"]
    assert horus_monitor.tail(log, 0) == []


def test_stop_does_not_kill_unowned_process(tmp_path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "webrtc.pid").write_text(str(sleeper.pid), encoding="utf-8")
        (run_dir / "webrtc.pid.start").write_text("not-this-process", encoding="utf-8")
        env_file = tmp_path / "horus.env"
        env_file.write_text("HORUS_CLOUD_RUN_TURN=0\nZENOH_BRIDGE_RUNTIME=binary\n", encoding="utf-8")
        env = dict(os.environ, HORUS_ENV=str(env_file), HORUS_RUN_DIR=str(run_dir))
        result = subprocess.run([str(ROOT / "horus"), "stop"], env=env, cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert sleeper.poll() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_status_lists_bundled_operator_rooms(tmp_path):
    config = tmp_path / "streams.json"
    config.write_text('{"version":2,"session":{"room":"main"},"streams":[{"id":"front"}]}', encoding="utf-8")
    env_file = tmp_path / "horus.env"
    env_file.write_text(
        f"HORUS_ROLE=robot\nHORUS_STREAMS_CONFIG={config}\n"
        "HORUS_WEBRTC_ADDITIONAL_ROOMS=second\nHORUS_CLOUD_RUN_TURN=0\n"
        "HORUS_TOPOLOGY=direct\nHORUS_MACHINE_IP=127.0.0.1\n",
        encoding="utf-8",
    )
    env = dict(os.environ, HORUS_ENV=str(env_file), HORUS_RUN_DIR=str(tmp_path / "run"))
    result = subprocess.run([str(ROOT / "horus"), "status"], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "webrtc-operator-1" in result.stdout


def test_metrics_reuses_ros_topic_cache(monkeypatch, tmp_path):
    import horus_metrics

    seen = []

    def fake_extract(root, env, cache, force_refresh):
        seen.append((cache, force_refresh))
        return {}

    monkeypatch.setattr(horus_metrics, "extract_state", fake_extract)
    monkeypatch.setattr(horus_metrics, "status_snapshot", lambda _state: {"processes": {}, "topics": []})
    monkeypatch.setattr(horus_metrics, "build_services", lambda _state: [])
    cache = {}
    horus_metrics.render_prometheus(tmp_path, tmp_path / "horus.env", cache)
    horus_metrics.render_prometheus(tmp_path, tmp_path / "horus.env", cache)
    assert seen == [(cache, False), (cache, False)]
