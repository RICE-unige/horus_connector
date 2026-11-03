#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime="${HORUS_WEBRTC_PYTHON:-${root}/.venv-webrtc/bin/python}"
if [[ ! -x "${runtime}" && -z "${HORUS_WEBRTC_PYTHON:-}" ]]; then
  runtime="$(command -v python3)"
fi
if [[ ! -x "${runtime}" ]]; then
  printf 'WebRTC Python runtime is unavailable: %s\n' "${runtime}" >&2
  exit 1
fi
port="${HORUS_WEBRTC_TEST_PORT:-19876}"
duration="${HORUS_WEBRTC_TEST_DURATION:-8}"
start_order="${HORUS_WEBRTC_TEST_ORDER:-robot-first}"
reconnect="${HORUS_WEBRTC_TEST_RECONNECT:-0}"
robot_duration="${HORUS_WEBRTC_TEST_ROBOT_DURATION:-${duration}}"
if [[ "${reconnect}" == "1" ]]; then
  robot_duration="${HORUS_WEBRTC_TEST_ROBOT_DURATION:-$((duration * 2 + 3))}"
fi
tmp_dir="$(mktemp -d)"
relay_pid=""
machine_pid=""
robot_pid=""

cleanup() {
  for pid in "${robot_pid}" "${machine_pid}" "${relay_pid}"; do
    if [[ -n "${pid}" ]]; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

fail_with_logs() {
  local message="$1"
  printf '%s\n' "${message}" >&2
  for log in relay machine robot; do
    printf '%s\n' "--- ${log}.log ---" >&2
    cat "${tmp_dir}/${log}.log" >&2 || true
  done
  exit 1
}

cd "${root}"
"${runtime}" scripts/webrtc_signal_relay.py \
  --host 127.0.0.1 \
  --port "${port}" \
  --credentials-file tests/fixtures/webrtc_credentials_test.json \
  >"${tmp_dir}/relay.log" 2>&1 &
relay_pid=$!
sleep 0.5

start_machine() {
  HORUS_WEBRTC_CLIENT_ID=machine:bundled-test \
  HORUS_WEBRTC_CLIENT_TOKEN=test-machine-token \
  "${runtime}" scripts/gst_webrtc_h264_machine.py \
    --streams-config tests/fixtures/webrtc_bundled_testsrc.json \
    --signaling-url "ws://127.0.0.1:${port}" \
    --video-output display \
    --video-sink fakesink \
    --profile tests/fixtures/webrtc_cpu_test.env \
    --clock-offset-ms 0 \
    --latency-json "${tmp_dir}/machine.json" \
    --duration "${duration}" \
    >"${tmp_dir}/machine.log" 2>&1 &
  machine_pid=$!
}

start_robot() {
  HORUS_WEBRTC_CLIENT_ID=robot:bundled-test \
  HORUS_WEBRTC_CLIENT_TOKEN=test-robot-token \
  "${runtime}" scripts/gst_webrtc_h264_robot.py \
    --streams-config tests/fixtures/webrtc_bundled_testsrc.json \
    --signaling-url "ws://127.0.0.1:${port}" \
    --latency-probe \
    --profile tests/fixtures/webrtc_cpu_test.env \
    --duration "${robot_duration}" \
    >"${tmp_dir}/robot.log" 2>&1 &
  robot_pid=$!
}

case "${start_order}" in
  robot-first)
    start_robot
    sleep 0.5
    start_machine
    ;;
  machine-first)
    start_machine
    sleep 0.5
    start_robot
    ;;
  *)
    fail_with_logs "HORUS_WEBRTC_TEST_ORDER must be robot-first or machine-first"
    ;;
esac

if [[ "${reconnect}" == "1" ]]; then
  if [[ "${start_order}" != "robot-first" ]]; then
    fail_with_logs "The reconnect smoke test requires HORUS_WEBRTC_TEST_ORDER=robot-first"
  fi
  wait "${machine_pid}" || fail_with_logs "First machine WebRTC endpoint failed"
  machine_pid=""
  "${runtime}" tests/check_media_results.py "${tmp_dir}/machine.json" || fail_with_logs "First session did not deliver decoded frames and matching timing"
  mv "${tmp_dir}/machine.log" "${tmp_dir}/machine-first.log"
  sleep 0.5
  start_machine
  wait "${machine_pid}" || fail_with_logs "Reconnected machine WebRTC endpoint failed"
  machine_pid=""
  grep -q "Incoming RTP video track=front" "${tmp_dir}/machine-first.log" \
    || fail_with_logs "First machine session did not decode the front track"
  grep -q "Incoming RTP video track=rear" "${tmp_dir}/machine-first.log" \
    || fail_with_logs "First machine session did not decode the rear track"
fi

wait "${robot_pid}" || fail_with_logs "Robot WebRTC endpoint failed"
robot_pid=""
if [[ -n "${machine_pid}" ]]; then
  wait "${machine_pid}" || fail_with_logs "Machine WebRTC endpoint failed"
  machine_pid=""
fi

grep -q "WebRTC session accepted: role=robot" "${tmp_dir}/robot.log" \
  || fail_with_logs "Robot endpoint was not authenticated by the relay"
grep -q "WebRTC session accepted: role=machine" "${tmp_dir}/machine.log" \
  || fail_with_logs "Machine endpoint was not authenticated by the relay"
grep -q "Incoming RTP video track=front" "${tmp_dir}/machine.log" \
  || fail_with_logs "Front video track was not decoded"
grep -q "Incoming RTP video track=rear" "${tmp_dir}/machine.log" \
  || fail_with_logs "Rear video track was not decoded"
"${runtime}" tests/check_media_results.py "${tmp_dir}/machine.json" || fail_with_logs "Insufficient decoded continuity or frame matching"

if grep -Eq "Traceback|GStreamer error|invalid signaling token" \
  "${tmp_dir}/relay.log" "${tmp_dir}/machine.log" "${tmp_dir}/robot.log"; then
  fail_with_logs "WebRTC smoke test logged a transport or media error"
fi

printf '%s\n' "Authenticated bundled WebRTC smoke test passed (front + rear tracks)."
if [[ "${reconnect}" == "1" ]]; then
  printf '%s\n' "Machine reconnect passed without restarting the robot endpoint."
fi
