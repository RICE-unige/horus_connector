#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROLE_ARG="${1:-}"
ROLE=""
RUN_APT="${RUN_APT:-auto}"

# ANSI color helpers
RESET="\033[0m"
BOLD="\033[1m"
FG_GREEN="\033[38;5;48m"
FG_YELLOW="\033[38;5;214m"
FG_RED="\033[38;5;203m"
FG_BLUE="\033[38;5;39m"
FG_CYAN="\033[38;5;51m"
FG_MUTED="\033[38;5;244m"

log_success() {
  echo -e "${FG_GREEN}OK${RESET}      $*"
}
log_info() {
  echo -e "${FG_BLUE}Info${RESET}    $*"
}
log_warn() {
  echo -e "${FG_YELLOW}Warning${RESET} $*"
}
log_error() {
  echo -e "${FG_RED}Error${RESET}   $*" >&2
}
log_run() {
  echo -e "${FG_CYAN}Run${RESET}     $*"
}

if [[ "${ROLE_ARG}" == "-h" || "${ROLE_ARG}" == "--help" ]]; then
  echo -e "\n${BOLD}${FG_CYAN}HORUS Bootstrap${RESET}"
  echo -e "${FG_MUTED}Prepare dependencies for robot, machine, teammate, or cloud roles${RESET}\n"
  echo -e "${BOLD}Usage${RESET}"
  echo -e "  ./scripts/bootstrap.sh ${FG_GREEN}[robot|machine|teammate|cloud|auto]${RESET}\n"
  echo "  ./scripts/bootstrap.sh --zenoh-only   Update only the project-local Zenoh bridge"
  echo -e "${BOLD}What it prepares${RESET}"
  echo -e "  - Zenoh ROS 2 DDS bridge binary or Docker fallback"
  echo -e "  - Python WebRTC and GLib dependencies"
  echo -e "  - GStreamer video path with hardware detection where available\n"
  echo -e "${FG_MUTED}If passwordless sudo is unavailable, bootstrap prints the apt command and continues with user-local setup.${RESET}\n"
  exit 0
fi

have() {
  command -v "$1" >/dev/null 2>&1
}

trim_spaces() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

load_key_value_file() {
  source "${ROOT}/scripts/config_env.sh"
  horus_load_config "$1" python3
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

has_intel_render_device() {
  local device
  for device in /sys/class/drm/renderD*/device/vendor; do
    [[ ! -r "${device}" || "$(<"${device}")" != "0x8086" ]] || return 0
  done
  return 1
}

native_runtime_requested() {
  [[ -n "${HORUS_RUNTIME_BUNDLE:-}${HORUS_RUNTIME_SHA256:-}${HORUS_CLOUD_MEDIA_CONFIG:-}" ||
     "${HORUS_SOURCE_MEDIA_ENABLED:-0}" == 1 ||
     ( -n "${HORUS_MEDIA_RELAY_CONFIG:-}" && "${HORUS_MEDIA_RELAY_ENGINE:-native}" == native ) ]]
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
  iproute2
  openssl
  procps
  unzip
  python3
  python3-pip
  python3-venv
)

APT_SIGNALING_PACKAGES=(
  python3-websocket
  python3-websockets
)

APT_CLOUD_TURN_PACKAGES=(
  coturn
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

  if apt_package_installed intel-media-va-driver && [[ "${HORUS_INTEL_MEDIA_DRIVER:-free}" == "non-free" ]]; then
    log_warn "intel-media-va-driver is already installed; keeping it to avoid a non-free driver conflict."
    packages_ref+=(intel-media-va-driver)
    return
  fi

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

dedupe_packages() {
  local -n packages_ref="$1"
  local deduped=()
  local package seen exists
  for package in "${packages_ref[@]}"; do
    exists=0
    for seen in "${deduped[@]}"; do
      if [[ "${seen}" == "${package}" ]]; then
        exists=1
        break
      fi
    done
    [[ "${exists}" == "0" ]] && deduped+=("${package}")
  done
  packages_ref=("${deduped[@]}")
}

filter_conflicting_packages() {
  local -n packages_ref="$1"
  local filtered=()
  local package
  local free_requested=0
  local nonfree_requested=0
  for package in "${packages_ref[@]}"; do
    [[ "${package}" == "intel-media-va-driver" ]] && free_requested=1
    [[ "${package}" == "intel-media-va-driver-non-free" ]] && nonfree_requested=1
  done
  if [[ "${free_requested}" == "1" && "${nonfree_requested}" == "1" ]]; then
    local keep="intel-media-va-driver"
    if apt_package_installed intel-media-va-driver-non-free; then
      keep="intel-media-va-driver-non-free"
    elif [[ "${HORUS_INTEL_MEDIA_DRIVER:-free}" == "non-free" ]] && ! apt_package_installed intel-media-va-driver; then
      keep="intel-media-va-driver-non-free"
    fi
    log_warn "Both Intel VA drivers are available; selecting ${keep} to avoid apt conflicts."
    for package in "${packages_ref[@]}"; do
      case "${package}" in
        intel-media-va-driver|intel-media-va-driver-non-free)
          [[ "${package}" == "${keep}" ]] && filtered+=("${package}")
          ;;
        *)
          filtered+=("${package}")
          ;;
      esac
    done
    packages_ref=("${filtered[@]}")
  fi
}

build_apt_packages() {
  local -n output_ref="$1"
  output_ref=("${APT_BASE_PACKAGES[@]}")
  append_available_packages output_ref "${APT_SIGNALING_PACKAGES[@]}"
  if [[ "${install_ros:-0}" == 1 ]]; then
    output_ref+=("ros-${ROS_DISTRO}-ros-base")
  fi
  if ros_runtime_needed && [[ "${install_ros:-0}" == 1 || -n "${HORUS_CLOUD_MEDIA_CONFIG:-}" ||
                             "${RMW_IMPLEMENTATION:-}" == rmw_cyclonedds_cpp ]]; then
    output_ref+=("ros-${ROS_DISTRO:-jazzy}-rmw-cyclonedds-cpp")
  fi
  if native_runtime_requested; then
    local abi="406t64"
    [[ "${ROS_DISTRO:-jazzy}" != humble ]] || abi="4.5d"
    output_ref+=("libopencv-core${abi}" "libopencv-imgproc${abi}" "libopencv-imgcodecs${abi}")
  fi

  if [[ "${ROLE}" == "cloud" ]]; then
    if [[ "${HORUS_CLOUD_RUN_TURN:-0}" == "1" ]]; then
      append_available_packages output_ref "${APT_CLOUD_TURN_PACKAGES[@]}"
    fi
    if native_runtime_requested; then
      append_available_packages output_ref "${APT_MEDIA_COMMON_PACKAGES[@]}"
    fi
    dedupe_packages output_ref
    return
  fi
  if [[ "${ROLE}" == "teammate" ]]; then
    if [[ "${FIELD_TEAMMATE_RAW_IMAGE:-0}" == "1" ]]; then
      append_available_packages output_ref python3-opencv python3-numpy
    fi
    dedupe_packages output_ref
    return
  fi

  append_available_packages output_ref "${APT_MEDIA_COMMON_PACKAGES[@]}"

  if is_jetson; then
    log_info "Using JetPack's installed codec stack; bootstrap will not alter L4T packages."
  elif has_intel_render_device; then
    append_available_packages output_ref "${APT_DRM_MEDIA_PACKAGES[@]}" "${APT_INTEL_MEDIA_PACKAGES[@]}"
    append_intel_media_driver output_ref
  else
    append_available_packages output_ref "${APT_DRM_MEDIA_PACKAGES[@]}"
  fi
  filter_conflicting_packages output_ref
  dedupe_packages output_ref
}

run_apt() {
  if [[ "${RUN_APT}" == "no" || ! -x /usr/bin/apt-get ]]; then
    return
  fi

  local packages=()
  apt_update() {
    local runner=("$@")
    if ! "${runner[@]}" apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; then
      log_warn "apt-get update failed. Continuing with the existing package cache."
      log_warn "If package installation fails, fix the broken apt source shown above and rerun bootstrap."
      return 0
    fi
  }

  apt_install() {
    local runner=("$@")
    if [[ "${install_ros:-0}" == 1 ]] && ! apt_package_available "ros-${ROS_DISTRO}-ros-base"; then
      log_error "ROS ${ROS_DISTRO} is not available from your apt sources. Configure the official ROS 2 apt repository, then rerun bootstrap."
      return 1
    fi
    "${runner[@]}" apt-get -o DPkg::Lock::Timeout=60 -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y "${packages[@]}"
  }

  if [[ "$(id -u)" -eq 0 ]]; then
    apt_update
    build_apt_packages packages
    apt_install
  elif sudo -n true >/dev/null 2>&1; then
    apt_update sudo
    build_apt_packages packages
    apt_install sudo
  elif [[ -t 0 ]] && have sudo; then
    log_warn "Bootstrap needs sudo to install system packages."
    sudo -v
    apt_update sudo
    build_apt_packages packages
    apt_install sudo
  else
    local install_cmd
    build_apt_packages packages
    printf -v install_cmd ' %q' "${packages[@]}"
    install_cmd="sudo apt-get -o Acquire::ForceIPv4=true update; sudo apt-get install -y${install_cmd}"
    log_warn "No passwordless sudo. Run this once if packages are missing:"
    echo -e "  ${FG_CYAN}${install_cmd}${RESET}"
    [[ "${install_ros:-0}" == 0 ]] || return 1
  fi
}

ros_runtime_needed() {
  [[ "${ROLE}" != cloud || -n "${HORUS_CLOUD_MEDIA_CONFIG:-}" ]]
}

check_ros2_runtime() {
  if ros_runtime_needed; then
    horus_ros_environment
  fi
}

resolve_zenoh_version() {
  if [[ -n "${ZENOH_VERSION:-}" ]]; then
    echo "${ZENOH_VERSION}"
    return
  fi

  python3 "${SCRIPT_DIR}/zenoh_release.py" --default-version
}

rust_target() {
  case "$(uname -m)" in
    x86_64|amd64) echo "x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) echo "aarch64-unknown-linux-gnu" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; return 1 ;;
  esac
}

zenoh_binary_matches() {
  local output line
  output="$(timeout 10 "$1" --version 2>/dev/null)" || return 1
  # Upstream may log to stdout before printing its standalone version line.
  while IFS= read -r line; do
    if [[ "${line}" =~ ^zenoh-bridge-ros2dds[[:space:]]v?([0-9]+\.[0-9]+\.[0-9]+)($|[[:space:]]) ]]; then
      [[ "${BASH_REMATCH[1]}" == "$2" ]]
      return
    fi
  done <<< "${output}"
  return 1
}

install_zenoh() (
  local version target archive zip url expected_sha artifact staging="" backup="" updating=0
  local temp_zip="" install_lock file glibc compat_key=""
  exec {install_lock}>"${ROOT}/.zenoh-install.lock"
  flock -w 30 "${install_lock}" || { log_error "Another Zenoh installation is in progress."; return 1; }
  cleanup_zenoh_install() {
    local result="$?"
    if [[ "${result}" != "0" && "${updating}" == "1" ]]; then
      for file in zenoh-bridge-ros2dds libzenoh_plugin_ros2dds.so .zenoh_profile.env; do
        if [[ -f "${backup}/${file}" ]]; then
          cp -p -- "${backup}/${file}" "${ROOT}/${file}" || log_error "Restore ${file} from ${backup} manually."
        else
          rm -f -- "${ROOT}/${file}"
        fi
      done
      log_error "Zenoh update failed; restored the previous installation from ${backup}."
    fi
    [[ -z "${temp_zip}" ]] || rm -f -- "${temp_zip}"
    [[ -z "${staging}" ]] || rm -rf -- "${staging}"
  }
  trap cleanup_zenoh_install EXIT
  version="$(resolve_zenoh_version)"
  target="$(rust_target)"

  log_info "Zenoh bridge target: ${version} (${target})"
  if [[ -x "${ROOT}/zenoh-bridge-ros2dds" && -f "${ROOT}/libzenoh_plugin_ros2dds.so" ]] &&
      zenoh_binary_matches "${ROOT}/zenoh-bridge-ros2dds" "${version}"; then
    write_zenoh_profile "binary" "" "eclipse/zenoh-bridge-ros2dds:${version}" "" || return 1
    log_success "Zenoh bridge already installed."
    return
  fi

  if ! have curl || ! have unzip || ! have sha256sum || ! have python3 || ! have timeout; then
    log_error "curl, unzip, sha256sum, python3, and timeout are required to install Zenoh automatically."
    return 1
  fi

  glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
  artifact="$(python3 "${SCRIPT_DIR}/zenoh_release.py" --version "${version}" --target "${target}" --glibc "${glibc}" \
    --download-base "${ZENOH_DOWNLOAD_BASE:-}")" || return 1
  IFS=$'\t' read -r archive url expected_sha <<< "${artifact}"
  zip="${ROOT}/${archive}"
  temp_zip="$(mktemp "${ROOT}/.zenoh-download.XXXXXX")"
  curl -fL --retry 3 --retry-max-time 300 --max-time 180 --connect-timeout 20 \
    --speed-limit 1024 --speed-time 30 -o "${temp_zip}" "${url}" || return 1
  if ! printf '%s  %s\n' "${expected_sha}" "${temp_zip}" | sha256sum -c -; then
    log_error "Zenoh archive checksum failed; existing installation was not changed."
    return 1
  fi
  mv -- "${temp_zip}" "${zip}"
  temp_zip=""
  staging="$(mktemp -d "${ROOT}/.zenoh-install.XXXXXX")"
  if ! unzip -q "${zip}" -d "${staging}"; then
    return 1
  fi
  if [[ ! -f "${staging}/zenoh-bridge-ros2dds" || ! -f "${staging}/libzenoh_plugin_ros2dds.so" ]]; then
    log_error "Zenoh archive is missing its standalone binary or matching plugin."
    return 1
  fi
  chmod +x "${staging}/zenoh-bridge-ros2dds"
  if [[ "${archive}" == *-x86_64-unknown-linux-musl-standalone.zip ]]; then
    log_info "Using the project-local Zenoh compatibility runtime (${glibc})."
    compat_key="$(python3 "${SCRIPT_DIR}/zenoh_release.py" --stage-compat-runtime "${staging}")" || return 1
  fi
  local output
  if output="$(timeout 10 "${staging}/zenoh-bridge-ros2dds" --version 2>&1)"; then
    if ! zenoh_binary_matches "${staging}/zenoh-bridge-ros2dds" "${version}"; then
      log_error "Downloaded binary does not report the requested Zenoh version ${version}: ${output}"
      return 1
    fi
    if [[ -n "${compat_key}" ]]; then
      # An existing wrapper and its rollback copy keep using their exact pinned runtime.
      local compat_source="${staging}/.zenoh-compat/${compat_key}"
      local compat_dest="${ROOT}/.zenoh-compat/${compat_key}"
      mkdir -p "${ROOT}/.zenoh-compat" || return 1
      if [[ -d "${compat_dest}" ]]; then
        for file in bridge ld-musl-x86_64.so.1 libgcc_s.so.1; do
          mv -f -- "${compat_source}/${file}" "${compat_dest}/${file}" || return 1
        done
      else
        mv -T -- "${compat_source}" "${compat_dest}" || return 1
      fi
    fi
    mkdir -p "${ROOT}/.zenoh-backups"
    backup="$(mktemp -d "${ROOT}/.zenoh-backups/$(date -u '+%Y%m%dT%H%M%SZ').XXXXXX")"
    for file in zenoh-bridge-ros2dds libzenoh_plugin_ros2dds.so .zenoh_profile.env; do
      [[ ! -f "${ROOT}/${file}" ]] || cp -p -- "${ROOT}/${file}" "${backup}/${file}"
    done
    updating=1
    mv -- "${staging}/libzenoh_plugin_ros2dds.so" "${ROOT}/libzenoh_plugin_ros2dds.so" || return 1
    mv -- "${staging}/zenoh-bridge-ros2dds" "${ROOT}/zenoh-bridge-ros2dds" || return 1
    write_zenoh_profile "binary" "" "eclipse/zenoh-bridge-ros2dds:${version}" "" || return 1
    updating=0
    log_success "Installed Zenoh ${version}; rollback files: ${backup}"
    "${ROOT}/zenoh-bridge-ros2dds" --version
    return
  fi

  if configure_zenoh_docker_fallback "${version}" "${output}"; then
    return
  fi

  log_error "Zenoh bridge binary is not usable on this host:"
  printf '%s\n' "${output}" >&2
  if [[ "${output}" =~ (GLIBC_[0-9]+(\.[0-9]+)+) ]]; then
    local required_glibc="${BASH_REMATCH[1]#GLIBC_}"
    log_warn "The downloaded Zenoh build requires glibc ${required_glibc}; this environment reports ${glibc:-unknown}." >&2
  fi
  log_warn "Use a compatible Zenoh ${version} build for this environment. Docker fallback requires the Docker CLI and access to its daemon." >&2
  return 1
)

write_zenoh_profile() {
  local runtime="$1"
  local docker_sudo="$2"
  local image="$3"
  local note="$4"
  local profile
  profile="$(mktemp "${ROOT}/.zenoh-profile.XXXXXX")" || return 1
  if ! {
    echo "# Generated by scripts/bootstrap.sh"
    echo "ZENOH_BRIDGE_RUNTIME='${runtime}'"
    echo "ZENOH_BRIDGE_IMAGE='${image}'"
    echo "ZENOH_BRIDGE_VERSION='${image##*:}'"
    echo "ZENOH_BRIDGE_DOCKER_SUDO='${docker_sudo}'"
    echo "ZENOH_BRIDGE_NOTE='${note//\'/}'"
  } > "${profile}"; then
    rm -f -- "${profile}"
    return 1
  fi
  if ! mv -- "${profile}" "${ROOT}/.zenoh_profile.env"; then
    rm -f -- "${profile}"
    return 1
  fi
}

docker_command_mode() {
  have docker || return 1
  if timeout 10 docker ps >/dev/null 2>&1; then
    echo "user"
    return 0
  fi
  if have sudo && timeout 10 sudo -n docker ps >/dev/null 2>&1; then
    echo "sudo"
    return 0
  fi
  if [[ -t 0 ]] && have sudo; then
    echo "Zenoh Docker fallback needs sudo Docker access." >&2
    if sudo -v && timeout 10 sudo -n docker ps >/dev/null 2>&1; then
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

  log_warn "Zenoh binary is not compatible with this host; using Docker fallback."
  log_warn "Binary error: ${error//$'\n'/ }"
  if [[ "${mode}" == "sudo" ]]; then
    timeout "${HORUS_DOCKER_PULL_TIMEOUT:-300}" sudo -n docker pull "${image}" || return 1
    write_zenoh_profile "docker" "1" "${image}" "Host binary incompatible; using Docker fallback with sudo." || return 1
  else
    timeout "${HORUS_DOCKER_PULL_TIMEOUT:-300}" docker pull "${image}" || return 1
    write_zenoh_profile "docker" "0" "${image}" "Host binary incompatible; using Docker fallback." || return 1
  fi
  log_success "Configured Zenoh Docker fallback: ${image}"
  return 0
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

if [[ "${ROLE_ARG}" == "--zenoh-only" ]]; then
  install_zenoh
  exit
fi
source "${SCRIPT_DIR}/ros_environment.sh"
source "${SCRIPT_DIR}/managed_runtime.sh"

cd "${ROOT}"
config_file="${HORUS_ENV:-${ROOT}/.env}"
if [[ ! -f "${config_file}" ]]; then
  [[ -z "${HORUS_ENV:-}" ]] || { log_error "Missing selected configuration: ${config_file}"; exit 1; }
  cp .env.example "${config_file}"
  log_success "Created new config: ${BOLD}.env${RESET} (from .env.example)"
fi

requested_ros_install="${HORUS_INSTALL_ROS-}"
load_key_value_file "${config_file}"
if [[ -f .zenoh_tls_profile.env ]]; then
  load_key_value_file .zenoh_tls_profile.env
fi
if [[ -n "${requested_ros_install}" ]]; then
  HORUS_INSTALL_ROS="${requested_ros_install}"
fi
case "${HORUS_INSTALL_ROS:-0}" in
  0|1) ;;
  *) log_error "HORUS_INSTALL_ROS must be 0 or 1."; exit 2 ;;
esac
runtime_archive="${HORUS_RUNTIME_BUNDLE:-}"
if [[ -n "${runtime_archive}${HORUS_RUNTIME_SHA256:-}" ]]; then
  runtime_archive="${runtime_archive/#\~/${HOME}}"
  if [[ ! -f "${runtime_archive}" || ! -r "${runtime_archive}" ||
        ! "${HORUS_RUNTIME_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]; then
    log_error "Set HORUS_RUNTIME_BUNDLE to a readable archive and HORUS_RUNTIME_SHA256 to its published lowercase SHA-256."
    exit 1
  fi
fi

if [[ -n "${ROLE_ARG}" && "${ROLE_ARG}" != "auto" ]]; then
  ROLE="${ROLE_ARG}"
else
  ROLE="${HORUS_ROLE:-machine}"
fi

if [[ -z "${ROLE}" || "${ROLE}" == "auto" ]]; then
  ROLE="machine"
fi

case "${ROLE}" in
  robot|machine|teammate|cloud) ;;
  *)
    log_error "Unknown role: ${ROLE}"
    log_warn "Supported roles: robot, machine, teammate, cloud, auto"
    exit 2
    ;;
esac

echo -e "\n${BOLD}${FG_CYAN}HORUS Bootstrap${RESET}"
echo -e "  role       ${ROLE}"
echo -e "  machine    $(machine_kind) ($(uname -m), $(hardware_kind))"
echo ""

install_ros=0
if ros_runtime_needed; then
  if [[ -z "${ROS_DISTRO:-}" ]]; then
    ROS_DISTRO="$(horus_default_ros_distro)"
    export ROS_DISTRO
  fi
  if [[ "${HORUS_INSTALL_ROS:-0}" == 1 && -z "${ROS_SETUP_PATH:-}" &&
        ! -r "$(horus_ros_setup_file)" ]]; then
    release_id="$(. /etc/os-release; printf '%s/%s' "${ID:-}" "${VERSION_ID:-}")"
    [[ "${release_id}/${ROS_DISTRO}" =~ ^ubuntu/(22[.]04/humble|24[.]04/jazzy)$ &&
       "${RUN_APT}" != no && -x /usr/bin/apt-get ]] || {
      log_error "ROS installation needs apt enabled on Ubuntu 22.04/Humble or 24.04/Jazzy. Otherwise install ROS separately and set ROS_SETUP_PATH."
      exit 1
    }
    install_ros=1
  else
    check_ros2_runtime
  fi
fi
run_apt
check_ros2_runtime
zenoh_quic_requested() {
  case "${ZENOH_TRANSPORT:-auto}" in
    quic) return 0 ;;
    auto) [[ "${ZENOH_AUTO_ENABLE_QUIC:-0}" == "1" ]] && return 0 ;;
  esac
  return 1
}

prepare_zenoh_tls() {
  zenoh_quic_requested || return 0
  case "${ROLE}:${HORUS_TOPOLOGY:-hub}" in
    cloud:*|machine:direct)
      bash "${SCRIPT_DIR}/setup_zenoh_tls.sh" server
      ;;
    robot:*|machine:hub|teammate:*)
      if [[ -n "${ZENOH_TLS_ROOT_CA:-}" && -f "${ZENOH_TLS_ROOT_CA/#\~/${HOME}}" ]]; then
        log_success "Zenoh QUIC root certificate is installed."
      elif [[ "${ZENOH_TRANSPORT:-auto}" == "quic" ]]; then
        log_error "ZENOH_TRANSPORT=quic requires the hub/listener public cert on this machine."
        log_warn "Run: ./horus quic install-cert <cert.pem>"
        return 1
      else
        log_warn "Zenoh QUIC is enabled, but no root certificate is installed yet."
        log_warn "Run './horus quic install-cert <cert.pem>' to activate QUIC; TCP fallback will be used until then."
      fi
      ;;
  esac
}

prepare_zenoh_tls
if [[ "${ROLE}" == "cloud" && "${HORUS_CLOUD_RUN_TURN:-0}" == "1" ]]; then
  bash "${SCRIPT_DIR}/setup_turn.sh"
fi
if [[ -n "${HORUS_RUNTIME_BUNDLE:-}${HORUS_RUNTIME_SHA256:-}" ]]; then
  horus_use_runtime_bundle install
elif [[ "${HORUS_ZENOH_SESSION_ISOLATION:-0}" != 1 && -z "${ZENOH_BRIDGE_BINARY:-}" ]]; then
  install_zenoh
elif [[ ! -x "${ZENOH_BRIDGE_BINARY:-${ROOT}/zenoh-bridge-ros2dds}" ]]; then
  log_error "Selected Zenoh profile requires the pinned native binary. Install a verified HORUS runtime bundle."
  exit 1
fi
"${SCRIPT_DIR}/setup_webrtc.sh" "${ROLE}" --ros-prepared
media_python="${WEBRTC_VENV:-${ROOT}/.venv-webrtc}/bin/python"
if [[ "${HORUS_ZENOH_SESSION_ISOLATION:-0}" == 1 ]]; then
  "${media_python}" - "${SCRIPT_DIR}" "${ZENOH_BRIDGE_BINARY:-${ROOT}/zenoh-bridge-ros2dds}" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from session_lanes import check_binary
try:
    check_binary(Path(sys.argv[2]))
except (OSError, ValueError) as error:
    raise SystemExit(f"Invalid independent-lane runtime: {error}")
PY
fi
if native_runtime_requested; then
  native_checks=()
  if [[ "${ROLE}" == robot && "${HORUS_SOURCE_MEDIA_ENABLED:-0}" == 1 ||
        "${ROLE}" == cloud && -n "${HORUS_CLOUD_MEDIA_CONFIG:-}" ]]; then
    native_checks+=("${HORUS_SOURCE_MEDIA_BINARY:-$(command -v horus_media_node || true)}")
  fi
  if [[ "${ROLE}" == machine && -n "${HORUS_MEDIA_RELAY_CONFIG:-}" && "${HORUS_MEDIA_RELAY_ENGINE:-native}" == native ]]; then
    native_checks+=("${HORUS_MEDIA_RELAY_BINARY:-$(command -v horus_unity_bridge_node || true)}")
  fi
  for binary in "${native_checks[@]}"; do
    libs=""
    if [[ ! -x "${binary}" ]] || ! libs="$(LC_ALL=C ldd "${binary}" 2>&1)" ||
       [[ "${libs}" == *"not found"* ]]; then
      log_error "Native media runtime cannot run on this ROS installation: ${binary:-missing binary}."
      if [[ -n "${libs:-}" ]]; then printf '%s\n' "${libs}" >&2; fi
      exit 1
    fi
  done
  if [[ -n "${HORUS_RUNTIME_SHA256:-}" ]]; then
    GST_PLUGIN_PATH_1_0="$(horus_native_plugin_path)" \
      gst-inspect-1.0 rtpgccbwe >/dev/null || { log_error "Installed bundle is missing usable GCC pacing."; exit 1; }
  fi
fi
if [[ "${ROLE}" == "cloud" || "${ROLE}" == "teammate" ]]; then
  log_info "${ROLE} role does not require an H.264 encoder or decoder."
else
  media_python="${WEBRTC_VENV:-${ROOT}/.venv-webrtc}/bin/python"
  probe_args=()
  if [[ "${WEBRTC_MEDIA_MODE:-h264}" == h264 ]]; then
    probe_args=(--root "${ROOT}" --profile "${ROOT}/.webrtc_profile.env")
  fi
  "${media_python}" "${SCRIPT_DIR}/media_runtime.py" --role "${ROLE}" --need-ros \
    --mode "${WEBRTC_MEDIA_MODE:-h264}" "${probe_args[@]}"
  if [[ ( "${ROLE}" == "robot" || "${ROLE}" == "machine" ) && "${WEBRTC_MEDIA_MODE:-h264}" == "h264" ]]; then
    if ! command -v gst-inspect-1.0 >/dev/null 2>&1 || \
       ! gst-inspect-1.0 webrtcbin >/dev/null 2>&1 || \
       ! gst-inspect-1.0 nice >/dev/null 2>&1; then
      log_error "H.264 WebRTC media is incomplete: GStreamer webrtcbin and nice are required."
      log_warn "Install gstreamer1.0-plugins-bad and gstreamer1.0-nice, then rerun bootstrap."
      exit 1
    fi
  fi
fi

echo ""
log_success "Bootstrap complete."
echo -e "${BOLD}Next commands${RESET}"
echo -e "  ${FG_CYAN}./horus setup${RESET}        ${FG_MUTED}optional, to review configuration${RESET}"
echo -e "  ${FG_CYAN}./horus launch ${ROLE}${RESET}\n"
