# Camera Transport Benchmark Methodology

This tooling characterizes practical camera transport paths for robot management.
The current ROS and WebRTC instruments measure different boundaries; do not
combine their latency/SLA values as equivalent publication results:

- ROS 2 DDS carrying `sensor_msgs/CompressedImage`
- Zenoh ROS 2 DDS bridge carrying `sensor_msgs/CompressedImage`
- WebRTC carrying low-latency H.264 video

The benchmark is Mode B: each transport uses the best practical path it would
normally use in the HORUS Connector system.

In the ROS 2 DDS and Zenoh arms, the benchmark publishes cached compressed-image
payloads so sender-side JPEG encoding does not contaminate the transport result.
The WebRTC arm performs live H.264 encoding, but its timing marker is generated
at RTP packetization, **after encoding**. Exact RTP-timestamp/decoded-PTS
correlation measures packetization-to-decoded-output delay. It excludes source
capture, encoding delay, Quest receipt/rendering and physical display latency.

## Decision Metric

For a fully matched capture measurement, fresh-frame SLA is:

```text
fresh-frame SLA = unique matched frames received under the latency deadline / actual source frames produced in the same measured interval
```

Late frames and dropped frames both count as failures. This avoids rewarding a
transport for delivering old visual information or silently dropping most of the
stream.

Rendered/displayed FPS requires separate instrumentation in the viewing app.
The current H.264 endpoint retains the last 10,000 matched timing samples.
Metadata is best effort and can be missing; the fresh fraction of those samples
is not a delivery SLA or fresh video FPS. `fresh_fps_estimate` is therefore null.
The renderer marks those artifacts `endpoint_timing_only` and excludes them
from capture-latency/SLA curves, as it does legacy uncorrelated-marker data.
No old research artifacts are rewritten or deleted.

Provisional decision threshold, only when that boundary is measured:

```text
150 ms source-to-receive target
```

Supporting metrics:

- P50, P95, and P99 latency
- Usable FPS
- Received FPS
- Dropped or skipped frames
- Stale frames
- Received Mbps

## Clock Correction

Each host pair should run `scripts/clock_offset_probe.py` before the benchmark.
The receiver uses the measured `offset_ms` with `--clock-offset-json`.

Do not subtract a percentile latency baseline in the renderer. Percentile
baseline subtraction can hide real queueing and makes the result hard to defend.

Negative/non-finite corrected measurements are invalid, not fresh frames.
Record offset uncertainty and path asymmetry; offset correction alone does not
prove sub-millisecond cross-host accuracy. A visual sequence/timing marker or
independent capture/display reference is required for glass-to-glass claims.

## Camera Semantics

Camera streams use freshest-frame behavior:

```text
QoS: best effort, keep last, depth 1
Receiver: keep newest complete decoded frame
```

This matches ROS 2 sensor-data guidance: camera and sensor streams usually care
more about the latest sample than reliable delivery of every old sample.

Do not use a one-buffer leaky queue for RTP fragments. The H.264 path keeps a
bounded packet-safe queue before depayloading and latest-frame behavior after
decoding. ROS input pacing occurs before pixel conversion/copy.

## Test Matrix

Recommended publication matrix:

| Variable | Values |
|---|---|
| Resolution | 1080p30, 720p30 |
| Path | LAN, VPN, cloud hub |
| Transport | ROS 2 DDS, Zenoh ROS 2 DDS bridge, WebRTC H.264 |
| Duration | 5 minutes per run |
| Repetitions | 3 to 5 runs per condition |

For each condition, plot the median across repetitions and show a light band for
run-to-run variation when space allows.

## Run Protocol

Use the existing host network configuration for baseline trials. Do not change
global network buffers or shape the active LAN/Wi-Fi/VPN interface implicitly.
Impairment trials require an explicitly configured disposable veth; the runner
rejects ordinary interfaces and cleans up only its own qdisc handle.

Run the distributed benchmark through the tracked runner:

```bash
HORUS_BENCH_DURATION=300 \
HORUS_BENCH_REPETITIONS=3 \
HORUS_BENCH_TRANSPORTS=dds,zenoh,webrtc \
HORUS_BENCH_ZENOH_ARM=quic_dgram \
python3 scripts/run_distributed_benchmark.py
```

The runner interleaves repetitions, uses camera QoS depth 1, probes clock offset
before and after each condition, rejects excessive drift, and aborts if the
requested Zenoh transport is not the transport that actually started.
The default clock-probe port is TCP `8765`, reused before WebRTC signaling starts
so cloud firewalls do not need an extra benchmark-only port.

Ubuntu outputs default to `.run/distributed_benchmark/<timestamp>-<unique-id>/`;
each remote endpoint has a matching `.run/bench/<run-id>/` directory.
`HORUS_BENCH_OUTPUT_DIR` overrides the local location. Previous runs are
preserved. Explicit resume does not erase remote evidence; missing fetches are
errors rather than empty files. Only this run's PID/start-time-identified
process groups are stopped. The runner does not kill other connector services
or reset host network policy.

Create one artifact name per condition:

```text
modeb_<resolution>_<path>_<transport>
```

Example: `modeb_1080p30_vpn_zenoh`.

Clock probe:

```bash
# sender side
python3 scripts/clock_offset_probe.py --server --host 0.0.0.0 --port 8899

# receiver side
python3 scripts/clock_offset_probe.py --host <sender-ip> --port 8899 \
  | tee .run/clock_<path>_<transport>.json
```

ROS 2 DDS or Zenoh image run:

```bash
# sender side
python3 scripts/benchmark_camera_ros.py pub \
  --profile compressed \
  --topic /benchmark/camera \
  --width 1920 --height 1080 --fps 30 --duration 300 \
  --json .run/modeb_1080p30_vpn_zenoh_pub.json

# receiver side
python3 scripts/benchmark_camera_ros.py sub \
  --profile compressed \
  --topic /benchmark/camera \
  --width 1920 --height 1080 --fps 30 --duration 300 \
  --clock-offset-json .run/clock_vpn_zenoh.json \
  --json .run/modeb_1080p30_vpn_zenoh_sub.json \
  --samples-json .run/modeb_1080p30_vpn_zenoh_sub.samples.json
```

WebRTC H.264 run:

```bash
# robot side
python3 scripts/gst_webrtc_h264_robot.py \
  --signaling-url ws://<signal-host>:8765 \
  --room benchmark \
  --video-source testsrc \
  --width 1920 --height 1080 --fps 30 --duration 300 \
  --latency-probe --latency-probe-rate 30

# machine side
python3 scripts/gst_webrtc_h264_machine.py \
  --signaling-url ws://<signal-host>:8765 \
  --room benchmark \
  --video-output ros2 \
  --duration 300 \
  --clock-offset-json .run/clock_vpn_webrtc.json \
  --latency-json .run/modeb_1080p30_vpn_webrtc_latency.json
```

The legacy renderer reads condition artifacts directly under `.run/`; it does
not aggregate the distributed runner's unique repetition folders automatically.
Retain raw repetitions and do not flatten separate trials into one condition.
Rendering is not a substitute for matched-source timing validation.

For compatible artifacts already arranged for that renderer:

```bash
python3 scripts/render_transport_benchmark.py
```

The renderer excludes legacy or endpoint-only WebRTC artifacts from capture/SLA
comparisons. Current endpoint measurements can be reported separately with
decoded FPS, sample coverage and packetization-to-decode percentiles.

## Graph Layout

For independently validated equivalent capture boundaries, use:

1. Fresh-frame SLA vs network path
2. P95 latency vs network path
3. Usable FPS vs network path

Keep the same color for each transport across all charts. Label axes with
directional guidance such as "higher is better" and "lower is better".

For the current WebRTC instrument, report decoded rate and matched endpoint
timing separately. A faster endpoint percentile does not prove lower
capture-to-Quest latency than another transport.

## References

- ROS 2 QoS sensor-data profile: https://docs.ros.org/en/rolling/Concepts/Intermediate/About-Quality-of-Service-Settings.html
- ROS 2 QoS design: https://design.ros2.org/articles/qos.html
- W3C WebRTC stats: https://www.w3.org/TR/webrtc-stats/
- QUIC datagrams for unreliable low-latency transport: https://www.rfc-editor.org/rfc/rfc9221.html
- VISTA teleoperation video benchmark: https://arxiv.org/html/2605.08886v1
