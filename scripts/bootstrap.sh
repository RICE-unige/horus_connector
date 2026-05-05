#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROLE_ARG="${1:-}"
ROLE=""
RUN_APT="${RUN_APT:-auto}"

if [[ "${ROLE_ARG}" == "-h" || "${ROLE_ARG}" == "--help" ]]; then
  cat <<'USAGE'
Usage: scripts/bootstrap.sh [robot|machine|cloud|auto]

Installs the local runtime pieces:
  - Zenoh ROS 2 DDS bridge binary
  - Python WebRTC dependencies
  - GStreamer video encoder detection profile

If passwordless sudo is unavailable, the script prints the apt command and
continues with user-local setup.
USAGE
  exit 0
fi

have() {
  command -v "$1" >/dev/null 2>&1
}

machine_kind() {
  if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    echo "wsl2"
  elif [[ "$(uname -s)" == "Linux" ]]; then
    echo "linux"
  else
    echo "unknown"
  fi
}

APT_BASE_PACKAGES=(
  curl
  ca-certificates
  unzip
  python3
  python3-pip
  python3-venv
)

APT_MEDIA_PACKAGES=(
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad
  gstreamer1.0-nice
  gstreamer1.0-plugins-ugly
  gstreamer1.0-libav
  python3-gi
  python3-gst-1.0
  gir1.2-gstreamer-1.0
  gir1.2-gst-plugins-base-1.0
  v4l-utils
  vainfo
  mesa-va-drivers
  intel-media-va-driver
)

run_apt() {
  if [[ "${RUN_APT}" == "no" || ! -x /usr/bin/apt-get ]]; then
    return
  fi

  local packages=("${APT_BASE_PACKAGES[@]}")
  if [[ "${ROLE}" != "cloud" ]]; then
    packages+=("${APT_MEDIA_PACKAGES[@]}")
  fi

  local install_cmd
  printf -v install_cmd ' %q' "${packages[@]}"
  install_cmd="sudo apt-get update && sudo apt-get install -y${install_cmd}"

  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${packages[@]}"
  elif sudo -n true >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  else
    echo "No passwordless sudo. Run this once if packages are missing:"
    echo "${install_cmd}"
  fi
}

latest_zenoh_version() {
  if [[ -n "${ZENOH_VERSION:-}" ]]; then
    echo "${ZENOH_VERSION}"
    return
  fi

  if have curl; then
    local latest
    latest="$(curl -fsSL --connect-timeout 10 --max-time 20 https://api.github.com/repos/eclipse-zenoh/zenoh-plugin-ros2dds/releases/latest 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      | head -1 \
      | sed 's/^v//')"
    [[ -n "${latest}" ]] && echo "${latest}" && return
  fi

  echo "1.9.0"
}

rust_target() {
  case "$(uname -m)" in
    x86_64|amd64) echo "x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) echo "aarch64-unknown-linux-gnu" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; return 1 ;;
  esac
}

install_zenoh() {
  local version target zip url
  version="$(latest_zenoh_version)"
  target="$(rust_target)"
  zip="${ROOT}/zenoh-plugin-ros2dds-${version}-${target}-standalone.zip"
  url="https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds/releases/download/${version}/zenoh-plugin-ros2dds-${version}-${target}-standalone.zip"

  echo "Zenoh bridge target: ${version}"
  if [[ -x "${ROOT}/zenoh-bridge-ros2dds" ]] && "${ROOT}/zenoh-bridge-ros2dds" --version 2>/dev/null | grep -q "${version}"; then
    echo "Zenoh bridge already installed."
    return
  fi

  if ! have curl || ! have unzip; then
    echo "curl/unzip missing; cannot install Zenoh automatically." >&2
    return 1
  fi

  curl -fL --retry 3 --connect-timeout 20 -o "${zip}.tmp" "${url}"
  mv "${zip}.tmp" "${zip}"
  unzip -o "${zip}" -d "${ROOT}"
  chmod +x "${ROOT}/zenoh-bridge-ros2dds" "${ROOT}/libzenoh_plugin_ros2dds.so" 2>/dev/null || true
  "${ROOT}/zenoh-bridge-ros2dds" --version || true
}

cd "${ROOT}"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it before launch."
fi

set -a
# shellcheck source=/dev/null
source .env
set +a

if [[ -n "${ROLE_ARG}" && "${ROLE_ARG}" != "auto" ]]; then
  ROLE="${ROLE_ARG}"
else
  ROLE="${HORUS_ROLE:-machine}"
fi

if [[ -z "${ROLE}" || "${ROLE}" == "auto" ]]; then
  ROLE="machine"
fi

case "${ROLE}" in
  robot|machine|cloud) ;;
  *)
    echo "Unknown role: ${ROLE}" >&2
    echo "Use robot, machine, cloud, or auto." >&2
    exit 2
    ;;
esac

echo "Bootstrap role: ${ROLE}"
echo "Machine: $(machine_kind) $(uname -m)"

run_apt
install_zenoh
"${SCRIPT_DIR}/setup_webrtc.sh" "${ROLE}"
if [[ "${ROLE}" == "cloud" ]]; then
  echo "Cloud role: skipping media hardware detection; hub mode relays signaling only."
else
  "${SCRIPT_DIR}/detect_video_stack.sh" --role "${ROLE}" --out "${ROOT}/.webrtc_profile.env"
fi

echo
echo "Bootstrap complete."
echo "Next:"
echo "  edit .env"
echo "  ./horus launch ${ROLE}"
