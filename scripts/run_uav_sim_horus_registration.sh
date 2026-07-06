#!/usr/bin/env bash
set -eo pipefail

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-51}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

source /opt/ros/jazzy/setup.bash
cd "${HOME}/horus_sdk"
exec python3 python/examples/uav_sim_horus_registration.py "$@"
