#!/usr/bin/env bash
set -eo pipefail

export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-51}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"
export ROS_AUTOMATIC_DISCOVERY_RANGE="${ROS_AUTOMATIC_DISCOVERY_RANGE:-LOCALHOST}"

source /opt/ros/jazzy/setup.bash
export LD_PRELOAD="/opt/ros/jazzy/lib/x86_64-linux-gnu/liboctomap.so${LD_PRELOAD:+:$LD_PRELOAD}"

exec rviz2 -d "$HOME/horus_connector/rviz/uav_sim_horus.rviz"
