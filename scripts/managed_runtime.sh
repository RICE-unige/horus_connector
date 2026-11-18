#!/usr/bin/env bash

horus_native_plugin_path() {
  local path combined="${GST_PLUGIN_PATH_1_0-${GST_PLUGIN_PATH:-}}"
  for path in "${WEBRTC_VENV:-${ROOT}/.venv-webrtc}"/native/usr/lib/*/gstreamer-1.0 \
      "${HORUS_MEDIA_GST_PLUGIN_DIR:-${ROOT}/.runtime/media-gcc/plugins}"; do
    [[ -d "${path}" ]] || continue
    combined="${path}${combined:+:${combined}}"
  done
  printf '%s' "${combined}"
}

horus_use_runtime_bundle() {
  local action="$1" path bundle="${HORUS_RUNTIME_BUNDLE:-}" digest="${HORUS_RUNTIME_SHA256:-}"
  [[ -n "${bundle}${digest}" ]] || return 0
  local args=()
  if [[ "${action}" == install ]]; then
    [[ -n "${bundle}" ]] || { printf 'Set HORUS_RUNTIME_BUNDLE to the release archive.\n' >&2; return 1; }
    args=(--archive "${bundle/#\~/${HOME}}")
  fi
  if [[ -n "${HORUS_RUNTIME_BRIDGE_REVISION:-}" ]]; then
    args+=(--bridge-revision "${HORUS_RUNTIME_BRIDGE_REVISION}")
  fi
  path="$(python3 "${ROOT}/scripts/runtime_bundle.py" "${action}" --root "${ROOT}/.runtime/releases" \
      --ros "${ROS_DISTRO:-$(horus_default_ros_distro)}" --sha256 "${digest}" "${args[@]}")" || return 1
  export ZENOH_BRIDGE_RUNTIME=binary ZENOH_BRIDGE_BINARY="${path}/zenoh-bridge-ros2dds"
  export HORUS_SOURCE_MEDIA_BINARY="${path}/horus_media_node"
  export HORUS_MEDIA_RELAY_BINARY="${path}/horus_unity_bridge_node"
  export HORUS_MEDIA_GST_PLUGIN_DIR="${path}"
}
