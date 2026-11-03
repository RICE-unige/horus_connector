# HORUS Connector Configuration

Use `./horus setup` for normal configuration. Edit `.env` directly only when you need advanced control.

Launch, bootstrap, monitoring and fleet use the same literal `KEY=VALUE`
grammar, with optional `export` and shell-style quoting. Put comments on their
own lines; inline `#` remains part of the value. Dollar signs and
`${VARIABLE}` remain literal; command substitutions and shell statements are
not executed. Quote values containing spaces, quotes or JSON. Generated fleet
files use this same grammar. See [reliability and performance](performance.md)
for routing, camera delivery, recovery budgets and qualification instructions.

## Core Environment

| Variable | Used by | Purpose |
|---|---|---|
| `HORUS_ROLE` | all | `robot`, `machine`, `teammate`, or `cloud`. |
| `HORUS_ROOM` | robot, machine, teammate | Pairing room. Use one stable room per robot or teammate endpoint, for example `robot-a`. |
| `HORUS_TOPOLOGY` | all | `hub` through a cloud server, or `direct` over VPN/Tailscale/LAN. |
| `HORUS_CLOUD_IP` | hub robot, hub machine | Cloud public IP or DNS name. |
| `HORUS_MACHINE_IP` | direct robot, direct machine | Operator machine VPN/LAN address. |
| `HORUS_WEBRTC_SIGNAL_IP` | optional | Override WebRTC signaling address. Usually empty. |
| `HORUS_SSH_ALIAS` | helper scripts | SSH alias for a cloud/server host. |
| `REMOTE_DIR` | helper scripts | Remote install path. |

## ROS 2

| Variable | Purpose |
|---|---|
| `ROS_DISTRO` | ROS 2 distro to source, for example `humble` or `jazzy`. |
| `ROS_DOMAIN_ID` | DDS domain used by local ROS 2 nodes and the bridge. |
| `ROS_SETUP_PATH` | Optional custom `setup.bash` or `local_setup.bash` path for source-built ROS installs. |
| `ROS_LOCALHOST_ONLY` | Keeps DDS traffic local to the host. Default: `1`. |
| `ROS_AUTOMATIC_DISCOVERY_RANGE` | ROS discovery range. Default: `LOCALHOST`. |
| `ROS_CMD_TOPIC` | Robot velocity command topic. Default: `/cmd_vel`. |
| `HORUS_SKIP_DDS_PREFLIGHT` | Set `1` to skip the launch-time DDS preflight gate. |
| `CYCLONEDDS_URI` | Optional existing CycloneDDS XML profile. Use this when a partner robot already has a working DDS interface profile. |
| `HORUS_DDS_INTERFACE` | Optional CycloneDDS interface name for the connector bridge participant. |
| `HORUS_DDS_PREFLIGHT_TOPIC_TIMEOUT` | Timeout in seconds for the preflight `ros2 topic list` probe. Default: `5.0`. |
| `HORUS_DDS_PROBE_TOPIC` | Optional absolute topic for a bounded real-sample receive probe. `doctor` also selects visible `/tf` or odometry automatically. This proves local DDS delivery, not end-to-end delivery. |

## DDS Preflight

`./horus launch <role>` runs a DDS preflight before starting Zenoh unless `HORUS_SKIP_DDS_PREFLIGHT=1` or `--skip-dds-preflight` is used. It snapshots the launch environment and classifies issues before the bridge starts. If `CYCLONEDDS_URI` is set, HORUS passes that existing profile to the bridge. If it is empty, HORUS renders a connector-owned CycloneDDS profile for the bridge participant.

Artifacts:

```text
.run/dds_env.json
.run/dds_preflight.json
.run/cyclonedds_connector.xml
.run/zenoh.log
```

The launch terminal DDS values are captured before `.env` is loaded, so stale `.env` settings can be detected. The snapshot includes `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `ROS_LOCALHOST_ONLY`, `ROS_AUTOMATIC_DISCOVERY_RANGE`, `ROS_STATIC_PEERS`, `ROS_DISCOVERY_SERVER`, `CYCLONEDDS_URI`, `FASTRTPS_DEFAULT_PROFILES_FILE`, network interfaces, and the selected route interface.

Use:

```bash
./horus doctor robot
./horus doctor machine
./horus doctor teammate
```

Typical diagnoses:

| Code | Meaning |
|---|---|
| `DDS_DOMAIN_MISMATCH` | The launch terminal and connector config use different `ROS_DOMAIN_ID` values. |
| `DISCOVERY_SERVER_UNSUPPORTED` | `ROS_DISCOVERY_SERVER` is set; the bridge cannot join a FastDDS discovery-server graph. |
| `ONLY_INFRASTRUCTURE_TOPICS` | Only `/rosout` and `/parameter_events` are visible, usually pointing to domain, discovery, or interface mismatch. |
| `HORUS_FILTERED_TOPIC` | A visible topic is blocked by the role's Zenoh allow-list. |
| `BRIDGE_ROUTE_MISSING` | A visible topic does not appear in the current bridge route log. |
| `MACHINE_IMPORT_ROUTES_NOT_SEEN` | The machine bridge log does not show remote import routes yet. |
| `DOCKER_NETWORK_NOT_HOST` | Docker bridge runtime must use host networking for DDS discovery. |
| `FASTDDS_SHM_CONTAINER_WARNING` | FastDDS shared-memory transport may break data flow across container IPC boundaries. |
| `DDS_SAMPLE_NOT_RECEIVED` | Discovery found an interface but the read-only local subscriber received no sample within its deadline. Check source activity, QoS and DDS selection. |

For Docker bridge runtime, HORUS mounts either the existing `CYCLONEDDS_URI` XML or the rendered CycloneDDS XML into the bridge container, passes the DDS environment explicitly, and runs the bridge with host networking.

For partner robots that discover ROS 2 nodes through a real LAN interface, do not use localhost-only discovery. Set:

```bash
ROS_LOCALHOST_ONLY=0
ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
CYCLONEDDS_URI=file:///absolute/path/to/their/cyclonedds.xml
```

Use a robot Zenoh config where `ros_localhost_only` is `false`; the default robot profile is localhost-only to avoid accidentally mixing ROS graphs on shared lab networks.

## Zenoh

| Variable | Purpose |
|---|---|
| `ZENOH_PORT` | Zenoh TCP port. Default: `7447`. |
| `ZENOH_VERSION` | Exact bridge release installed by full bootstrap; template default is `1.10.2`. `./horus update-zenoh [version]` updates only the project-local bridge/plugin and does not rewrite this setting. |
| `ZENOH_CONFIG` | `auto` selects the role config, or set a custom JSON5 path. |
| `ZENOH_TRANSPORT` | `auto`, `tcp`, or `quic`. Default: `auto`. |
| `ZENOH_AUTO_ENABLE_QUIC` | Set `1` to let `auto` try QUIC first when TLS is configured, with TCP fallback. |
| `ZENOH_QUIC_PARAMS` | QUIC endpoint options. Default: `multistream=1;mixed_rel=auto`. |
| `ZENOH_TLS_ROOT_CA` | Hub/listener public cert trusted by robot and machine clients. Usually written by `./horus quic install-cert`. |
| `ZENOH_TLS_LISTEN_KEY` | Listener private key for cloud or direct machine. Usually written by `./horus quic setup-server`. |
| `ZENOH_TLS_LISTEN_CERT` | Listener certificate for cloud or direct machine. Usually written by `./horus quic setup-server`. |
| `ZENOH_TLS_VERIFY_NAME` | `0` by default to avoid IP/DNS certificate mismatch during lab testing. |
| `ZENOH_NAMESPACE` | Optional namespace for robot topics, for example `/robot_a`. |
| `HORUS_ZENOH_ENABLED` | Set `0` to disable Zenoh for a run. |

Role configs:

| Role | Config |
|---|---|
| `robot` | `config/zenoh_robot.json5` |
| `machine` | `config/zenoh_machine.json5` |
| `teammate` | `config/zenoh_teammate.json5` |
| `cloud` | `config/zenoh_cloud.json5` |

The teammate profile exports only the field-teammate observability topics and
imports only guidance topics. It deliberately excludes velocity, service, and
action interfaces so the connector mirrors the SDK's default-deny human entity
contract.

The default robot profile exports robot ROS topics through Zenoh and imports command and goal topics from the operator machine. The machine profile imports robot topics and exports command and goal topics, plus service and action clients:

```text
/cmd_vel
/<robot>/cmd_vel
/goal
/<robot>/goal
/<robot>/command
```

The default topic routes also cover `/cmd/velocity`, `/goal_pose`, and `/move_base_simple/goal`. Check custom `ZENOH_CONFIG` profiles separately; they can override these defaults.

Both endpoint profiles also route `/horus/**` and `/<namespace>/horus/**` in the
return direction, including registration acknowledgements, heartbeat, replay,
model requests and teleop runtime state. Static TF is not rate-limited; retained
history uses a cache multiplier of 10 and a five-second late-join query budget.
If more than ten independent retained writers share a topic, size that cache
for the actual writer count and history depth.

Control/HORUS events have higher priority than maps and model chunks. The queue
allows a bounded 1 ms enqueue wait instead of dropping immediately on a burst.
The cloud router uses the same bounded enqueue wait. Initial DDS route
discovery can retain startup samples; report startup and steady-state latency
separately rather than treating a newly opened connection as a ready graph.
Reliable routes now preserve the publisher's requested reliability through
blocking enqueue; best-effort routes may still drop. Backpressure can reach
DDS, and priorities do not guarantee lossless end-to-end delivery or remove
TCP head-of-line blocking. Nonreplaceable transfers still require application
revision/acknowledgement/retry protocols and bounded source histories. Keep
camera/map rates and bitrates within measured link capacity. Default endpoint
profiles no longer impose whole-message TF or hidden sensor-frequency caps.

The operator camera path uses WebRTC media. ROS image topics can still cross
Zenoh when a remote subscriber matches them. Use per-stream `webrtc_only` with
`HORUS_ZENOH_CAMERA_POLICY=configured` or an explicit custom profile to avoid
duplicate camera traffic. Capability-derived custom command routes are
configured with `HORUS_ROUTES_CONFIG`; see [routing details](performance.md#routing-and-backpressure).

`ZENOH_TRANSPORT=auto` uses QUIC first only when the local TLS profile is ready; TCP remains available as fallback. Use `ZENOH_TRANSPORT=tcp` to force the conservative path. Use `ZENOH_TRANSPORT=quic` only when you want the launch to fail if QUIC TLS is missing.

QUIC setup:

```bash
# Cloud hub or direct-mode machine listener
./horus quic setup-server
./horus quic export-cert

# Robot and hub-mode machine clients
./horus quic install-cert <hub-cert.pem>
```

The generated profile is local and ignored by git.

To add a state topic, update both the robot `allow.publishers` list and the machine `allow.subscribers` list. Add throttling and priority on the robot side when needed:

```json5
pub_max_frequencies: [
  ".*/imu$=50",
],

pub_priorities: [
  ".*/imu$=3:express",
],
```

Zenoh priority `1` is highest, `7` is lowest. `:express` reduces latency for small high-value messages.

## Field Teammate

The `teammate` role runs the HoloLens relay and a restricted Zenoh bridge. The
relay converts HoloLens pose, hand, PV, status, and guidance data to ordinary
ROS 2 topics. Zenoh carries only the allow-listed teammate topics to the
operator network; live voice uses its separate WebRTC signaling path.

Normal setup:

```bash
./horus setup teammate
./horus bootstrap teammate
./horus launch teammate
```

The teammate setup wizard configures the endpoint identity, HoloLens address,
ROS 2 environment, hub or direct topology, and Zenoh transport. It leaves the
robot camera WebRTC settings independent from the HoloLens PV relay.

| Variable | Purpose |
|---|---|
| `FIELD_TEAMMATE_NAME` | Stable teammate endpoint name used in ROS topic names. |
| `FIELD_TEAMMATE_HOLOLENS_HOST` | HoloLens IP or DNS name. Leave empty to run without a headset connection. |
| `FIELD_TEAMMATE_PV_PORT` | HoloLens photo/video stream port. |
| `FIELD_TEAMMATE_SPATIAL_INPUT_PORT` | HoloLens spatial input port. |
| `FIELD_TEAMMATE_UMQ_PORT` | HoloLens message queue port. |
| `FIELD_TEAMMATE_MAP_FRAME` | ROS frame used for teammate poses. |
| `FIELD_TEAMMATE_PROFILE_HEIGHT` | Teammate profile height in meters. |
| `FIELD_TEAMMATE_CAMERA_HEIGHT` | Camera height offset in meters. |
| `FIELD_TEAMMATE_FLOOR_HEIGHT` | Floor height offset in meters. |
| `FIELD_TEAMMATE_POSE_ORIGIN` | Pose origin mode. Default: `camera`. |
| `FIELD_TEAMMATE_CONNECT_TIMEOUT` | Connection timeout in seconds. |
| `FIELD_TEAMMATE_RECONNECT_DELAY` | Reconnect delay in seconds. |
| `FIELD_TEAMMATE_VIDEO_PROFILE` | Preset video profile, for example `fast60`. |
| `FIELD_TEAMMATE_RAW_IMAGE` | Set `1` to publish raw image data when supported. |
| `FIELD_TEAMMATE_VIDEO_MODE` | Optional video mode override. |
| `FIELD_TEAMMATE_VIDEO_WIDTH` | Optional video width override. |
| `FIELD_TEAMMATE_VIDEO_HEIGHT` | Optional video height override. |
| `FIELD_TEAMMATE_VIDEO_FPS` | Optional video FPS override. |
| `FIELD_TEAMMATE_VIDEO_QUALITY` | Optional video quality override. |

## WebRTC Camera And Control

| Variable | Purpose |
|---|---|
| `WEBRTC_MEDIA_MODE` | `h264` is the supported path. `jpeg` is legacy direct-mode fallback only. |
| `WEBRTC_SIGNAL_PORT` | WebRTC signaling port. Default: `8765`. |
| `HORUS_WEBRTC_SIGNAL_TLS` | Set `1` to use WSS signaling. |
| `HORUS_WEBRTC_SIGNAL_TLS_CERT` | Relay TLS certificate for WSS. |
| `HORUS_WEBRTC_SIGNAL_TLS_KEY` | Relay TLS private key for WSS. |
| `WEBRTC_DURATION` | `0` (default) runs until stopped; positive seconds are for finite tests. |
| `WEBRTC_JITTER_BUFFER_MS` | Explicit receive jitter budget. Defaults to 50 ms for direct/VPN profiles and 100 ms otherwise; tune against measured loss/jitter, not to zero indiscriminately. |
| `WEBRTC_ICE_SERVERS` | Comma-separated STUN/TURN servers. |
| `WEBRTC_ICE_TRANSPORT_POLICY` | `all` for normal direct-first behavior, or `relay` to force TURN validation. |
| `HORUS_WEBRTC_DEBUG_ICE` | Set `1` to log sanitized ICE candidate exchange while debugging connectivity. |
| `WEBRTC_VIDEO_WIDTH` | Camera output width. |
| `WEBRTC_VIDEO_HEIGHT` | Camera output height. |
| `WEBRTC_VIDEO_FPS` | Camera FPS. |
| `WEBRTC_VIDEO_SOURCE` | Robot source. Use `ros2` for ROS image input. |
| `WEBRTC_VIDEO_OUTPUT` | Machine output. Use `ros2`, `display`, or `both`. |
| `WEBRTC_VIDEO_SINK` | GStreamer display sink when output includes display. |
| `WEBRTC_ROS_IMAGE_INPUT_TOPIC` | Robot raw image topic. |
| `WEBRTC_ROS_IMAGE_OUTPUT_TOPIC` | Machine decoded image topic. |
| `WEBRTC_ROS_IMAGE_OUTPUT_ENCODING` | ROS encoding for decoded frames. |
| `WEBRTC_ROS_IMAGE_FRAME_ID` | Frame ID for decoded ROS images. |
| `WEBRTC_ROS_IMAGE_QOS` | `auto` is recommended. |
| `HORUS_WEBRTC_PYTHON` | Exact media interpreter; launch checks synchronous websockets, GI, NICE and ROS imports before starting services. |
| `WEBRTC_VENV` | Optional media virtualenv path used by setup and interpreter selection. |
| `HORUS_STREAMS_CONFIG` | Optional JSON config for multiple WebRTC camera streams. Usually written by `./horus setup`. |
| `HORUS_WEBRTC_SESSION_MODE` | `auto`, `bundled`, or `isolated`. Version-2 configs default to `bundled`. |
| `HORUS_WEBRTC_SESSION_ROOM` | Optional override for the bundled robot session room. |
| `HORUS_WEBRTC_ADDITIONAL_ROOMS` | Comma-separated extra bundled-session rooms on the robot for additional operators in hub topology. Each extra room launches a separate encoder; not available in direct or isolated mode. |
| `VIDEO_BITRATE_KBIT` | Starting H.264 bitrate. |
| `WEBRTC_ADAPTIVE_BITRATE` | Enable adaptive bitrate control. Default: `1`. |
| `WEBRTC_CONTROL_ENABLED` | Legacy/debug option for WebRTC DataChannel publishing to `ROS_CMD_TOPIC`. Keep `0` for normal runs. Default: `0`. |
| `WEBRTC_ENABLE_CONTROL` | Legacy alias for `WEBRTC_CONTROL_ENABLED`. |
| `HORUS_WEBRTC_CONTROL_TOKEN` | Shared token required on robot and authorized operator paths when WebRTC control is enabled. |
| `WEBRTC_ALLOW_UNAUTHENTICATED_CONTROL` | Allow WebRTC control commands without a token for trusted lab tests only. Default: `0`. |
| `WEBRTC_CMD_MAX_LINEAR_MPS` | Clamp each linear Twist component. Default: `0.5`. |
| `WEBRTC_CMD_MAX_ANGULAR_RPS` | Clamp each angular Twist component. Default: `1.0`. |
| `HORUS_WEBRTC_RELAY_TOKEN` | Shared token required by public hub signaling relays. |
| `HORUS_WEBRTC_CREDENTIALS_FILE` | Relay-side JSON file containing per-client role and room credentials. Preferred over the shared token. |
| `HORUS_WEBRTC_CLIENT_ID` | Stable endpoint identity, for example `robot:robot-a`. |
| `HORUS_WEBRTC_CLIENT_TOKEN` | Endpoint-specific signaling token. |
| `HORUS_WEBRTC_RELAY_ALLOW_UNAUTHENTICATED` | Allow a public signaling relay without a token for trusted test networks. Default: `0`. |
| `HORUS_VOICE_TRUSTED_DEVELOPMENT` | Allow tokenless `operator` and `field_teammate` voice registration only. Default: `1`; credential files still take precedence. |
| `HORUS_VOICE_ENABLED` | Start and configure field-teammate live voice. Default: `1`. |
| `FIELD_TEAMMATE_VOICE_SIGNALING_HOST` | Optional remote signaling override. Empty uses the bridge-side/local relay route automatically. |
| `FIELD_TEAMMATE_VOICE_TOKEN` | Optional production field-teammate credential. Empty selects trusted-development mode. |
| `WEBRTC_MIN_BITRATE_KBIT` | Minimum adaptive bitrate. |
| `WEBRTC_MAX_BITRATE_KBIT` | Maximum adaptive bitrate. |
| `WEBRTC_H264_KEY_INT_MAX` | H.264 keyframe interval. |
| `WEBRTC_CMD_WATCHDOG_TIMEOUT` | Robot command timeout before publishing zero velocity. |
| `WEBRTC_CMD_RATE_LIMIT_HZ` | Maximum accepted command rate. |
| `WEBRTC_CMD_MAX_AGE_MS` | Reject commands older than this deadline. Set `0` only when clocks cannot be synchronized. |
| `WEBRTC_CMD_MAX_FUTURE_MS` | Reject commands whose timestamp is too far in the future. |
| `WEBRTC_BITRATE_BAD_LOSS` | Packet-loss fraction that immediately reduces bitrate. |
| `WEBRTC_BITRATE_GOOD_LOSS` | Packet-loss ceiling used for a healthy sample. |
| `WEBRTC_BITRATE_BAD_RTT_MS` | RTT that immediately reduces bitrate. |
| `WEBRTC_BITRATE_GOOD_RTT_MS` | RTT ceiling used for a healthy sample. |

### Multiple Camera Streams

Use `./horus setup` and choose the number of WebRTC camera streams. The setup writes `config/webrtc_streams.json`, which is ignored by git because each deployment usually has different cameras and room names.

New version-2 configurations bundle all camera tracks for one robot into one `webrtcbin` peer connection and one ICE/DTLS session. Each track keeps its own input topic, output topic, resolution, frame rate, and bitrate. The machine maps the SDP track ID back to the configured ROS 2 output topic.

For two operators in hub mode, set `HORUS_WEBRTC_ADDITIONAL_ROOMS=operator_b` on the robot. Keep the original room on operator A; set `HORUS_WEBRTC_SESSION_ROOM=operator_b` on operator B. Each operator machine runs its own connector instance and receives the same configured tracks. The robot runs one sender/encoder per room, so this is a static fan-out mechanism, not a shared-encode broadcast or automatic join system. Ensure the relay credentials authorize both rooms. With zero enabled streams in an explicit config, no WebRTC media process is started.

Version-1 configurations remain supported as `isolated` mode and continue to launch one sender, receiver, and signaling room per stream. Use that mode only for compatibility or fault-isolation testing.

IDs are matched to exact negotiated MSID tokens, not substrings or pad order.
Use the same camera IDs on both endpoints. Unknown tracks are ignored rather
than silently published as another camera. Fleet launch rejects duplicate
instance names, rooms and image/CameraInfo output topics before starting any
services. Direct fleets share one room-aware local signaling relay.

### ROS Image Metadata

Bundled cameras require GStreamer 1.22+ so each incoming track has its negotiated
MSID, not a guessed pad order. Single-camera H.264 recovery requires 1.20+.
The MSID property is documented in [GStreamer webrtcbin](https://gstreamer.freedesktop.org/documentation/webrtc/index.html).

Each input has one depth-one subscription. `auto`/`sensor_data` use best effort
and accept reliable publishers too; `default` explicitly requests reliable QoS.
Input pacing applies the configured FPS before copying/converting pixels. Raw
ingress and decoded output keep the newest frame; the bounded H.264 RTP queue
does not discard arbitrary fragments as if they were complete images.

Optional fields on each camera in the JSON configuration:

```json
{
  "id": "front",
  "input_topic": "/robot/camera/image_raw",
  "output_topic": "/remote/camera/image_raw",
  "input_camera_info_topic": "/robot/camera/camera_info",
  "output_camera_info_topic": "/remote/camera/camera_info",
  "preserve_source_header": true,
  "width": 640,
  "height": 480,
  "fps": 30
}
```

Default image headers retain the receiver publication clock and configured
frame ID for compatibility. Opting into `preserve_source_header` uses only an
exact RTP-timestamp/decoded-PTS metadata match. A frame without matching
metadata is dropped rather than given a timestamp in another clock domain.
Metadata uses an unordered best-effort DataChannel, so coverage is not
guaranteed for every decoded frame. Monitor `metadata_missing_frames` in the
per-track media health file. Preserved source frames must also exist in the receiving
ROS/TF graph, including any namespace translation.

CameraInfo forwarding supports full-frame scaling: K and P are scaled to the
decoded width/height, while D and R are preserved. Cropped ROI or binned
calibration is not guessed. Invalid or unavailable calibration is omitted.
These options work in bundled and isolated sessions. They do not instrument
Quest display latency.

Example template:

```bash
cp config/webrtc_streams.example.json config/webrtc_streams.json
./horus setup
```

Bundled sessions use one media log:

```bash
./horus logs webrtc
```

Version-1 isolated streams use the stream service name:

```bash
./horus logs webrtc-primary
./horus logs webrtc-camera-2
```

## Hardware Codecs

| Variable | Purpose |
|---|---|
| `WEBRTC_ENCODER_PREFERENCE` | `stable`, `hardware`, or `software`. |
| `WEBRTC_DECODER_PREFERENCE` | `stable`, `hardware`, or `software`. |
| `HORUS_INTEL_MEDIA_DRIVER` | `free` by default. Use `non-free` only when required. |

Bootstrap probes the local machine and writes the selected video path to `.webrtc_profile.env`.

Launch uses a cached, atomic per-instance `.run/webrtc_profile.env`. Interpreter,
detector, GStreamer version, plugin search paths, devices and video settings
form its cache fingerprint. A codec probe proves availability, not sustained
multi-camera capacity. `WEBRTC_H264_KEY_INT_MAX` drives supported hardware and
software GOP properties. Launch does not change headset render quality.

Expected paths:

| Platform | Typical path |
|---|---|
| Generic Linux / WSL / NUC | x264 encode, libav decode. |
| Intel with working VAAPI | VAAPI encode/decode when selected and probe passes. |
| NVIDIA Jetson | NVIDIA V4L2 H.264 when JetPack/L4T packages are available. |
| Older JetPack 4 | Zenoh Docker fallback may be used because of glibc compatibility. |

## TURN

TURN is optional. Use it when WebRTC cannot establish media through NAT/firewalls.

| Variable | Purpose |
|---|---|
| `HORUS_CLOUD_RUN_TURN` | Set `1` on cloud to install/configure TURN. |
| `TURN_PORT` | TURN listening port. Default: `3478`. |
| `TURN_MIN_PORT` | Minimum relay UDP port. Default: `49152`. |
| `TURN_MAX_PORT` | Maximum relay UDP port. Default: `65535`. |
| `TURN_REALM` | TURN realm. |
| `TURN_USER` | TURN username. |
| `TURN_PASSWORD` | TURN password. |
| `TURN_PRIVATE_IP` | Optional VM/private IP for cloud NAT mapping. Auto-detected during bootstrap when empty. |
| `HORUS_WEBRTC_TURN_PUBLIC_HOST` | Public TURN DNS name or IP advertised by signaling. |
| `HORUS_WEBRTC_TURN_SECRET` | Coturn REST API shared secret used to mint expiring credentials. |
| `HORUS_WEBRTC_TURN_TTL_SEC` | Lifetime of issued TURN credentials. Default: `3600`. |

Example:

```bash
HORUS_CLOUD_RUN_TURN=1
TURN_USER=horus
TURN_PASSWORD=<password>
WEBRTC_ICE_SERVERS=stun:stun.l.google.com:19302,turn://horus:<password>@cloud.example.com:3478
./horus bootstrap cloud
```

Cloud firewall requirements:

```text
tcp/3478
udp/3478
udp/49152-65535
```

Robot and machine roles do not run TURN. With static credentials they use the same `WEBRTC_ICE_SERVERS`; with REST credentials the authenticated signaling relay supplies the expiring TURN URLs before media negotiation.

For production, configure `HORUS_WEBRTC_TURN_SECRET` on the hub instead of a static `TURN_USER`/`TURN_PASSWORD`. `setup_turn.sh` then enables coturn's REST credential mode, and the signaling relay issues a fresh credential to each authenticated session. The static credential path remains available for existing deployments.

## Signaling Identity

The shared relay token remains compatible, but per-client credentials prevent one robot or machine from registering under another room or role.

Generate a room credential file on the hub:

```bash
python3 scripts/generate_webrtc_credentials.py --room robot-a
```

Set `HORUS_WEBRTC_CREDENTIALS_FILE` on the hub. Put only the printed robot identity/token on the robot and only the machine identity/token on the operator machine. Do not copy the complete credential file to endpoints.

## Synthetic Data

| Variable | Purpose |
|---|---|
| `FAKE_DATA_ROBOT_ID` | Robot ID used by `./horus fake-data`. |
| `FAKE_ROS_IMAGE_TOPIC` | Fake camera topic. |
| `FAKE_ROS_ODOM_TOPIC` | Fake odometry topic. |
| `FAKE_ROS_SCAN_TOPIC` | Fake LaserScan topic. |
| `FAKE_ROS_JOINT_TOPIC` | Fake joint state topic. |
| `FAKE_ROS_POINTS_TOPIC` | Fake point cloud topic. |
| `FAKE_ROS_IMAGE_QOS` | Fake image QoS. |
| `FAKE_CAMERA_WIDTH` | Fake camera width. |
| `FAKE_CAMERA_HEIGHT` | Fake camera height. |
| `FAKE_CAMERA_FPS` | Fake camera FPS. |
| `FAKE_FRAME_CACHE` | Number of generated frames reused by the fake publisher. |
| `FAKE_STATE_RATE` | Fake state topic rate. |
| `FAKE_POINTS_RATE` | Fake point cloud rate. |
| `FAKE_POINT_COUNT` | Points per fake point cloud. |
| `FAKE_COLOR_SEED` | Stable color seed for generated camera images. |

## Runtime And Observability

| Variable | Purpose |
|---|---|
| `HORUS_LOG_KEEP` | Number of archived logs to keep per service. |
| `HORUS_METRICS_PORT` | Optional Prometheus metrics port. Empty disables metrics. |
| `HORUS_LAUNCH_MONITOR` | Set `0` to prevent `launch` from opening the live console. |

Runtime files are written to `.run/` and ignored by git.

Each service has an owned supervisor, rotated logs (10 MiB plus three archives),
PID/start-time identity and a `*.service.json` state file. Startup survival is
reported separately from data readiness. The supervisor retries later crashes
with bounded backoff, up to five failures in five minutes. Early startup
failures roll back only services newly started by that launch. Repeating a
launch preserves healthy services; stop waits up to ten seconds for graceful
cleanup before escalation.
Forced cleanup checks both supervisor and child start-time identities before
stopping an owned child process group; it does not sweep unrelated processes.
Coturn is a shared system service, not an instance-owned child. Launch preserves
an already-running relay and ordinary `stop` leaves it unchanged; system-level
relay shutdown must be explicit. This does not change its credentials or policy.
Codec probes are cached per instance and check the actual configured camera
sizes/FPS, not an invented combined resolution. They prove codec availability,
not simultaneous camera capacity.

Media endpoints write `*.media.json` with per-track counts and frame age.
`WAIT` means no current data, `STALE` means old data, and `LIVE`/`PARTIAL` require
fresh frames/pose rather than a historical log message. Silent ROS publishers
do not trigger transport restart storms. Failed negotiation or active-sender
decode stalls coordinate a new offer; repeated media faults back off from
2 seconds to a 30-second maximum and reset after stable connection time.
Relay registration owns reconnect startup. Peer notifications identify the
endpoint session, so a delayed notification cannot discard an offer that
already belongs to that session. Outgoing negotiation messages wait for the
registration acknowledgement. Use matching connector versions on the sender,
receiver and relay for these lifecycle guarantees; legacy relays without
session identity retain the compatible but less precise replacement behavior.
Field-teammate pose/video reads have absolute deadlines and fresh-data status.

H.264 timing samples match actual RTP timestamps to decoded PTS and retain at
most 10,000 samples. Their boundary is **packetization to decoded output**, not
capture-to-display or glass-to-glass. Negative corrected timings are invalid.
The benchmark renderer excludes these samples from capture-latency/SLA
comparison curves rather than inventing fresh-frame FPS.

## Fleet

Fleet is for one operator machine receiving multiple robot rooms.

```bash
./horus fleet launch config/fleet.example.json --role machine
./horus fleet status config/fleet.example.json
./horus fleet stop config/fleet.example.json
```

Each robot still runs a normal robot launch with its own room:

```bash
HORUS_ROOM=robot-a
ZENOH_NAMESPACE=/robot_a
./horus launch robot
```
