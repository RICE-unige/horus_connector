#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export HORUS_ENV="${HORUS_ENV:-${XDG_CONFIG_HOME:-${HOME}/.config}/horus/uav-sim-machine.env}"
export HORUS_RUN_DIR="${HORUS_RUN_DIR:-${ROOT}/.run/uav_sim}"
exec bash "${ROOT}/horus" stop zenoh
