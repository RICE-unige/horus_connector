import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def wait_until(callback, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if callback():
                return
        except (OSError, ValueError):
            pass
        time.sleep(.05)
    raise AssertionError("condition was not reached within the deadline")


@pytest.mark.parametrize("inherited_name", [None, "unrelated-service"])
def test_service_pid_lookup_does_not_depend_on_callers_name(tmp_path, inherited_name):
    launcher = (ROOT / "horus").read_text()
    body = launcher.split("service_pid_owned() {\n", 1)[1].split("\n}\n", 1)[0]
    environment = dict(os.environ, RUN_DIR=str(tmp_path))
    environment.pop("name", None)
    if inherited_name is not None:
        environment["name"] = inherited_name
    script = "set -u\nservice_pid_owned() {\n" + body + "\n}\n"
    script += "service_pid_owned zenoh\n"
    result = subprocess.run(["bash", "-c", script], env=environment,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert not result.stderr


def test_supervisor_restarts_crash_and_stops_owned_group(tmp_path):
    state = tmp_path / "service.json"
    marker = tmp_path / "started"
    command = ("import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); "
               "first=not p.exists(); p.touch(); time.sleep(.35 if first else 20); "
               "sys.exit(2 if first else 0)")
    runner = subprocess.Popen([sys.executable, str(ROOT / "scripts/service_runner.py"),
                               "--state", str(state), "--log", str(tmp_path / "service.log"),
                               "--startup-grace", ".1", "--stop-grace", "1", "--",
                               sys.executable, "-c", command, str(marker)])
    try:
        wait_until(lambda: json.loads(state.read_text()).get("restarts", 0) == 1
                   and json.loads(state.read_text())["phase"] == "running")
        child = json.loads(state.read_text())["child_pid"]
        runner.terminate()
        assert runner.wait(timeout=4) == 0
        assert not Path(f"/proc/{child}").exists()
        assert json.loads(state.read_text())["phase"] == "stopped"
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.wait(timeout=5)


def test_rotating_logs_are_bounded(tmp_path):
    from service_runner import RotatingOutput
    output = RotatingOutput(tmp_path / "service.log", 100, 2)
    for _ in range(20):
        output.write(b"x" * 60)
    output.close()
    assert len(list(tmp_path.iterdir())) == 3
    assert all(path.stat().st_size <= 100 for path in tmp_path.iterdir())


def test_forced_cleanup_verifies_supervisor_and_child_identity(tmp_path):
    from service_runner import process_start_time, stop_owned_group
    state = tmp_path / "service.json"
    runner = subprocess.Popen([sys.executable, str(ROOT / "scripts/service_runner.py"),
                               "--state", str(state), "--log", str(tmp_path / "service.log"),
                               "--startup-grace", ".1", "--stop-grace", "1", "--",
                               sys.executable, "-c", "import time; time.sleep(20)"])
    child = None
    try:
        wait_until(lambda: json.loads(state.read_text()).get("phase") == "running")
        child = json.loads(state.read_text())["child_pid"]
        owner_start = process_start_time(runner.pid)
        os.kill(runner.pid, signal.SIGSTOP)
        assert not stop_owned_group(state, runner.pid, "wrong-start")
        assert process_start_time(child) is not None
        record = json.loads(state.read_text())
        record["child_start"] = "wrong-start"
        state.write_text(json.dumps(record))
        assert not stop_owned_group(state, runner.pid, owner_start)
        record["child_start"] = process_start_time(child)
        state.write_text(json.dumps(record))
        assert stop_owned_group(state, runner.pid, owner_start)
        wait_until(lambda: process_start_time(child) is None)
    finally:
        os.kill(runner.pid, signal.SIGCONT)
        runner.terminate()
        runner.wait(timeout=5)
        if child and process_start_time(child) is not None:
            os.killpg(child, signal.SIGKILL)


def test_cloud_launcher_is_idempotent_and_rolls_back_failure(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        blocked_metrics = occupied.getsockname()[1]
        env_file = tmp_path / "settings.env"
        run_dir = tmp_path / "run"
        basic = (f"HORUS_ZENOH_ENABLED=0\nHORUS_CLOUD_RUN_TURN=0\n"
                 f"WEBRTC_SIGNAL_PORT={port}\nHORUS_WEBRTC_RELAY_ALLOW_UNAUTHENTICATED=1\n")
        env_file.write_text(basic + f"HORUS_METRICS_PORT={blocked_metrics}\n")
        env = dict(os.environ, HORUS_ENV=str(env_file), HORUS_RUN_DIR=str(run_dir),
                   HORUS_WEBRTC_PYTHON=sys.executable)
        def launch():
            return subprocess.run([str(ROOT / "horus"), "launch", "cloud", "--no-monitor"],
                                  env=env, capture_output=True, text=True, timeout=30)
        try:
            failed = launch()
            assert failed.returncode != 0
            assert "stopping only services created" in failed.stdout + failed.stderr
            assert not (run_dir / "signal.pid").exists()
            state_path = run_dir / "metrics.service.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
                for pid in (state.get("supervisor_pid"), state.get("child_pid")):
                    if pid:
                        status = Path(f"/proc/{pid}/stat")
                        assert not status.exists() or status.read_text().rsplit(")",1)[1].split()[0] == "Z", state
            env_file.write_text(basic + "HORUS_METRICS_PORT=0\n")
            first = launch()
            assert first.returncode == 0, first.stdout + first.stderr
            pid = (run_dir / "signal.pid").read_text()
            second = launch()
            assert second.returncode == 0, second.stdout + second.stderr
            assert (run_dir / "signal.pid").read_text() == pid
        finally:
            stopped = subprocess.run([str(ROOT / "horus"), "stop"], env=env,
                                     capture_output=True, text=True, timeout=30)
            assert stopped.returncode == 0, stopped.stderr


def test_benchmark_never_shapes_or_clears_lan_by_default(monkeypatch):
    import run_distributed_benchmark as benchmark
    calls = []
    monkeypatch.setattr(benchmark, "route_interface", lambda *_args: "wlan0")
    monkeypatch.setattr(benchmark, "checked", lambda _node, script, **_kwargs: calls.append(script) or "qdisc fq_codel 0: root")
    benchmark.apply_network_profile("wsl", "127.0.0.1", "unconstrained")
    benchmark.clear_network_profile("wsl", "wlan0")
    with pytest.raises(RuntimeError, match="disposable"):
        benchmark.apply_network_profile("wsl", "127.0.0.1", "bw5")
    assert not any("sudo" in script or "qdisc del" in script or "qdisc replace" in script for script in calls)


def test_launch_and_stop_preserve_an_existing_shared_turn_service(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "system-calls"
    for name, body in {
        "systemctl": 'printf "%s\\n" "$*" >> "$TEST_SYSTEM_CALLS"\nexit 0\n',
        "sudo": 'printf "unexpected sudo %s\\n" "$*" >> "$TEST_SYSTEM_CALLS"\nexit 13\n',
        "turnserver": 'exit 13\n',
    }.items():
        executable = binaries / name
        executable.write_text("#!/bin/sh\n" + body)
        executable.chmod(0o755)
    settings = tmp_path / "settings.env"
    settings.write_text("HORUS_ZENOH_ENABLED=0\nHORUS_CLOUD_RUN_SIGNALING=0\n"
                        "HORUS_CLOUD_RUN_TURN=1\nHORUS_METRICS_PORT=0\n")
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"],
               TEST_SYSTEM_CALLS=str(calls), HORUS_ENV=str(settings),
               HORUS_RUN_DIR=str(tmp_path / "run"), HORUS_WEBRTC_PYTHON=sys.executable)
    for command in (("launch", "cloud", "--no-monitor"), ("stop",)):
        result = subprocess.run([str(ROOT / "horus"), *command], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
    assert "shared system service left unchanged" in result.stdout
    observed = calls.read_text()
    assert "is-active" in observed
    assert "unexpected sudo" not in observed and "stop " not in observed and "restart " not in observed


def test_benchmark_unowned_cleanup_is_noop(monkeypatch):
    import run_distributed_benchmark as benchmark
    monkeypatch.setattr(benchmark, "node_cmd", lambda *_args, **_kwargs: pytest.fail("unowned cleanup attempted"))
    benchmark.stop_all()
    benchmark.stop_label("wsl", "old-unrelated-process")


def test_hololens_partial_body_has_absolute_deadline():
    from field_teammate_hololens_relay import DeadlineReader
    first, second = socket.socketpair()
    try:
        reader = DeadlineReader(first, .1)
        second.sendall(b"a")
        with pytest.raises(TimeoutError):
            reader.read(5)
    finally:
        first.close()
        second.close()
