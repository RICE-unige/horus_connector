#!/usr/bin/env bash

horus_load_config() {
  local file="$1" runtime="$2" resolved key value
  resolved="$(mktemp "${TMPDIR:-/tmp}/horus-config.XXXXXX")" || return 1
  if ! "${runtime}" "${ROOT}/scripts/connector_config.py" "${file}" >"${resolved}"; then
    rm -f -- "${resolved}"
    return 1
  fi
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    # Blank optional ROS settings are template defaults, not overrides.
    case "$key" in
      ROS_SETUP_PATH|CYCLONEDDS_URI|RMW_IMPLEMENTATION|ROS_STATIC_PEERS|ROS_DISCOVERY_SERVER|FASTRTPS_DEFAULT_PROFILES_FILE|FASTDDS_DEFAULT_PROFILES_FILE)
        [[ -n "$value" || -z "${!key:-}" ]] || continue
        ;;
    esac
    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done <"${resolved}"
  rm -f -- "${resolved}"
}
