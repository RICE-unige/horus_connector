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

if python_lt_38 && [[ "${ROLE}" != "cloud" ]]; then
  echo "Python ${PY_MINOR} detected; using legacy GStreamer signaling dependencies."
  PIP_ARGS=("websocket-client<1.4")
fi

if python3 -m venv --system-site-packages "${VENV_DIR}" >/tmp/webrtc_venv_create.log 2>&1; then
  if python_lt_38; then
    "${VENV_DIR}/bin/python" -m pip install --upgrade "pip<22" "setuptools<60" wheel
  else
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  fi
  "${VENV_DIR}/bin/python" -m pip install "${PIP_ARGS[@]}"
  echo "Created WebRTC Python environment: ${VENV_DIR}"
  echo "Activate with: source ${VENV_DIR}/bin/activate"
else
  echo "python3-venv is not available; falling back to user-site install." >&2
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
    python3 /tmp/get-pip.py --user --break-system-packages
  fi
  python3 -m pip install --user --break-system-packages "${PIP_ARGS[@]}"
  echo "Installed WebRTC dependencies into the current user's Python site-packages."
fi
