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

is_jetson() {
  [[ -f /etc/nv_tegra_release ]] && return 0
  grep -qi 'nvidia,tegra\|jetson' /proc/device-tree/compatible 2>/dev/null
}

hardware_kind() {
  if is_jetson; then
    echo "jetson"
  elif [[ -d /dev/dri ]]; then
    echo "drm"
  else
    echo "generic"
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

APT_SIGNALING_PACKAGES=(
  python3-websocket
  python3-websockets
)

APT_MEDIA_COMMON_PACKAGES=(
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
  gir1.2-gst-plugins-bad-1.0
  v4l-utils
)

APT_DRM_MEDIA_PACKAGES=(
  vainfo
  mesa-va-drivers
)

APT_INTEL_MEDIA_PACKAGES=(
  gstreamer1.0-vaapi
)

APT_INTEL_DRIVER_PACKAGES=(
  intel-media-va-driver
  intel-media-va-driver-non-free
)

APT_JETSON_MEDIA_PACKAGES=(
  nvidia-l4t-gstreamer
  nvidia-l4t-multimedia
  nvidia-l4t-multimedia-utils
)

apt_package_available() {
  apt-cache show "$1" >/dev/null 2>&1
}

apt_package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

append_available_packages() {
  local -n packages_ref="$1"
  shift
  local package
  for package in "$@"; do
    if apt_package_available "${package}"; then
      packages_ref+=("${package}")
    fi
  done
}

append_intel_media_driver() {
  local -n packages_ref="$1"
  local package

  for package in "${APT_INTEL_DRIVER_PACKAGES[@]}"; do
    if apt_package_installed "${package}"; then
      packages_ref+=("${package}")
      return
    fi
  done

  case "${HORUS_INTEL_MEDIA_DRIVER:-free}" in
    free)
      if apt_package_available intel-media-va-driver; then
        packages_ref+=(intel-media-va-driver)
      elif apt_package_available intel-media-va-driver-non-free; then
        packages_ref+=(intel-media-va-driver-non-free)
      fi
      ;;
    non-free)
      if apt_package_available intel-media-va-driver-non-free; then
        packages_ref+=(intel-media-va-driver-non-free)
      elif apt_package_available intel-media-va-driver; then
        packages_ref+=(intel-media-va-driver)
      fi
      ;;
    *)
      echo "Unknown HORUS_INTEL_MEDIA_DRIVER=${HORUS_INTEL_MEDIA_DRIVER}. Use free or non-free." >&2
      exit 2
      ;;
  esac
}

build_apt_packages() {
  local -n output_ref="$1"
  output_ref=("${APT_BASE_PACKAGES[@]}")
  append_available_packages output_ref "${APT_SIGNALING_PACKAGES[@]}"

  if [[ "${ROLE}" == "cloud" ]]; then
    return
  fi

  output_ref+=("${APT_MEDIA_COMMON_PACKAGES[@]}")

  if is_jetson; then
    append_available_packages output_ref "${APT_JETSON_MEDIA_PACKAGES[@]}"
  elif [[ "$(uname -m)" == "x86_64" || "$(uname -m)" == "amd64" ]]; then
    append_available_packages output_ref "${APT_DRM_MEDIA_PACKAGES[@]}" "${APT_INTEL_MEDIA_PACKAGES[@]}"
    append_intel_media_driver output_ref
  else
    append_available_packages output_ref "${APT_DRM_MEDIA_PACKAGES[@]}"
  fi
}

run_apt() {
  if [[ "${RUN_APT}" == "no" || ! -x /usr/bin/apt-get ]]; then
    return
  fi

  local packages=()
  build_apt_packages packages

  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${packages[@]}"
  elif sudo -n true >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  elif [[ -t 0 ]] && have sudo; then
    echo "Bootstrap needs sudo to install system packages."
    sudo -v
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  else
    local install_cmd
    printf -v install_cmd ' %q' "${packages[@]}"
    install_cmd="sudo apt-get update && sudo apt-get install -y${install_cmd}"
    echo "No passwordless sudo. Run this once if packages are missing:"
    echo "${install_cmd}"
  fi
}

check_ros2_runtime() {
  if [[ "${ROLE}" == "cloud" ]]; then
    return
  fi

  local distro="${ROS_DISTRO:-jazzy}"
  if [[ -f "/opt/ros/${distro}/setup.bash" || -f "/opt/ros/${distro}/local_setup.bash" ]]; then
    return
  fi

  echo "ROS 2 setup file not found for ROS_DISTRO=${distro}." >&2
  echo "Install ROS 2 on this robot/machine, or set ROS_DISTRO to the installed ROS 2 distro before bootstrap." >&2
  echo "Expected: /opt/ros/${distro}/setup.bash" >&2
  return 1
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
  local check_log="${TMPDIR:-/tmp}/horus_connector_${UID}_zenoh_bridge_check.log"
  version="$(latest_zenoh_version)"
  target="$(rust_target)"
  zip="${ROOT}/zenoh-plugin-ros2dds-${version}-${target}-standalone.zip"
  url="https://github.com/eclipse-zenoh/zenoh-plugin-ros2dds/releases/download/${version}/zenoh-plugin-ros2dds-${version}-${target}-standalone.zip"

  echo "Zenoh bridge target: ${version} (${target})"
  if [[ -x "${ROOT}/zenoh-bridge-ros2dds" ]] && "${ROOT}/zenoh-bridge-ros2dds" --version 2>/dev/null | grep -q "${version}"; then
    write_zenoh_profile "binary" "" "eclipse/zenoh-bridge-ros2dds:${version}" ""
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
  if "${ROOT}/zenoh-bridge-ros2dds" --version >"${check_log}" 2>&1; then
    write_zenoh_profile "binary" "" "eclipse/zenoh-bridge-ros2dds:${version}" ""
    "${ROOT}/zenoh-bridge-ros2dds" --version || true
    return
  fi

  local error
  error="$(cat "${check_log}")"
  if configure_zenoh_docker_fallback "${version}" "${error}"; then
    return
  fi

  echo "Zenoh bridge binary is not usable on this host:" >&2
  echo "${error}" >&2
  echo "Install Docker or use a newer OS/JetPack with glibc >= 2.28." >&2
  return 1
}

write_zenoh_profile() {
  local runtime="$1"
  local docker_sudo="$2"
  local image="$3"
  local note="$4"
  {
    echo "# Generated by scripts/bootstrap.sh"
    echo "ZENOH_BRIDGE_RUNTIME='${runtime}'"
    echo "ZENOH_BRIDGE_IMAGE='${image}'"
    echo "ZENOH_BRIDGE_DOCKER_SUDO='${docker_sudo}'"
    echo "ZENOH_BRIDGE_NOTE='${note//\'/}'"
  } > "${ROOT}/.zenoh_profile.env"
}

docker_command_mode() {
  if have docker && docker ps >/dev/null 2>&1; then
    echo "user"
    return 0
  fi
  if have sudo && sudo -n docker ps >/dev/null 2>&1; then
    echo "sudo"
    return 0
  fi
  if have docker && have sudo && [[ ! -t 0 ]]; then
    echo "sudo-unverified"
    return 0
  fi
  if [[ -t 0 ]] && have sudo; then
    echo "Zenoh Docker fallback needs sudo Docker access."
    sudo -v
    if sudo docker ps >/dev/null 2>&1; then
      echo "sudo"
      return 0
    fi
  fi
  return 1
}

configure_zenoh_docker_fallback() {
  local version="$1"
  local error="$2"
  local mode image
  image="eclipse/zenoh-bridge-ros2dds:${version}"
  mode="$(docker_command_mode || true)"
  if [[ -z "${mode}" ]]; then
    return 1
  fi

  echo "Zenoh binary is not compatible with this host; using Docker fallback."
  echo "Binary error: ${error//$'\n'/ }"
  if [[ "${mode}" == "sudo" ]]; then
    if ! sudo -n docker pull "${image}"; then
      image="eclipse/zenoh-bridge-ros2dds:latest"
      sudo -n docker pull "${image}" || return 1
    fi
    write_zenoh_profile "docker" "1" "${image}" "Host binary incompatible; using Docker fallback. Add the user to the docker group for passwordless launches."
  elif [[ "${mode}" == "sudo-unverified" ]]; then
    write_zenoh_profile "docker" "1" "${image}" "Host binary incompatible; using sudo Docker fallback. Run sudo -v before launch, or add the user to the docker group."
    echo "Docker is installed but needs sudo. Configured sudo Docker fallback."
    echo "Before launch, run: sudo -v"
    echo "If the image is not cached yet, run: sudo docker pull ${image}"
  else
    if ! docker pull "${image}"; then
      image="eclipse/zenoh-bridge-ros2dds:latest"
      docker pull "${image}" || return 1
    fi
    write_zenoh_profile "docker" "0" "${image}" "Host binary incompatible; using Docker fallback."
  fi
  echo "Configured Zenoh Docker fallback: ${image}"
  return 0
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
echo "Machine: $(machine_kind) $(uname -m) $(hardware_kind)"

check_ros2_runtime
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
