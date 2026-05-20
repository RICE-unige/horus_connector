#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ_FILE="${REPO_ROOT}/requirements.txt"
VENV_DIR="${WEBRTC_VENV:-${REPO_ROOT}/.venv-webrtc}"
ROLE="${1:-${HORUS_ROLE:-}}"
MEDIA_MODE="${WEBRTC_MEDIA_MODE:-h264}"
VENV_LOG="${TMPDIR:-/tmp}/horus_connector_${UID}_webrtc_venv_create.log"

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "Missing ${REQ_FILE}" >&2
  exit 1
fi

PY_MINOR="$(python3 - <<'PY'
import sys
print(sys.version_info[:2])
PY
)"

python_lt_38() {
  python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info < (3, 8) else 1)
PY
}

pip_bootstrap_url() {
  if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info < (3, 7) else 1)
PY
  then
    echo "https://bootstrap.pypa.io/pip/3.6/get-pip.py"
  else
    echo "https://bootstrap.pypa.io/get-pip.py"
  fi
}

pip_user_install_flags() {
  PIP_USER_FLAGS=(--user)
  if python3 -m pip install --help 2>/dev/null | grep -q -- "--break-system-packages"; then
    PIP_USER_FLAGS+=(--break-system-packages)
  fi
}

select_pip_args() {
  if [[ "${ROLE}" == "cloud" ]]; then
    if python_lt_38; then
      PIP_ARGS=("websockets>=8,<10")
    else
      PIP_ARGS=("websockets>=12")
    fi
    return
  fi

  if [[ "${MEDIA_MODE}" == "jpeg" ]]; then
    PIP_ARGS=(-r "${REQ_FILE}")
    return
  fi

  if python_lt_38; then
    echo "Python ${PY_MINOR} detected; using legacy GStreamer signaling dependencies."
    PIP_ARGS=("websocket-client<1.4")
  else
    PIP_ARGS=("websockets>=12")
  fi
}

deps_available() {
  local python_cmd="$1"
  if [[ "${ROLE}" == "cloud" ]]; then
    "${python_cmd}" - <<'PY' >/dev/null 2>&1
import websockets
PY
    return
  fi

  if [[ "${MEDIA_MODE}" == "jpeg" ]]; then
    "${python_cmd}" - <<'PY' >/dev/null 2>&1
import aiortc
import numpy
import PIL
import websockets
PY
    return
  fi

  "${python_cmd}" - <<'PY' >/dev/null 2>&1
try:
    from websockets.sync.client import connect  # noqa: F401
except Exception:
    import websocket  # noqa: F401
PY
}

install_python_deps() {
  local python_cmd="$1"
  shift
  if deps_available "${python_cmd}"; then
    echo "WebRTC Python dependencies already available for ${MEDIA_MODE} mode."
    return
  fi

  "${python_cmd}" -m pip install "$@"
}

select_pip_args

echo "WebRTC dependency mode: role=${ROLE:-auto}, media=${MEDIA_MODE}"
if [[ "${MEDIA_MODE}" != "jpeg" && "${ROLE}" != "cloud" ]]; then
  echo "Using native GStreamer WebRTC dependencies; aiortc is only installed for WEBRTC_MEDIA_MODE=jpeg."
fi

if python3 -m venv --system-site-packages "${VENV_DIR}" >"${VENV_LOG}" 2>&1; then
  if deps_available "${VENV_DIR}/bin/python"; then
    echo "WebRTC Python dependencies already available from the virtualenv/system site-packages."
  else
    if python_lt_38; then
      "${VENV_DIR}/bin/python" -m pip install --upgrade "pip<22" "setuptools<60" wheel
    else
      "${VENV_DIR}/bin/python" -m pip install --upgrade pip
    fi
    install_python_deps "${VENV_DIR}/bin/python" "${PIP_ARGS[@]}"
  fi
  echo "Created WebRTC Python environment: ${VENV_DIR}"
  echo "Activate with: source ${VENV_DIR}/bin/activate"
else
  echo "python3-venv is not available; falling back to user-site install." >&2
  if deps_available python3; then
    echo "WebRTC Python dependencies already available from system Python."
  else
    if ! python3 -m pip --version >/dev/null 2>&1; then
      echo "pip is not installed for python3; bootstrapping user-local pip." >&2
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$(pip_bootstrap_url)" -o /tmp/get-pip.py
      elif command -v wget >/dev/null 2>&1; then
        wget -qO /tmp/get-pip.py "$(pip_bootstrap_url)"
      else
        echo "Need curl or wget to bootstrap pip without sudo." >&2
        exit 1
      fi
      python3 /tmp/get-pip.py --user
    fi
    pip_user_install_flags
    install_python_deps python3 "${PIP_USER_FLAGS[@]}" "${PIP_ARGS[@]}"
  fi
  echo "Installed WebRTC dependencies into the current user's Python site-packages."
fi
