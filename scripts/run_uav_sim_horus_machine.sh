#!/usr/bin/env bash
set -eo pipefail

ROOT="${HOME}/horus_connector"
ENV_FILE="${ROOT}/.env.uav_sim_machine"
LOG_DIR="${ROOT}/.run/uav_sim"
PID_FILE="${LOG_DIR}/zenoh_machine.pid"
LOG_FILE="${LOG_DIR}/zenoh_machine.log"

mkdir -p "${LOG_DIR}"

trim_spaces() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

load_key_value_file() {
  local file="$1"
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    line="$(trim_spaces "${line}")"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ "${line}" =~ ^export[[:space:]]+(.+)$ ]]; then
      line="${BASH_REMATCH[1]}"
    fi
    if [[ ! "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      echo "Ignoring invalid config line in ${file}: ${line}" >&2
      continue
    fi
    key="${BASH_REMATCH[1]}"
    value="$(trim_spaces "${BASH_REMATCH[2]}")"
    if [[ "${#value}" -ge 2 ]]; then
      if [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done < "${file}"
}

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing UAV simulation config: ${ENV_FILE}" >&2
  exit 1
fi
load_key_value_file "${ENV_FILE}"

ROS_SETUP="/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
if [[ ! -f "${ROS_SETUP}" ]]; then
  echo "ROS setup file not found: ${ROS_SETUP}" >&2
  exit 1
fi
source "${ROS_SETUP}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-51}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" >/dev/null 2>&1; then
  echo "machine bridge already running: $(cat "${PID_FILE}")"
  exit 0
fi

nohup "${ROOT}/zenoh-bridge-ros2dds" \
  -c "${ROOT}/config/zenoh_uav_sim_machine.json5" \
  --no-multicast-scouting \
  -l "tcp/0.0.0.0:${ZENOH_PORT:-7447}" \
  router >"${LOG_FILE}" 2>&1 < /dev/null &

echo "$!" > "${PID_FILE}"
echo "started machine bridge: $(cat "${PID_FILE}")"
