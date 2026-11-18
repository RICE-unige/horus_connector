#!/usr/bin/env bash

horus_ros_setup_file() {
  local path="${ROS_SETUP_PATH:-}" distribution="${ROS_DISTRO:-}"
  if [[ -n "${path}" ]]; then
    path="${path/#\~/${HOME}}"
  else
    distribution="${distribution:-$(horus_default_ros_distro)}"
    path="/opt/ros/${distribution}/setup.bash"
    if [[ ! -r "${path}" && -r "/opt/ros/${distribution}/local_setup.bash" ]]; then
      path="/opt/ros/${distribution}/local_setup.bash"
    fi
  fi
  printf '%s\n' "${path}"
}

# Source a ROS installation once, retaining the configured DDS contract.
horus_ros_environment() {
  [[ "${horus_ros_ready:-0}" != "1" ]] || return 0
  local distribution="${ROS_DISTRO:-}" path key selected terminal status=0
  local -A configured=()
  path="$(horus_ros_setup_file)"
  [[ -f "${path}" && -r "${path}" ]] || { printf 'ROS setup not found: %s\n' "${path}" >&2; return 1; }
  for key in ROS_DOMAIN_ID RMW_IMPLEMENTATION ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE \
             ROS_STATIC_PEERS ROS_DISCOVERY_SERVER CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE \
             FASTDDS_DEFAULT_PROFILES_FILE; do
    if [[ -v "${key}" ]]; then configured[${key}]="${!key}"; fi
  done
  local restore_nounset=0
  if [[ "$-" == *u* ]]; then restore_nounset=1; set +u; fi
  # shellcheck source=/dev/null
  source "${path}" || status=$?
  if (( restore_nounset )); then set -u; fi
  (( status == 0 )) || { printf 'Could not load ROS setup: %s\n' "${path}" >&2; return "${status}"; }
  if [[ -n "${distribution}" && "${ROS_DISTRO:-}" != "${distribution}" ]]; then
    printf 'ROS setup loaded %s but the profile requires %s.\n' "${ROS_DISTRO:-no distro}" "${distribution}" >&2
    return 1
  fi
  selected="${configured[ROS_DOMAIN_ID]:-${ROS_DOMAIN_ID:-0}}"
  terminal="${HORUS_TERMINAL_ROS_DOMAIN_ID:-}"
  if [[ ! "${selected}" =~ ^[0-9]{1,3}$ ]] || (( 10#${selected} > 232 )); then
    printf 'ROS_DOMAIN_ID must be an integer from 0 through 232.\n' >&2
    return 1
  fi
  if [[ -n "${terminal}" ]] && { [[ ! "${terminal}" =~ ^[0-9]{1,3}$ ]] ||
                                 (( 10#${terminal} != 10#${selected} )); }; then
    printf 'ROS_DOMAIN_ID conflict: terminal %s, profile %s. Use the domain of the local ROS graph.\n' \
      "${terminal}" "${selected}" >&2
    return 1
  fi
  for key in "${!configured[@]}"; do
    printf -v "${key}" '%s' "${configured[${key}]}"
    export "${key}"
  done
  export ROS_DOMAIN_ID="$((10#${selected}))"
  export ROS_SETUP_PATH="${path}"
  horus_ros_ready=1
}

horus_default_ros_distro() (
  if [[ -r /etc/os-release ]]; then
    source /etc/os-release
    if [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 22.04 ]]; then
      printf '%s\n' humble
      return
    fi
  fi
  printf '%s\n' jazzy
)
