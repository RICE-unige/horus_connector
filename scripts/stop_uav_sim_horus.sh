#!/usr/bin/env bash
set -eo pipefail

ROOT="${1:-${HOME}/horus_connector}"
LOG_DIR="${ROOT}/.run/uav_sim"

for pid_file in "${LOG_DIR}"/*.pid; do
  [[ -f "${pid_file}" ]] || continue
  pid="$(cat "${pid_file}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" || true
    sleep 1
    kill -0 "${pid}" >/dev/null 2>&1 && kill -9 "${pid}" || true
  fi
  rm -f "${pid_file}"
done
