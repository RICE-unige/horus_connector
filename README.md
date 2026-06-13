<p align="center">
  <img src="docs/horus_logo_black.svg#gh-light-mode-only" alt="HORUS logo" height="90">
  <img src="docs/horus_logo_white.svg#gh-dark-mode-only" alt="HORUS logo" height="90">
</p>

<p align="center"><em>Holistic Operational Reality for Unified Systems</em></p>

![ROS2](https://img.shields.io/badge/ROS2-Humble%20%7C%20Jazzy-22314E)
![Zenoh](https://img.shields.io/badge/Zenoh-ROS2DDS-blue)
![WebRTC](https://img.shields.io/badge/WebRTC-camera%20%2B%20cmd_vel-0A7)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)

`horus_connector` is the transport layer for HORUS robot management. It connects robot ROS 2 graphs to operator machines over the internet, VPN, Tailscale, or LAN.

## What It Does

- **Zenoh** carries ROS 2 state topics such as TF, odometry, joint states, LaserScan, and point clouds.
- **WebRTC** carries camera video as low-latency H.264 and publishes the decoded stream back to ROS 2 on the machine.
- **WebRTC DataChannel** carries control commands such as `/cmd_vel`.

The cloud role is only a hub: it routes Zenoh traffic and relays WebRTC signaling. Camera encoding/decoding stays on the robot and machine.

## Install

```bash
git clone https://github.com/RICE-unige/horus_connector.git
cd horus_connector
./horus setup
./horus bootstrap robot    # or machine/cloud
```

Run `./horus setup` on each computer. It asks for the role, topology, robot room name, network address, ROS 2 setup, domain ID, and camera profile.

ROS 2 must already be installed on `robot` and `machine` computers. The `cloud` role does not need a local ROS 2 graph.

## Roles

| Role | Runs on | Purpose |
|---|---|---|
| `robot` | Robot computer | Sends camera/state and receives control commands. |
| `machine` | Operator computer | Receives camera/state and sends control commands. |
| `cloud` | Public VM or server | Shared hub for remote deployments. |

## Topologies

```text
hub:    robot(s) -> cloud <- machine(s)
direct: robot    -> machine    # VPN/Tailscale/LAN
```

Use `hub` when devices cannot reach each other directly. Use `direct` when VPN, Tailscale, or LAN gives the robot a reachable machine address.

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

Direct mode:

```bash
# machine first
./horus launch machine

# robot second
./horus launch robot
```

`./horus launch <role>` starts the backend services and opens the live console. Use `--no-monitor` for backend-only relaunches:

```bash
./horus launch machine --no-monitor
```

## Common Commands

```bash
./horus status
./horus monitor
./horus logs zenoh
./horus logs webrtc
./horus doctor
./horus stop
```

Synthetic test data:

```bash
./horus fake-data robot-a
```

Fleet receiver mode on one operator machine:

```bash
./horus fleet launch config/fleet.example.json --role machine
./horus fleet status config/fleet.example.json
./horus fleet stop config/fleet.example.json
```

Fleet starts one machine-side receiver per robot room.

## Configuration

Most users should use:

```bash
./horus setup
```

Advanced configuration is documented in [docs/configuration.md](docs/configuration.md), including environment variables, Zenoh topic filters, priorities, TURN, hardware codec preferences, and fleet examples.

## Benchmark

![Camera transport benchmark](docs/transport_benchmark.svg)

The benchmark compares practical camera transport paths using fresh-frame delivery and latency. WebRTC is used for camera streams; Zenoh remains focused on robot state.

## Network

- Zenoh: TCP `7447`
- WebRTC signaling: TCP `8765`
- WebRTC media/control: UDP/ICE end-to-end
- TURN fallback, when enabled on cloud: TCP/UDP `3478` and UDP `49152-65535`

## Contact

For questions or support:

Omotoye Shamsudeen Adekoya<br>
Email: omotoye.adekoya@edu.unige.it

## Acknowledgments

This project is part of PhD research at the University of Genoa, under the supervision of:

Prof. Carmine Recchiuto<br>
Prof. Antonio Sgorbissa

Developed by RICE Lab, University of Genoa.
