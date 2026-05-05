#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ_FILE="${REPO_ROOT}/requirements.txt"
VENV_DIR="${WEBRTC_VENV:-${REPO_ROOT}/.venv-webrtc}"
ROLE="${1:-${HORUS_ROLE:-}}"

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "Missing ${REQ_FILE}" >&2
  exit 1
fi

PIP_ARGS=(-r "${REQ_FILE}")
if [[ "${ROLE}" == "cloud" ]]; then
  PIP_ARGS=("websockets>=12")
fi

if python3 -m venv --system-site-packages "${VENV_DIR}" >/tmp/webrtc_venv_create.log 2>&1; then
  "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  "${VENV_DIR}/bin/python" -m pip install "${PIP_ARGS[@]}"
  echo "Created WebRTC Python environment: ${VENV_DIR}"
  echo "Activate with: source ${VENV_DIR}/bin/activate"
else
  echo "python3-venv is not available; falling back to user-site install." >&2
  if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "pip is not installed for python3; bootstrapping user-local pip." >&2
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    elif command -v wget >/dev/null 2>&1; then
      wget -qO /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
    else
      echo "Need curl or wget to bootstrap pip without sudo." >&2
      exit 1
    fi
    python3 /tmp/get-pip.py --user --break-system-packages
  fi
  python3 -m pip install --user --break-system-packages "${PIP_ARGS[@]}"
  echo "Installed WebRTC dependencies into the current user's Python site-packages."
fi
