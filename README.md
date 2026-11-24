<p align="center">
  <img src="docs/horus_logo_black.svg#gh-light-mode-only" alt="HORUS logo" height="90">
  <img src="docs/horus_logo_white.svg#gh-dark-mode-only" alt="HORUS logo" height="90">
</p>

# HORUS Connector

Connect robots and operators in [HORUS](https://rice-unige.github.io/horus/). Zenoh transports ROS 2 state, sensor and task traffic. WebRTC carries H.264 camera streams and optional voice. Connect directly over a LAN or VPN, or through an Internet hub when operators cannot join the robot network.

## Quick Start

Use Ubuntu 24.04 with ROS 2 Jazzy, or Ubuntu 22.04 with ROS 2 Humble. For Jetson, install the matching JetPack first; bootstrap leaves the NVIDIA driver stack untouched. See [platform requirements](docs/configuration.md#installation) before choosing a runtime.

```bash
git clone https://github.com/RICE-unige/horus_connector.git
cd horus_connector
./horus setup
./horus bootstrap
./horus launch
```

If ROS is not installed, [configure the ROS apt repository](docs/configuration.md#installation) and run `HORUS_INSTALL_ROS=1 ./horus bootstrap` instead.

The setup wizard selects the role, topology and local ROS domain. Repeat on each host:

| Role | Host |
| --- | --- |
| `robot` | Robot or fleet computer with access to the local ROS graph |
| `machine` | Operator computer on the same LAN as the Quest |
| `cloud` | Internet hub for deployments without direct reachability |
| `teammate` | Relay computer for a HoloLens Field teammate |

Start the `cloud` before its clients in hub mode. In direct mode, start the `machine` before the robot. In HORUS on the headset, connect to the operator computer's LAN address and HORUS bridge port, not the hub address.

## Join an Existing Hub

Replace `RELEASE_TAG` with the connector tag supplied by your fleet administrator. Install the matching runtime archive and private operator profile as directed. Keep profiles and credentials out of Git.

```bash
git clone --branch RELEASE_TAG https://github.com/RICE-unige/horus_connector.git
cd horus_connector
export HORUS_ENV="$HOME/.config/horus/operator.env"
./horus bootstrap machine
./horus doctor machine
./horus launch machine --no-monitor
HORUS_DDS_REQUIRE_SAMPLE=1 ./horus doctor machine
```

Before enabling motion, verify the expected robot poses and cameras in HORUS and test control handoff and link-loss recovery with motion disabled.

## Diagnostics

```bash
./horus status
./horus doctor
./horus logs zenoh
./horus stop
```

- [Configuration](docs/configuration.md): installation, ROS domains, runtime bundles and networking
- [Field teammate](docs/field_teammate_voice.md): HoloLens relay and voice
- [Reliability and performance](docs/performance.md): transport, recovery and validation
- [HORUS Documentation](https://rice-unige.github.io/horus_sdk/): SDK tutorials, ROS 2 bridge, and MR integration

Developed by RICE Lab at the University of Genoa. [Apache 2.0](LICENSE) | [Report an issue](https://github.com/RICE-unige/horus_connector/issues)
