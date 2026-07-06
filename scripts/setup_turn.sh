#!/usr/bin/env bash
set -euo pipefail

TURN_ENABLED="${HORUS_CLOUD_RUN_TURN:-0}"
if [[ "${TURN_ENABLED}" != "1" ]]; then
  exit 0
fi

TURN_PORT="${TURN_PORT:-3478}"
TURN_MIN_PORT="${TURN_MIN_PORT:-49152}"
TURN_MAX_PORT="${TURN_MAX_PORT:-65535}"
TURN_REALM="${TURN_REALM:-horus}"
TURN_USER="${TURN_USER:-horus}"
TURN_PASSWORD="${TURN_PASSWORD:-}"
TURN_PUBLIC_IP="${TURN_PUBLIC_IP:-${HORUS_CLOUD_IP:-}}"
TURN_PRIVATE_IP="${TURN_PRIVATE_IP:-${HORUS_PRIVATE_IP:-}}"
TURN_LISTEN_IP="${TURN_LISTEN_IP:-0.0.0.0}"

if [[ -z "${TURN_PASSWORD}" || "${TURN_PASSWORD}" == "change-me" ]]; then
  echo "HORUS_CLOUD_RUN_TURN=1 requires TURN_PASSWORD in .env." >&2
  echo "Set a real password, then rerun ./horus bootstrap cloud." >&2
  exit 2
fi

if [[ ! "${TURN_PORT}" =~ ^[0-9]+$ || ! "${TURN_MIN_PORT}" =~ ^[0-9]+$ || ! "${TURN_MAX_PORT}" =~ ^[0-9]+$ ]]; then
  echo "TURN_PORT, TURN_MIN_PORT, and TURN_MAX_PORT must be numeric." >&2
  exit 2
fi

if (( TURN_MIN_PORT > TURN_MAX_PORT )); then
  echo "TURN_MIN_PORT must be less than or equal to TURN_MAX_PORT." >&2
  exit 2
fi

if [[ -z "${TURN_PRIVATE_IP}" && -n "${TURN_PUBLIC_IP}" ]]; then
  TURN_PRIVATE_IP="$(
    hostname -I 2>/dev/null \
      | tr ' ' '\n' \
      | awk '/^([0-9]{1,3}\.){3}[0-9]{1,3}$/ && $0 !~ /^127\./ && $0 !~ /^172\.17\./ { print; exit }'
  )"
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
  elif sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
  elif [[ -t 0 ]]; then
    sudo "$@"
  else
    echo "TURN setup needs sudo to write /etc/turnserver.conf and restart coturn." >&2
    return 1
  fi
}

tmp_conf="$(mktemp)"
{
  echo "listening-ip=${TURN_LISTEN_IP}"
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
  echo "no-multicast-peers"
  if [[ -n "${TURN_PUBLIC_IP}" ]]; then
    if [[ -n "${TURN_PRIVATE_IP}" && "${TURN_PRIVATE_IP}" != "${TURN_PUBLIC_IP}" ]]; then
      echo "external-ip=${TURN_PUBLIC_IP}/${TURN_PRIVATE_IP}"
    else
      echo "external-ip=${TURN_PUBLIC_IP}"
    fi
  fi
} > "${tmp_conf}"

if getent group turnserver >/dev/null 2>&1; then
  run_root install -o root -g turnserver -m 0640 "${tmp_conf}" /etc/turnserver.conf
else
  run_root install -m 0644 "${tmp_conf}" /etc/turnserver.conf
fi
rm -f "${tmp_conf}"

if [[ -f /etc/default/coturn ]]; then
  tmp_default="$(mktemp)"
  sed 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn > "${tmp_default}" || true
  if ! grep -q '^TURNSERVER_ENABLED=' "${tmp_default}"; then
    echo "TURNSERVER_ENABLED=1" >> "${tmp_default}"
  fi
  run_root install -m 0644 "${tmp_default}" /etc/default/coturn
  rm -f "${tmp_default}"
else
  tmp_default="$(mktemp)"
  echo "TURNSERVER_ENABLED=1" > "${tmp_default}"
  run_root install -m 0644 "${tmp_default}" /etc/default/coturn
  rm -f "${tmp_default}"
fi

if command -v systemctl >/dev/null 2>&1; then
  service_name="coturn"
  if ! systemctl list-unit-files coturn.service >/dev/null 2>&1 && systemctl list-unit-files turnserver.service >/dev/null 2>&1; then
    service_name="turnserver"
  fi
  run_root systemctl enable "${service_name}" >/dev/null 2>&1 || true
  run_root systemctl restart "${service_name}"
  run_root systemctl --no-pager --quiet is-active "${service_name}"
  echo "TURN relay is running on udp/tcp ${TURN_PORT}, relay ports ${TURN_MIN_PORT}-${TURN_MAX_PORT}."
else
  echo "TURN config written to /etc/turnserver.conf. Start coturn with your init system."
fi
