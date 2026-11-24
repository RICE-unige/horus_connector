#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ_FILE="${REPO_ROOT}/requirements-media.txt"
VENV_DIR="${WEBRTC_VENV:-${REPO_ROOT}/.venv-webrtc}"
ROLE="${1:-${HORUS_ROLE:-}}"
[[ -n "${ROLE}" && "${ROLE}" != auto ]] || ROLE="${HORUS_ROLE:-machine}"
MEDIA_MODE="${WEBRTC_MEDIA_MODE:-h264}"
ros_prepared=0
routing_only=0
probe_camera=0
for option in "${@:2}"; do
  case "${option}" in
    --ros-prepared) ros_prepared=1 ;;
    --routing-only) routing_only=1 ;;
    --probe-camera) probe_camera=1 ;;
    *) echo "Unknown setup option: ${option}" >&2; exit 2 ;;
  esac
done
BOOTSTRAP_PYTHON="${HORUS_BOOTSTRAP_PYTHON:-/usr/bin/python3}"
command -v "${BOOTSTRAP_PYTHON}" >/dev/null || BOOTSTRAP_PYTHON=python3
if ! "${BOOTSTRAP_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "HORUS connector requires Python 3.10 or newer. Select a compatible interpreter before bootstrap." >&2
  exit 1
fi
if [[ "${ROLE}" != cloud && "${ros_prepared}" == 0 ]]; then
  source "${SCRIPT_DIR}/ros_environment.sh"
  horus_ros_environment
fi
PIP_INDEX_ARGS=()

if [[ "${routing_only}" == 1 ]]; then
  REQ_FILE="${REPO_ROOT}/requirements-core.txt"
fi
if [[ ! -f "${REQ_FILE}" ]]; then
  echo "Missing ${REQ_FILE}" >&2
  exit 1
fi

select_pip_args() {
  if [[ "${routing_only}" == 1 || "${ROLE}" == cloud || "${ROLE}" == teammate ]]; then
    PIP_ARGS=(-r "${REQ_FILE}")
    return
  fi

  if [[ "${MEDIA_MODE}" == jpeg && "${HORUS_ALLOW_LEGACY_JPEG:-0}" != 1 ]]; then
    echo "WEBRTC_MEDIA_MODE=jpeg is legacy-only and disabled by default during bootstrap."
    echo "Select WEBRTC_MEDIA_MODE=h264, or explicitly set HORUS_ALLOW_LEGACY_JPEG=1 for the compatibility runtime." >&2
    return 1
  fi

  if [[ "${MEDIA_MODE}" == "jpeg" ]]; then
    PIP_ARGS=(-r "${REQ_FILE}" -r "${REPO_ROOT}/requirements.txt")
    return
  fi

  PIP_ARGS=(-r "${REQ_FILE}")
}

select_pip_index() {
  if [[ -n "${HORUS_PIP_INDEX_URL:-}" ]]; then
    PIP_INDEX_ARGS=(--index-url "${HORUS_PIP_INDEX_URL}")
  elif [[ "${PIP_INDEX_URL:-}" == *"nvidia"* || "${PIP_EXTRA_INDEX_URL:-}" == *"nvidia"* ]]; then
    echo "Detected NVIDIA pip index; using PyPI for connector Python dependencies."
    PIP_INDEX_ARGS=(--index-url "https://pypi.org/simple")
  fi
}

install_python_deps() {
  local python_cmd="$1"
  shift
  "${python_cmd}" -m pip install --disable-pip-version-check --timeout 30 --retries 2 "${PIP_INDEX_ARGS[@]}" "$@"
}

select_pip_args
select_pip_index

if [[ "${routing_only}" == 1 ]]; then
  echo "Preparing routing dependencies only."
fi

if [[ ! -r "${VENV_DIR}/pyvenv.cfg" || ! -x "${VENV_DIR}/bin/python" ]]; then
  if ! "${BOOTSTRAP_PYTHON}" -m venv --system-site-packages "${VENV_DIR}"; then
    echo "Cannot create the Connector environment at ${VENV_DIR}. Install python3-venv or select a writable WEBRTC_VENV." >&2
    exit 1
  fi
fi
install_python_deps "${VENV_DIR}/bin/python" "${PIP_ARGS[@]}"
if [[ "${routing_only}" == 1 ]]; then
  "${VENV_DIR}/bin/python" -c 'import json5'
  exit
fi

check_args=(--role "${ROLE:-machine}" --mode "${MEDIA_MODE}")
[[ "${ROLE:-machine}" == "cloud" ]] || check_args+=(--need-ros)
if [[ "${probe_camera}" == 1 ]]; then
  check_args+=(--root "${REPO_ROOT}" --profile "${REPO_ROOT}/.webrtc_profile.env")
fi
"${VENV_DIR}/bin/python" "${SCRIPT_DIR}/media_runtime.py" "${check_args[@]}"
