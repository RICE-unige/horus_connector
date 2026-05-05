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
- WebRTC carries H.264 camera traffic; robot encodes and machine decodes when hardware support is available.
- WebRTC `cmd-vel` DataChannel carries robot control commands such as `/cmd_vel`.

`config/zenoh_split.json5` intentionally keeps cameras and `/cmd_vel` out of Zenoh.

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

Bootstrap installs/probes Zenoh, GStreamer WebRTC, `gstreamer1.0-nice`, and the best available H.264 encoder/decoder on `robot` and `machine`. The `cloud` role installs only Zenoh plus signaling dependencies and skips media packages/hardware probing. If passwordless sudo is unavailable, bootstrap prints the apt command to run once.

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
```

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
