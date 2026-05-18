#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="${PROFILE:-${REPO_ROOT}/.webrtc_profile.env}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-30}"
DURATION="${DURATION:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --width)
      WIDTH="$2"
      shift 2
      ;;
    --height)
      HEIGHT="$2"
      shift 2
      ;;
    --fps)
      FPS="$2"
      shift 2
      ;;
    --duration)
      DURATION="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: scripts/gst_h264_smoke_test.sh [--profile .webrtc_profile.env] [--width 1920] [--height 1080] [--fps 30] [--duration 5]

Runs a short synthetic H.264 encode/decode through the selected GStreamer
elements. This validates that the selected hardware/software path is usable.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "${PROFILE}" ]]; then
  "${SCRIPT_DIR}/detect_video_stack.sh" --out "${PROFILE}"
fi

# shellcheck source=/dev/null
source "${PROFILE}"

if ! command -v gst-launch-1.0 >/dev/null 2>&1; then
  echo "gst-launch-1.0 is missing. Run ./horus bootstrap or install GStreamer packages." >&2
  exit 1
fi

if [[ -z "${GST_H264_ENCODER_NAME:-}" ]]; then
  echo "No H.264 encoder was selected in ${PROFILE}." >&2
  exit 1
fi

FRAMES=$((FPS * DURATION))
read -r -a ENCODER_PROPS <<< "${GST_H264_ENCODER_PROPS:-}"
read -r -a ENCODER_PREPROCESS <<< "${GST_H264_ENCODER_PREPROCESS:-videoconvert}"
read -r -a DECODER_PROPS <<< "${GST_H264_DECODER_PROPS:-}"
read -r -a DECODER_POSTPROCESS <<< "${GST_H264_DECODER_POSTPROCESS:-}"

cmd=(
  gst-launch-1.0 -q
  videotestsrc is-live=true num-buffers="${FRAMES}" pattern=ball
  '!'
  "video/x-raw,width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1"
  '!'
  queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream
)

if [[ -n "${GST_H264_ENCODER_PREPROCESS:-videoconvert}" ]]; then
  cmd+=('!')
  for part in "${ENCODER_PREPROCESS[@]}"; do
    [[ -n "${part}" ]] && cmd+=("${part}")
  done
fi

cmd+=('!' "${GST_H264_ENCODER_NAME}")

for prop in "${ENCODER_PROPS[@]}"; do
  [[ -n "${prop}" ]] && cmd+=("${prop}")
done

cmd+=(
  '!'
  h264parse config-interval=-1
)

if [[ -n "${GST_H264_DECODER_NAME:-}" ]]; then
  cmd+=('!' "${GST_H264_DECODER_NAME}")
  for prop in "${DECODER_PROPS[@]}"; do
    [[ -n "${prop}" ]] && cmd+=("${prop}")
  done
  if [[ -n "${GST_H264_DECODER_POSTPROCESS:-}" ]]; then
    cmd+=('!')
    for part in "${DECODER_POSTPROCESS[@]}"; do
      [[ -n "${part}" ]] && cmd+=("${part}")
    done
  fi
  cmd+=('!' videoconvert)
fi

cmd+=('!' fakesink sync=false)

echo "Encoder: ${GST_H264_ENCODER_NAME} (${VIDEO_ACCEL:-unknown})"
echo "Encoder preprocess: ${GST_H264_ENCODER_PREPROCESS:-none}"
if [[ -n "${GST_H264_DECODER_NAME:-}" ]]; then
  echo "Decoder: ${GST_H264_DECODER_NAME} (${VIDEO_DECODER_ACCEL:-unknown})"
  echo "Decoder postprocess: ${GST_H264_DECODER_POSTPROCESS:-none}"
else
  echo "Decoder: none selected; running encode-only smoke test"
fi
echo "Resolution/FPS: ${WIDTH}x${HEIGHT}@${FPS}, frames: ${FRAMES}"
start_ns="$(date +%s%N)"
"${cmd[@]}"
end_ns="$(date +%s%N)"
elapsed_ms=$(((end_ns - start_ns) / 1000000))
if [[ "${elapsed_ms}" -le 0 ]]; then
  elapsed_ms=1
fi
encoded_fps=$((FRAMES * 1000 / elapsed_ms))
echo "H.264 smoke test passed in ${elapsed_ms} ms, approx ${encoded_fps} fps."
