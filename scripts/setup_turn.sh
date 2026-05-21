#!/usr/bin/env bash
set -euo pipefail

TURN_ENABLED="${HORUS_CLOUD_RUN_TURN:-0}"
if [[ "${TURN_ENABLED}" != "1" ]]; then
  exit 0
fi

TURN_PORT="${TURN_PORT:-3478}"
TURN_MIN_PORT="${TURN_MIN_PORT:-49160}"
TURN_MAX_PORT="${TURN_MAX_PORT:-49200}"
TURN_REALM="${TURN_REALM:-horus}"
TURN_USER="${TURN_USER:-horus}"
TURN_PASSWORD="${TURN_PASSWORD:-}"
TURN_PUBLIC_IP="${TURN_PUBLIC_IP:-${HORUS_CLOUD_IP:-}}"

if [[ -z "${TURN_PASSWORD}" || "${TURN_PASSWORD}" == "change-me" ]]; then
  echo "HORUS_CLOUD_RUN_TURN=1 requires TURN_PASSWORD in .env." >&2
  echo "Set a real password, then rerun ./horus bootstrap cloud." >&2
  exit 2
fi

if ! command -v turnserver >/dev/null 2>&1; then
  echo "TURN requested, but coturn is not installed." >&2
  echo "Install it with: sudo apt-get update && sudo apt-get install -y coturn" >&2
  echo "Continuing without a local TURN relay." >&2
  exit 0
fi

run_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

tmp_conf="$(mktemp)"
{
  echo "listening-port=${TURN_PORT}"
  echo "fingerprint"
  echo "lt-cred-mech"
  echo "realm=${TURN_REALM}"
  echo "user=${TURN_USER}:${TURN_PASSWORD}"
  echo "min-port=${TURN_MIN_PORT}"
  echo "max-port=${TURN_MAX_PORT}"
  echo "no-cli"
  echo "no-tls"
  echo "no-dtls"
  echo "log-file=/var/log/turnserver.log"
  echo "simple-log"
  if [[ -n "${TURN_PUBLIC_IP}" ]]; then
    echo "external-ip=${TURN_PUBLIC_IP}"
  fi
} > "${tmp_conf}"

run_root install -m 0640 "${tmp_conf}" /etc/turnserver.conf
rm -f "${tmp_conf}"

if [[ -f /etc/default/coturn ]]; then
  tmp_default="$(mktemp)"
  sed 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn > "${tmp_default}" || true
  if ! grep -q '^TURNSERVER_ENABLED=' "${tmp_default}"; then
    echo "TURNSERVER_ENABLED=1" >> "${tmp_default}"
  fi
  run_root install -m 0644 "${tmp_default}" /etc/default/coturn
  rm -f "${tmp_default}"
fi

if command -v systemctl >/dev/null 2>&1; then
  run_root systemctl enable coturn >/dev/null 2>&1 || true
  run_root systemctl restart coturn
  run_root systemctl --no-pager --quiet is-active coturn
  echo "TURN relay is running on udp/tcp ${TURN_PORT}, relay ports ${TURN_MIN_PORT}-${TURN_MAX_PORT}."
else
  echo "TURN config written to /etc/turnserver.conf. Start coturn with your init system."
fi
