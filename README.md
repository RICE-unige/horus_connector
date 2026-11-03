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
- **WebRTC** carries latency-sensitive H.264 camera tracks. The native HORUS path forwards encoded packets to the headset without converting received video back to ROS images. The legacy decoded-ROS camera mode remains optional.
- **HORUS Voice** uses authenticated WebRTC signaling for direct Opus calls between the MR operator and a communicative field teammate. The connector never terminates or records call media.
- **Control** uses the native lease-checked WebRTC path when configured; ordinary ROS topics, tasks and service/action traffic remain supported through Zenoh. Robot-side commands ultimately reach their registered ROS interfaces.

The cloud role is only a hub: it routes Zenoh traffic and relays WebRTC signaling. In the native packet-relay path, video is encoded at the robot site and decoded by the receiving headset/client, not by the hub or operator-side relay.

Version-2 camera configurations use one peer connection per operator room, with multiple named video tracks, expiring TURN credentials when configured, per-client signaling authorization, and bitrate adaptation driven by WebRTC packet-loss and RTT statistics. A robot can serve additional operator rooms with `HORUS_WEBRTC_ADDITIONAL_ROOMS`; each extra room starts a separate encoder, so test the robot's media capacity before adding viewers. Existing version-1 per-camera rooms remain supported.

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

Zenoh ROS 2 DDS is pinned to **1.10.2** in `config/zenoh-release.json`.
To update only the project-local bridge and matching plugin, without rerunning
apt, ROS, certificates or camera setup:

```bash
./horus update-zenoh
# An exact older release can be selected explicitly:
./horus update-zenoh 1.9.0
```

The updater checks the upstream archive digest and exact executable version,
stages the pair before replacement and retains rollback files under
`.zenoh-backups/`. It does not start or restart services. A running bridge keeps
its existing executable until you restart that connector yourself. Existing
`.env` files are not rewritten: keep `ZENOH_VERSION` aligned with the desired
release for subsequent full bootstrap runs. Binary and Docker defaults use the
same pinned release; the host-incompatibility Docker fallback remains available.

Before robot, machine, or teammate launch, HORUS checks DDS visibility and
records `.run/dds_env.json`. If the launch terminal can see topics but the
bridge cannot safely transport them, `./horus doctor <role>` reports domain,
discovery, interface, Docker, or Zenoh filter problems.

See [Reliability and Performance](docs/performance.md) for camera timestamp and
calibration contracts, traffic priorities, capability-derived routes, supervised
recovery, capacity budgets and reproducible qualification checks. These do not
change the deployment's existing authentication or network-access policy.

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

## Development Checks

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-webrtc/bin/python -m pytest tests -q
for script in horus scripts/*.sh tests/*.sh; do bash -n "$script" || exit 1; done
bash tests/run_webrtc_bundled_smoke.sh
HORUS_WEBRTC_TEST_ORDER=machine-first bash tests/run_webrtc_bundled_smoke.sh
HORUS_WEBRTC_TEST_RECONNECT=1 bash tests/run_webrtc_bundled_smoke.sh
```

The media checks use two synthetic H.264 tracks over a real local WebRTC connection. They require GStreamer `webrtcbin`, NICE, `x264enc`, a decoder, and Python `websockets>=12`. They verify startup ordering and receiver reconnect without restarting the robot endpoint; they do not establish WAN latency, Quest display performance, or physical-robot safety.

Additional native checks (ROS 2 Jazzy sourced, Python 3.10+ and GStreamer 1.22+ for bundled cameras):

```bash
.venv-webrtc/bin/python tests/check_ros_webrtc.py --letterbox --latched-info --output .run/checks/ros-camera
.venv-webrtc/bin/python tests/check_zenoh_routes.py --output .run/checks/zenoh
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bulk --custom-routes --output .run/checks/zenoh-bulk
.venv-webrtc/bin/python tests/check_zenoh_routes.py --hub --bulk --output .run/checks/zenoh-hub-bulk
.venv-webrtc/bin/python tests/check_connector_launch.py --output .run/checks/launcher
.venv-webrtc/bin/python tests/check_media_recovery.py --track-only --output .run/checks/media-recovery
.venv-webrtc/bin/python tests/check_supervised_media_recovery.py --output .run/checks/supervised-recovery
.venv-webrtc/bin/python tests/check_multi_operator_media.py --tracks 8 --output .run/checks/two-operators
```

Use separate output directories for each trial. These checks isolate their ROS
domains (193-197), ports and owned processes. They exercise original image
headers/scaled calibration, publisher silence, receiver reset, late static-TF
replay, HORUS return routes, mixed bulk traffic, both camera launch modes and
idempotent relaunch and repeated receiver/sender/relay recovery. The Zenoh check
reports initial discovery delays separately from steady-state timings after a
declared three-second source warmup. They are localhost acceptance checks, not WAN/Quest or
hardware capacity results.

Normal media runs until stopped (`WEBRTC_DURATION=0`). Monitor current per-track
frame age/counts, not just an open socket or running PID. See
[configuration](docs/configuration.md) for recovery, metadata and jitter budgets,
and [benchmark methodology](docs/benchmark_methodology.md) for timing boundaries.

Signaling callbacks queue sends without waiting for connectivity. Pending sends are bounded, failed sends retain their position until reconnect, and shutdown closes owned sockets and listeners. ROS image publishing validates dimensions and repacks padded GStreamer rows before publishing tightly packed messages.

## Contact

For questions or support:

Omotoye Shamsudeen Adekoya<br>
Email: omotoye.adekoya@edu.unige.it

## Acknowledgments

This project is part of PhD research at the University of Genoa, under the supervision of:

Prof. Carmine Recchiuto<br>
Prof. Antonio Sgorbissa

Developed by RICE Lab, University of Genoa.
