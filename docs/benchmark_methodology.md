# Camera Transport Benchmark Methodology

This benchmark is designed for a publication-quality comparison of practical
camera transport paths for robot management:

- ROS 2 DDS carrying `sensor_msgs/CompressedImage`
- Zenoh ROS 2 DDS bridge carrying `sensor_msgs/CompressedImage`
- WebRTC carrying low-latency H.264 video

The benchmark is Mode B: each transport uses the best practical path it would
normally use in the HORUS Connector system.

In the ROS 2 DDS and Zenoh arms, the benchmark publishes cached compressed-image
payloads so sender-side JPEG encoding does not contaminate the transport result.
In the WebRTC arm, live H.264 encoding is part of the practical media path and
is therefore included in the measured endpoint pipeline.

## Decision Metric

The headline metric is fresh-frame SLA:

```text
fresh-frame SLA = frames rendered/received under the latency deadline / frames captured
```

Late frames and dropped frames both count as failures. This avoids rewarding a
transport for delivering old visual information or silently dropping most of the
stream.

Default decision threshold:

```text
150 ms glass-to-glass / capture-to-receive deadline
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

## Camera Semantics

Camera streams use freshest-frame behavior:

```text
QoS: best effort, keep last, depth 1
Receiver: keep newest frame and drop stale buffered frames
```

This matches ROS 2 sensor-data guidance: camera and sensor streams usually care
more about the latest sample than reliable delivery of every old sample.

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

Before running a publication benchmark:

```bash
sudo sysctl -w net.core.rmem_max=8388608
sudo sysctl -w net.core.wmem_max=8388608
sudo sysctl -w net.core.rmem_default=1048576
sudo sysctl -w net.core.wmem_default=1048576
```

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

Render after all conditions are complete:

```bash
python3 scripts/render_transport_benchmark.py
```

The renderer refuses to plot legacy artifacts that do not contain the current
fresh-frame SLA and clock-correction metadata.

## Graph Layout

Use three line charts per resolution:

1. Fresh-frame SLA vs network path
2. P95 latency vs network path
3. Usable FPS vs network path

Keep the same color for each transport across all charts. Label axes with
directional guidance such as "higher is better" and "lower is better".

## References

- ROS 2 QoS sensor-data profile: https://docs.ros.org/en/rolling/Concepts/Intermediate/About-Quality-of-Service-Settings.html
- ROS 2 QoS design: https://design.ros2.org/articles/qos.html
- W3C WebRTC stats: https://www.w3.org/TR/webrtc-stats/
- QUIC datagrams for unreliable low-latency transport: https://www.rfc-editor.org/rfc/rfc9221.html
- VISTA teleoperation video benchmark: https://arxiv.org/html/2605.08886v1
