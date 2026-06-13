#!/usr/bin/env bash
set -eo pipefail

ROOT="${HOME}/horus_connector"
ENV_FILE="${ROOT}/.env.uav_sim_machine"
LOG_DIR="${ROOT}/.run/uav_sim"
PID_FILE="${LOG_DIR}/zenoh_machine.pid"
LOG_FILE="${LOG_DIR}/zenoh_machine.log"

mkdir -p "${LOG_DIR}"
set -a
source "${ENV_FILE}"
set +a

source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
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
