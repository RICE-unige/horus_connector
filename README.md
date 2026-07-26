<p align="center">
  <img src="docs/horus_logo_black.svg#gh-light-mode-only" alt="HORUS logo" height="90">
  <img src="docs/horus_logo_white.svg#gh-dark-mode-only" alt="HORUS logo" height="90">
</p>

<p align="center"><em>Holistic Operational Reality for Unified Systems</em></p>

![ROS2](https://img.shields.io/badge/ROS2-Humble%20%7C%20Jazzy-22314E)
![Zenoh](https://img.shields.io/badge/Zenoh-ROS2DDS-blue)
![WebRTC](https://img.shields.io/badge/WebRTC-camera%20%2B%20voice-0A7)
[![License](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)

`horus_connector` is the transport layer for HORUS robot management. It connects robot ROS 2 graphs to operator machines over the internet, VPN, Tailscale, or LAN.

## What It Does

- **Zenoh** carries ROS 2 state topics such as TF, odometry, joint states, LaserScan, and point clouds.
- **WebRTC** carries one or more low-latency H.264 camera tracks in a per-robot session and publishes each decoded track back to ROS 2 on the machine.
- **HORUS Voice** uses authenticated WebRTC signaling for direct Opus calls between the MR operator and a communicative field teammate. The connector never terminates or records call media.
- **ROS 2 commands** such as `/cmd_vel` stay on ROS 2 and cross the Zenoh bridge.

The cloud role is only a hub: it routes Zenoh traffic and relays WebRTC signaling. Camera encoding/decoding stays on the robot and machine.

Version-2 camera configurations use one peer connection per robot, with multiple named video tracks, expiring TURN credentials when configured, per-client signaling authorization, and bitrate adaptation driven by WebRTC packet-loss and RTT statistics. Existing version-1 per-camera rooms remain supported.

## Install

```bash
git clone https://github.com/RICE-unige/horus_connector.git
cd horus_connector
./horus setup
./horus bootstrap robot    # or machine/teammate/cloud
```

Run `./horus setup` on each computer. Setup covers topology, network address,
ROS 2, domain ID, and role-specific media. The teammate role also configures
the HoloLens endpoint and its restricted Zenoh topic policy.

ROS 2 must already be installed on `robot`, `machine`, and `teammate` computers. The `cloud` role does not need a local ROS 2 graph.

Before robot, machine, or teammate launch, HORUS checks DDS visibility and
records `.run/dds_env.json`. If the launch terminal can see topics but the
bridge cannot safely transport them, `./horus doctor <role>` reports domain,
discovery, interface, Docker, or Zenoh filter problems.

## Roles

| Role | Runs on | Purpose |
|---|---|---|
| `robot` | Robot computer | Sends camera/state and optionally receives bounded control commands. |
| `machine` | Operator computer | Receives camera/state and can send control commands when enabled. |
| `teammate` | Field teammate computer | Converts HoloLens streams to ROS 2 and transports the allowed teammate topics through Zenoh. |
| `cloud` | Public VM or server | Shared hub for remote deployments. |

## Topologies

```text
hub:    robot(s), teammate(s) -> cloud <- machine(s)
direct: robot, teammate       -> machine    # VPN/Tailscale/LAN
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

# optional field teammate endpoint
./horus launch teammate
```

Direct mode:

```bash
# machine first
./horus launch machine

# robot second
./horus launch robot

# optional field teammate endpoint
./horus launch teammate
```

`./horus launch <role>` starts the backend services and opens the live console. Use `--no-monitor` for backend-only relaunches:

```bash
./horus launch machine --no-monitor
```

For HoloLens long-throw dense mapping, run `./horus setup teammate` once and
select the `mapping` video profile before `./horus launch teammate`. That
profile uses high-quality 640x360@15 PV color so Research Mode depth and head
tracking can run concurrently. The relay publishes a nearest fresh head-camera
pose when Windows omits locatable PV metadata; `/field_teammate_1/status`
reports native and fallback pose counts separately.

## Common Commands

```bash
./horus status
./horus monitor
./horus doctor robot
./horus logs zenoh
./horus logs webrtc
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

Advanced configuration is documented in [docs/configuration.md](docs/configuration.md), including environment variables, Zenoh topic filters, priorities, QUIC/TLS, TURN, hardware codec preferences, and fleet examples.

Field-teammate automatic setup, call roles, and the later production-security switch are documented in [docs/field_teammate_voice.md](docs/field_teammate_voice.md).

## Network

- Zenoh: TCP `7447`; optional QUIC on UDP `7447`
- WebRTC signaling: TCP `8765`
- WebRTC media: UDP/ICE end-to-end
- TURN fallback, when enabled on cloud: TCP/UDP `3478` and UDP `49152-65535`

Zenoh QUIC uses TLS certificates. In `auto` mode HORUS uses QUIC first when TLS is configured and keeps TCP as fallback:

```bash
./horus quic setup-server          # cloud or direct-mode listener
./horus quic install-cert cert.pem # robot and machine clients
```

Legacy WebRTC `/cmd_vel` publishing is disabled by default. Enable it only for robot profiles with known velocity limits:

```bash
export WEBRTC_CONTROL_ENABLED=1
export HORUS_WEBRTC_CONTROL_TOKEN=<shared-control-token>
export WEBRTC_CMD_MAX_LINEAR_MPS=0.5
export WEBRTC_CMD_MAX_ANGULAR_RPS=1.0
```

For hub deployments that expose the signaling relay, also set `HORUS_WEBRTC_RELAY_TOKEN` on every authorized endpoint. Use unauthenticated relay/control flags only on trusted lab networks.

## Contact

For questions or support:

Omotoye Shamsudeen Adekoya<br>
Email: omotoye.adekoya@edu.unige.it

## Acknowledgments

This project is part of PhD research at the University of Genoa, under the supervision of:

Prof. Carmine Recchiuto<br>
Prof. Antonio Sgorbissa

Developed by RICE Lab, University of Genoa.
