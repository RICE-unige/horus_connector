#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROLE_ARG="${1:-}"
ROLE=""
RUN_APT="${RUN_APT:-auto}"
ZENOH_ONLY=0
PREPARE_CAMERA=0
PREPARE_DECODER=0
PREPARE_SIGNALING=0

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
  echo -e "  ./horus bootstrap ${FG_GREEN}[robot|machine|teammate|cloud|auto]${RESET} [--zenoh-only]\n"
  echo "  ./scripts/bootstrap.sh --zenoh-only   Update only the project-local Zenoh bridge"
  echo -e "${BOLD}What it prepares${RESET}"
  echo -e "  - Zenoh ROS 2 DDS bridge binary or Docker fallback"
  echo -e "  - Python and video runtime dependencies for enabled features"
  echo -e "  - Existing hardware codec detection, without installing GPU drivers\n"
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

native_media_needed() {
  [[ "${ZENOH_ONLY}" == 0 ]] || return 1
  case "${ROLE}" in
    robot) [[ "${HORUS_SOURCE_MEDIA_ENABLED:-0}" == 1 ]] ;;
    machine) [[ -n "${HORUS_MEDIA_RELAY_CONFIG:-}" && "${HORUS_MEDIA_RELAY_ENGINE:-native}" == native ]] ;;
    cloud) [[ -n "${HORUS_CLOUD_MEDIA_CONFIG:-}" ]] ;;
    *) return 1 ;;
  esac
}

select_bootstrap_features() {
  PREPARE_CAMERA=0 PREPARE_DECODER=0 PREPARE_SIGNALING=0
  [[ "${ZENOH_ONLY}" == 0 ]] || return 0
  case "${ROLE}" in
    cloud)
      if [[ "${HORUS_CLOUD_RUN_SIGNALING:-1}" == 1 ]]; then PREPARE_SIGNALING=1; fi
      ;;
    teammate)
      PREPARE_SIGNALING=1
      ;;
    robot|machine)
      case "${WEBRTC_MEDIA_MODE:-h264}" in
        h264)
          local features
          features="$(python3 - "${SCRIPT_DIR}" "${ROOT}" "${ROLE}" <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from webrtc_streams import load_streams

try:
    streams = load_streams(Path(sys.argv[2]), sys.argv[3])
except (OSError, ValueError) as exc:
    raise SystemExit(f"Invalid WebRTC streams config: {exc}")
print(int(bool(streams)), int(any(stream["video_source"] != "ros2" and stream["source_pipeline"] for stream in streams)))
PY
)" || return 1
          read -r PREPARE_CAMERA PREPARE_DECODER <<< "${features}"
          if [[ "${PREPARE_CAMERA}" == 1 && "${ROLE}" == machine ]]; then PREPARE_DECODER=1; fi
          ;;
        jpeg)
          if [[ "${HORUS_ALLOW_LEGACY_JPEG:-0}" != 1 ]]; then
            log_error "WEBRTC_MEDIA_MODE=jpeg is legacy-only. Use h264 or set HORUS_ALLOW_LEGACY_JPEG=1."
            return 1
          fi
          PREPARE_CAMERA=1
          ;;
        *)
          log_error "WEBRTC_MEDIA_MODE must be h264 or jpeg."
          return 1
          ;;
      esac
      PREPARE_SIGNALING="${PREPARE_CAMERA}"
      ;;
  esac
}

zenoh_quic_requested() {
  case "${ZENOH_TRANSPORT:-auto}" in
    quic) return 0 ;;
    auto) [[ "${ZENOH_AUTO_ENABLE_QUIC:-0}" == 1 ]] ;;
    *) return 1 ;;
  esac
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
  procps
  unzip
  python3
  python3-venv
)

APT_H264_PACKAGES=(
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly
)

APT_PYTHON_VIDEO_PACKAGES=(
  gstreamer1.0-nice
  python3-gi
  python3-gst-1.0
  gir1.2-gstreamer-1.0
  gir1.2-gst-plugins-base-1.0
  gir1.2-gst-plugins-bad-1.0
)

apt_package_available() {
  apt-cache show "$1" >/dev/null 2>&1
}

apt_package_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

build_apt_packages() {
  local -n output_ref="$1"
  output_ref=("${APT_BASE_PACKAGES[@]}")
  if [[ "${install_ros:-0}" == 1 ]]; then
    output_ref+=("ros-${ROS_DISTRO}-ros-base")
  fi
  if ros_runtime_needed && [[ "${install_ros:-0}" == 1 || "${ROLE}" == cloud ||
                             "${RMW_IMPLEMENTATION:-}" == rmw_cyclonedds_cpp ]]; then
    output_ref+=("ros-${ROS_DISTRO:-jazzy}-rmw-cyclonedds-cpp")
  fi
  if [[ "${HORUS_ZENOH_ENABLED:-1}" != 0 ]] && zenoh_quic_requested &&
     [[ "${ROLE}" == cloud || ( "${ROLE}" == machine && "${HORUS_TOPOLOGY:-hub}" == direct ) ]]; then
    output_ref+=(openssl)
  fi
  if [[ "${ZENOH_ONLY}" == 0 && "${ROLE}" == cloud && "${HORUS_CLOUD_RUN_TURN:-0}" == 1 ]]; then
    output_ref+=(coturn)
  fi
  if [[ "${ZENOH_ONLY}" == 0 && "${ROLE}" == teammate && "${FIELD_TEAMMATE_RAW_IMAGE:-0}" == 1 ]]; then
    output_ref+=(python3-opencv python3-numpy)
  fi

  local native=0 python_video=0
  if native_media_needed; then
    native=1
    local abi="406t64"
    [[ "${ROS_DISTRO:-jazzy}" != humble ]] || abi="4.5d"
    output_ref+=("libopencv-core${abi}" "libopencv-imgproc${abi}" "libopencv-imgcodecs${abi}")
  fi
  if [[ "${PREPARE_CAMERA}" == 1 && "${WEBRTC_MEDIA_MODE:-h264}" == h264 ]]; then python_video=1; fi
  if [[ "${native}" == 1 || "${python_video}" == 1 ]]; then output_ref+=("${APT_H264_PACKAGES[@]}"); fi
  if [[ "${python_video}" == 1 ]]; then output_ref+=("${APT_PYTHON_VIDEO_PACKAGES[@]}"); fi
  # A machine's decoder probe uses x264, which is provided by the base H.264 selection.
  if [[ "${native}" == 1 || "${PREPARE_DECODER}" == 1 ]]; then output_ref+=(gstreamer1.0-libav); fi
}

run_apt() {
  if [[ "${RUN_APT}" == "no" || ! -x /usr/bin/apt-get ]]; then
    return
  fi

  local packages=() missing=() package
  build_apt_packages packages
  for package in "${packages[@]}"; do
    apt_package_installed "${package}" || missing+=("${package}")
  done
  if [[ "${#missing[@]}" == 0 ]]; then
    log_info "Runtime packages are already installed; skipping APT."
    return 0
  fi
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
    "${runner[@]}" apt-get -o DPkg::Lock::Timeout=60 -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends "${missing[@]}"
  }

  if [[ "$(id -u)" -eq 0 ]]; then
    apt_update
    apt_install
  elif sudo -n true >/dev/null 2>&1; then
    apt_update sudo
    apt_install sudo
  elif [[ -t 0 ]] && have sudo; then
    log_warn "Bootstrap needs sudo to install system packages."
    sudo -v
    apt_update sudo
    apt_install sudo
  else
    local install_cmd
    printf -v install_cmd ' %q' "${missing[@]}"
    install_cmd="sudo apt-get -o Acquire::ForceIPv4=true update && sudo apt-get install -y --no-install-recommends${install_cmd}"
    log_warn "No passwordless sudo. Run this once if packages are missing:"
    echo -e "  ${FG_CYAN}${install_cmd}${RESET}"
    [[ "${install_ros:-0}" == 0 ]] || return 1
  fi
}

ros_runtime_needed() {
  [[ "${ROLE}" != cloud ]] || native_media_needed
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
  [[ "$#" == 1 ]] || { log_error "Unexpected options for the Zenoh update."; exit 2; }
  install_zenoh
  exit
fi
for option in "${@:2}"; do
  case "${option}" in
    --zenoh-only) ZENOH_ONLY=1 ;;
    *) log_error "Unknown bootstrap option: ${option}"; exit 2 ;;
  esac
done
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

if [[ "${ZENOH_ONLY}" == 1 && "${HORUS_ZENOH_ENABLED:-1}" == 0 ]]; then
  log_error "--zenoh-only requires Zenoh to be enabled in the selected profile."
  exit 2
fi
select_bootstrap_features

echo -e "\n${BOLD}${FG_CYAN}HORUS Bootstrap${RESET}"
echo -e "  role       ${ROLE}"
echo -e "  machine    $(machine_kind) ($(uname -m), $(hardware_kind))"
if [[ "${ZENOH_ONLY}" == 1 ]]; then
  echo "  services   Zenoh only"
fi
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

prepare_zenoh_tls() {
  [[ "${HORUS_ZENOH_ENABLED:-1}" != 0 ]] || return 0
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
if [[ "${ZENOH_ONLY}" == 0 && "${ROLE}" == cloud && "${HORUS_CLOUD_RUN_TURN:-0}" == 1 ]]; then
  bash "${SCRIPT_DIR}/setup_turn.sh"
fi
if [[ -n "${HORUS_RUNTIME_BUNDLE:-}${HORUS_RUNTIME_SHA256:-}" ]]; then
  horus_use_runtime_bundle install
elif [[ "${HORUS_ZENOH_ENABLED:-1}" == 0 ]]; then
  log_info "Zenoh is disabled; skipping bridge installation."
elif [[ "${HORUS_ZENOH_SESSION_ISOLATION:-0}" != 1 && -z "${ZENOH_BRIDGE_BINARY:-}" ]]; then
  install_zenoh
elif [[ ! -x "${ZENOH_BRIDGE_BINARY:-${ROOT}/zenoh-bridge-ros2dds}" ]]; then
  log_error "Selected Zenoh profile requires the pinned native binary. Install a verified HORUS runtime bundle."
  exit 1
fi
setup_args=(--ros-prepared)
if [[ "${PREPARE_CAMERA}" == 1 && "${WEBRTC_MEDIA_MODE:-h264}" == h264 ]]; then
  setup_args+=(--probe-camera)
elif [[ "${PREPARE_SIGNALING}" == 0 ]]; then
  setup_args+=(--routing-only)
fi
"${SCRIPT_DIR}/setup_webrtc.sh" "${ROLE}" "${setup_args[@]}"
media_python="${WEBRTC_VENV:-${ROOT}/.venv-webrtc}/bin/python"
if [[ "${HORUS_ZENOH_ENABLED:-1}" != 0 && "${HORUS_ZENOH_SESSION_ISOLATION:-0}" == 1 ]]; then
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
if native_media_needed; then
  native_checks=()
  case "${ROLE}" in
    robot|cloud) native_checks+=("${HORUS_SOURCE_MEDIA_BINARY:-$(command -v horus_media_node || true)}") ;;
    machine) native_checks+=("${HORUS_MEDIA_RELAY_BINARY:-$(command -v horus_unity_bridge_node || true)}") ;;
  esac
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

echo ""
log_success "Bootstrap complete."
echo -e "${BOLD}Next commands${RESET}"
echo -e "  ${FG_CYAN}./horus setup${RESET}        ${FG_MUTED}optional, to review configuration${RESET}"
launch_suffix=""
if [[ "${ZENOH_ONLY}" == 1 ]]; then launch_suffix=" --zenoh-only"; fi
echo -e "  ${FG_CYAN}./horus launch ${ROLE}${launch_suffix}${RESET}\n"
