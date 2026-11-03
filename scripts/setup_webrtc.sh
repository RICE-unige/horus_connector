#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ_FILE="${REPO_ROOT}/requirements-media.txt"
VENV_DIR="${WEBRTC_VENV:-${REPO_ROOT}/.venv-webrtc}"
ROLE="${1:-${HORUS_ROLE:-}}"
[[ -n "${ROLE}" && "${ROLE}" != auto ]] || ROLE="${HORUS_ROLE:-machine}"
MEDIA_MODE="${WEBRTC_MEDIA_MODE:-h264}"
BOOTSTRAP_PYTHON="${HORUS_BOOTSTRAP_PYTHON:-/usr/bin/python3}"
command -v "${BOOTSTRAP_PYTHON}" >/dev/null || BOOTSTRAP_PYTHON=python3
if ! "${BOOTSTRAP_PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "HORUS connector requires Python 3.10 or newer. Select a compatible interpreter before bootstrap." >&2
  exit 1
fi
if [[ "${ROLE}" != cloud ]]; then
  ros_setup="${ROS_SETUP_PATH:-/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash}"
  ros_setup="${ros_setup/#\~/${HOME}}"
  if [[ -f "${ros_setup}" ]]; then
    set +u
    source "${ros_setup}"
    set -u
  fi
fi
VENV_LOG="${TMPDIR:-/tmp}/horus_connector_${UID}_webrtc_venv_create.log"
PIP_INDEX_ARGS=()

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "Missing ${REQ_FILE}" >&2
  exit 1
fi

select_pip_args() {
  if [[ "${MEDIA_MODE}" == "jpeg" && "${HORUS_ALLOW_LEGACY_JPEG:-0}" != "1" ]]; then
    echo "WEBRTC_MEDIA_MODE=jpeg is legacy-only and disabled by default during bootstrap."
    echo "Select WEBRTC_MEDIA_MODE=h264, or explicitly set HORUS_ALLOW_LEGACY_JPEG=1 for the compatibility runtime." >&2
    return 1
  fi

  if [[ "${ROLE}" == "cloud" ]]; then
    PIP_ARGS=(-r "${REQ_FILE}")
    return
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

deps_available() {
  local python_cmd="$1"
  if [[ "${ROLE}" == "cloud" ]]; then
    "${python_cmd}" - <<'PY' >/dev/null 2>&1
from websockets.sync.client import connect
import json5
PY
    return
  fi

  if [[ "${MEDIA_MODE}" == "jpeg" ]]; then
    "${python_cmd}" - <<'PY' >/dev/null 2>&1
import aiortc
import numpy
import PIL
import websockets
import json5
PY
    return
  fi

  "${python_cmd}" - <<'PY' >/dev/null 2>&1
from websockets.sync.client import connect
import json5
PY
}

install_python_deps() {
  local python_cmd="$1"
  shift
  "${python_cmd}" -m pip install --timeout 30 --retries 2 "${PIP_INDEX_ARGS[@]}" "$@"
}

select_pip_args
select_pip_index

echo "WebRTC dependency mode: role=${ROLE:-auto}, media=${MEDIA_MODE}"
if [[ "${MEDIA_MODE}" != "jpeg" && "${ROLE}" != "cloud" ]]; then
  echo "Using native GStreamer WebRTC dependencies; aiortc is only installed for WEBRTC_MEDIA_MODE=jpeg."
fi

if "${BOOTSTRAP_PYTHON}" -m venv --system-site-packages "${VENV_DIR}" >"${VENV_LOG}" 2>&1; then
  install_python_deps "${VENV_DIR}/bin/python" "${PIP_ARGS[@]}"
  echo "Created WebRTC Python environment: ${VENV_DIR}"
  echo "Activate with: source ${VENV_DIR}/bin/activate"
else
  echo "Cannot create isolated media environment at ${VENV_DIR}. Install python3-venv or select a writable WEBRTC_VENV; no host Python packages were changed." >&2
  exit 1
fi
check_args=(--role "${ROLE:-machine}" --mode "${MEDIA_MODE}")
[[ "${ROLE:-machine}" == "cloud" ]] || check_args+=(--need-ros)
"${VENV_DIR}/bin/python" "${SCRIPT_DIR}/media_runtime.py" "${check_args[@]}"
