<p align="center">
  <img src="docs/horus_logo_black.svg#gh-light-mode-only" alt="HORUS logo" height="90">
  <img src="docs/horus_logo_white.svg#gh-dark-mode-only" alt="HORUS logo" height="90">
</p>

<p align="center"><em>Holistic Operational Reality for Unified Systems</em></p>

![ROS2](https://img.shields.io/badge/ROS2-Humble%20%7C%20Jazzy-22314E)
![Zenoh](https://img.shields.io/badge/Zenoh-ROS2DDS-blue)
![WebRTC](https://img.shields.io/badge/WebRTC-camera%20%2B%20cmd_vel-0A7)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)

> [!IMPORTANT]
> `horus_connector` owns the internet/VPN transport layer for HORUS robot management. It is the bridge between remote robot ROS 2 graphs and operator/cloud machines.

## Purpose

- Zenoh carries ROS 2 state topics: TF, odometry, joint states, LaserScan, and limited point clouds.
- WebRTC carries camera traffic as H.264. The robot subscribes to a ROS 2 raw image topic, encodes it, and the machine decodes it back into a ROS 2 image topic.
- WebRTC `cmd-vel` DataChannel carries robot control commands such as `/cmd_vel`.

`config/zenoh_split.json5` intentionally keeps cameras and `/cmd_vel` out of Zenoh. The decoded WebRTC image is published only on the machine-side ROS 2 graph unless you explicitly bridge that topic elsewhere.

## Benchmark

![Camera transport benchmark](docs/transport_benchmark.svg)

Mode B 1080p30/720p30 camera benchmark. Freshness uses a 150 ms clock-normalized deadline.

## Roles

| Role | Responsibility |
|---|---|
| `robot` | Robot-side system. Sends cameras and receives `/cmd_vel`. |
| `machine` | Operator machine. Receives cameras and connects to robot state. |
| `cloud` | Single Zenoh router and WebRTC signaling relay for hub deployments. |

## Topologies

```text
hub:    robot(s) -> cloud <- machine(s)
direct: robot    -> machine    # VPN/Tailscale/LAN
```

Use `hub` when robots and machines cannot reach each other directly. Use `direct` when Tailscale/VPN/LAN gives the robot a reachable machine IP.

In hub mode, the cloud does not encode or decode camera streams. It only routes Zenoh and relays WebRTC signaling; media and `cmd_vel` stay between robot and machine through ICE/TURN.

## Setup

```bash
cd ~/horus_connector
./horus init
nano .env
./horus bootstrap robot     # or machine/cloud
```

Bootstrap installs/probes Zenoh, GStreamer WebRTC, `gstreamer1.0-nice`, and the best available H.264 encoder/decoder on `robot` and `machine`. The `cloud` role installs only Zenoh plus signaling dependencies and skips media packages/hardware probing.

Each machine normally only needs:

```bash
git clone <repo-url>
cd horus_connector
./horus init
nano .env
./horus bootstrap robot     # robot-side system
./horus bootstrap machine   # operator-side system
```

Bootstrap asks for sudo when system packages are needed. If the machine has no interactive sudo, it prints the exact apt command to run once.

Supported hardware paths:

- NVIDIA Jetson/ARM64: Zenoh `aarch64`, `nvv4l2h264enc`, `nvv4l2decoder`, and NVMM conversion when JetPack/L4T GStreamer packages are available.
- Jetson Orin NX / recent JetPack: native Zenoh plus NVIDIA V4L2 H.264 should work after bootstrap.
- JetPack 4 / L4T R32: uses Docker fallback for Zenoh because the upstream ARM64 binary needs newer glibc than Ubuntu 18.04 provides.
- Intel NUC/Linux: VAAPI packages, `/dev/dri` probing, `vah264enc`/`vaapih264enc`, and hardware decode are used where available.
- Generic Linux: exposed hardware acceleration when available, otherwise x264/libav fallback.

Older JetPack 4 systems may not expose WebRTC DataChannel support in GStreamer 1.14. In that case H.264 camera streaming still works, but `/cmd_vel` over WebRTC requires a newer JetPack/GStreamer stack or a ROS/Zenoh fallback.

Required `.env` values:

```bash
HORUS_ROLE=robot            # robot | machine | cloud
HORUS_ROOM=robot1           # one room per robot-machine pair
HORUS_TOPOLOGY=hub          # hub | direct
HORUS_CLOUD_IP=34.6.77.21   # hub mode
HORUS_MACHINE_IP=           # direct mode or direct WebRTC target
ZENOH_NAMESPACE=/robot1     # unique per robot
ROS_DISTRO=jazzy
ROS_CMD_TOPIC=/cmd_vel
WEBRTC_ROS_IMAGE_INPUT_TOPIC=/camera/image_raw
WEBRTC_ROS_IMAGE_OUTPUT_TOPIC=/camera/webrtc/image_raw
```

Set `ROS_DISTRO` to the ROS 2 distro actually installed on the machine, for example `humble` or `jazzy`.

Camera path:

```text
robot ROS 2 Image -> WebRTC H.264 -> machine ROS 2 Image
```

Set `WEBRTC_VIDEO_SOURCE=ros2` on the robot and `WEBRTC_VIDEO_OUTPUT=ros2` on the machine. Use `WEBRTC_VIDEO_OUTPUT=both` if the machine should also open a local GStreamer video sink.

## Launch

Hub mode:

```bash
# cloud
./horus launch cloud

# each robot
./horus launch robot

# each operator machine
./horus launch machine
```

Direct VPN/Tailscale mode:

```bash
# machine first
./horus launch machine

# robot second
./horus launch robot
```

Operations:

```bash
./horus status
./horus logs zenoh
./horus logs webrtc
./horus stop
```

## Network

- Zenoh: TCP `7447`.
- WebRTC signaling relay: TCP `8765`.
- WebRTC media/control: UDP/ICE end-to-end, or configure TURN with `WEBRTC_ICE_SERVERS`.

Runtime files are written to `.run/` and are ignored by git.

Run `./scripts/gst_h264_smoke_test.sh` after bootstrap to confirm the selected encoder/decoder path.

## Contact

For questions or support:

Omotoye Shamsudeen Adekoya  
Email: omotoye.adekoya@edu.unige.it

## Acknowledgments

This project is part of PhD research at the University of Genoa, under the supervision of:

Prof. Carmine Recchiuto  
Prof. Antonio Sgorbissa

Developed by RICE Lab, University of Genoa.
