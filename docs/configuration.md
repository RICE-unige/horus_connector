# HORUS Connector Configuration

Use `./horus setup` for normal configuration. Edit `.env` directly only when you need advanced control.

## Core Environment

| Variable | Used by | Purpose |
|---|---|---|
| `HORUS_ROLE` | all | `robot`, `machine`, or `cloud`. |
| `HORUS_ROOM` | robot, machine | Pairing room. Use one stable room per robot, for example `robot-a`. |
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

## Zenoh

| Variable | Purpose |
|---|---|
| `ZENOH_PORT` | Zenoh TCP port. Default: `7447`. |
| `ZENOH_VERSION` | Bridge version installed by bootstrap. |
| `ZENOH_CONFIG` | `auto` selects the role config, or set a custom JSON5 path. |
| `ZENOH_NAMESPACE` | Optional namespace for robot topics, for example `/robot-a`. |
| `HORUS_ZENOH_ENABLED` | Set `0` to disable Zenoh for a run. |

Role configs:

| Role | Config |
|---|---|
| `robot` | `config/zenoh_robot.json5` |
| `machine` | `config/zenoh_machine.json5` |
| `cloud` | `config/zenoh_cloud.json5` |

The default robot profile exports only state topics:

```text
/tf
/tf_static
/odom
/joint_states
/scan
/points
```

Camera and `/cmd_vel` are intentionally excluded from Zenoh. Camera uses WebRTC media; `/cmd_vel` uses the WebRTC DataChannel.

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

## WebRTC Camera And Control

| Variable | Purpose |
|---|---|
| `WEBRTC_MEDIA_MODE` | `h264` is the supported path. `jpeg` is legacy direct-mode fallback only. |
| `WEBRTC_SIGNAL_PORT` | WebRTC signaling port. Default: `8765`. |
| `WEBRTC_DURATION` | Runtime duration in seconds. Use `86400` for long runs. |
| `WEBRTC_ICE_SERVERS` | Comma-separated STUN/TURN servers. |
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
| `VIDEO_BITRATE_KBIT` | Starting H.264 bitrate. |
| `WEBRTC_ADAPTIVE_BITRATE` | Enable adaptive bitrate control. Default: `1`. |
| `WEBRTC_MIN_BITRATE_KBIT` | Minimum adaptive bitrate. |
| `WEBRTC_MAX_BITRATE_KBIT` | Maximum adaptive bitrate. |
| `WEBRTC_H264_KEY_INT_MAX` | H.264 keyframe interval. |
| `WEBRTC_CMD_WATCHDOG_TIMEOUT` | Robot command timeout before publishing zero velocity. |
| `WEBRTC_CMD_RATE_LIMIT_HZ` | Maximum accepted command rate. |

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
| `TURN_MIN_PORT` | Minimum relay UDP port. |
| `TURN_MAX_PORT` | Maximum relay UDP port. |
| `TURN_REALM` | TURN realm. |
| `TURN_USER` | TURN username. |
| `TURN_PASSWORD` | TURN password. |

Example:

```bash
HORUS_CLOUD_RUN_TURN=1
TURN_USER=horus
TURN_PASSWORD=<password>
WEBRTC_ICE_SERVERS=stun:stun.l.google.com:19302,turn://horus:<password>@cloud.example.com:3478
./horus bootstrap cloud
```

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
ZENOH_NAMESPACE=/robot-a
./horus launch robot
```
