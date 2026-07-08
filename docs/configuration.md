# HORUS Connector Configuration

Use `./horus setup` for normal configuration. Edit `.env` directly only when you need advanced control.

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
| `HORUS_DDS_INTERFACE` | Optional CycloneDDS interface name for the connector bridge participant. |
| `HORUS_DDS_PREFLIGHT_TOPIC_TIMEOUT` | Timeout in seconds for the preflight `ros2 topic list` probe. Default: `5.0`. |

## DDS Preflight

`./horus launch <role>` runs a DDS preflight before starting Zenoh unless `HORUS_SKIP_DDS_PREFLIGHT=1` or `--skip-dds-preflight` is used. The preflight does not replace a partner robot's DDS setup. It snapshots the launch environment, renders a connector-owned CycloneDDS profile for the bridge participant, and classifies issues before the bridge starts.

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

For Docker bridge runtime, HORUS mounts the rendered CycloneDDS XML into the bridge container, passes the DDS environment explicitly, and runs the bridge with host networking.

## Zenoh

| Variable | Purpose |
|---|---|
| `ZENOH_PORT` | Zenoh TCP port. Default: `7447`. |
| `ZENOH_VERSION` | Bridge version installed by bootstrap. |
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

The default robot profile exports robot ROS topics through Zenoh and imports command topics from the operator machine. The machine profile imports robot topics and exports only command topics:

```text
/cmd_vel
/<robot>/cmd_vel
```

The operator camera path uses WebRTC media. ROS image topics can still cross Zenoh when the robot profile is left open for experiments, but that is not required for the low-latency camera view. For bandwidth-limited runs, use a custom `ZENOH_CONFIG` to filter high-rate image topics.

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

The `teammate` role is for a field teammate endpoint that relays local teammate/HoloLens data into the HORUS ROS 2 graph through Zenoh. It is not a cloud hub and it does not run the robot camera WebRTC sender/receiver path.

Normal setup:

```bash
./horus setup
./horus bootstrap teammate
./horus launch teammate
```

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
| `WEBRTC_DURATION` | Runtime duration in seconds. Use `86400` for long runs. |
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
| `HORUS_STREAMS_CONFIG` | Optional JSON config for multiple WebRTC camera streams. Usually written by `./horus setup`. |
| `VIDEO_BITRATE_KBIT` | Starting H.264 bitrate. |
| `WEBRTC_ADAPTIVE_BITRATE` | Enable adaptive bitrate control. Default: `1`. |
| `WEBRTC_CONTROL_ENABLED` | Legacy/debug option for WebRTC DataChannel publishing to `ROS_CMD_TOPIC`. Keep `0` for normal runs. Default: `0`. |
| `WEBRTC_ENABLE_CONTROL` | Legacy alias for `WEBRTC_CONTROL_ENABLED`. |
| `HORUS_WEBRTC_CONTROL_TOKEN` | Shared token required on robot and authorized operator paths when WebRTC control is enabled. |
| `WEBRTC_ALLOW_UNAUTHENTICATED_CONTROL` | Allow WebRTC control commands without a token for trusted lab tests only. Default: `0`. |
| `WEBRTC_CMD_MAX_LINEAR_MPS` | Clamp each linear Twist component. Default: `0.5`. |
| `WEBRTC_CMD_MAX_ANGULAR_RPS` | Clamp each angular Twist component. Default: `1.0`. |
| `HORUS_WEBRTC_RELAY_TOKEN` | Shared token required by public hub signaling relays. |
| `HORUS_WEBRTC_RELAY_ALLOW_UNAUTHENTICATED` | Allow a public signaling relay without a token for trusted test networks. Default: `0`. |
| `WEBRTC_MIN_BITRATE_KBIT` | Minimum adaptive bitrate. |
| `WEBRTC_MAX_BITRATE_KBIT` | Maximum adaptive bitrate. |
| `WEBRTC_H264_KEY_INT_MAX` | H.264 keyframe interval. |
| `WEBRTC_CMD_WATCHDOG_TIMEOUT` | Robot command timeout before publishing zero velocity. |
| `WEBRTC_CMD_RATE_LIMIT_HZ` | Maximum accepted command rate. |

### Multiple Camera Streams

Use `./horus setup` and choose the number of WebRTC camera streams. The setup writes `config/webrtc_streams.json`, which is ignored by git because each deployment usually has different cameras and room names.

Each stream has its own internal WebRTC room, input topic, output topic, resolution, frame rate, and bitrate. `./horus launch robot` starts one sender per enabled stream, and `./horus launch machine` starts one receiver per enabled stream. The machine republishes each decoded stream to its configured ROS 2 output topic.

Example template:

```bash
cp config/webrtc_streams.example.json config/webrtc_streams.json
./horus setup
```

Per-stream logs use the stream service name:

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

Robot and machine roles do not run TURN. They only need the same `WEBRTC_ICE_SERVERS` value that points to the cloud relay.

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
