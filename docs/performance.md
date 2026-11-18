# Reliability and Performance

See [experiment runtime configuration](configuration.md#pinned-experiment-runtime)
for the authenticated hub, native runtime and cloud media setup.

The connector keeps ROS state/maps on Zenoh and camera media on WebRTC.
The native remote-control profile uses a separate WebRTC control peer with
fleet-side leases; the compatibility profile retains ROS/Zenoh control.
Use `./horus launch <role>` rather than running media scripts directly: the
launcher validates the selected runtime and supervises only its own processes.
Existing authentication, certificates and network-access settings are unchanged.

## Routing and Backpressure

- Dynamic/static TF is not sampled as whole messages. Independent publishers
  can carry different child frames; a 20 Hz topic cap can lose entire frames.
- Control/leases precede transforms, state and bulk maps/model chunks. An unknown
  `/horus/**` topic is not automatically classified as highest-priority control.
- Reliable ROS routes use Zenoh blocking congestion control; best-effort routes
  may drop under pressure. This preserves the requested reliability at the
  connector boundary, but can backpressure DDS. It does not make finite DDS
  histories, disconnected sessions or a saturated TCP link lossless.
- Incremental maps still need producer revision/checkpoint/retry semantics.
  The connector must not coalesce an arbitrary delta or remove map deletions.
  TCP priorities cannot preempt bytes already sent or eliminate loss recovery.
- Choose explicit source rates/bitrate budgets for the actual link. HORUS does
  not silently lower geometry, resolution or configured camera FPS.

For interfaces not covered by the standard profiles, set
`HORUS_ROUTES_CONFIG=/absolute/path/routes.json` on both endpoints. Example:

```json
{
  "registrations": [{
    "control": {
      "teleop": {"command_topic": "/fmu/in/trajectory_setpoint"},
      "tasks": {"waypoint": {"path_topic": "/navigation/request_waypoints"}}
    }
  }],
  "service_clients": ["/mission/reset"],
  "action_clients": ["/navigate_to_pose"],
  "priorities": {"/mission/command": "1:express"},
  "max_frequencies": {"/overview/cloud": 2}
}
```

Capability-derived command topics are added in the appropriate direction and
receive control priority. Explicit interface categories are role-local: pair
`service_clients` with the other endpoint's `service_servers`, and similarly
for actions. A route does not create the robot-side interface or message type.
Generated configuration is stored in the selected run directory. TF rate caps
are rejected; other rate limits are opt-in and may be inappropriate for deltas.

To prevent a camera also crossing Zenoh, set its stream `transport` to
`webrtc_only` and enable `HORUS_ZENOH_CAMERA_POLICY=configured` on the robot.
The compatibility default keeps existing routes. Automatic exclusion supports
the standard robot publisher profile; custom profiles must define their own
explicit routing. This affects topic routing, not ports or authentication.

## Camera Contract

### Weak-Link Transport

Set `HORUS_ZENOH_TRAFFIC_LANES=1` on both binary endpoints to separate urgent
priorities 0-1, sensing priorities 2-5 and bulk priorities 6-7 onto three native
TCP links to the same endpoint. QoS and `max_links=3` are required; the launcher
validates them and saves the generated configuration in the run directory.
Priority senders use explicitly connected Zenoh peer mode, with no implicit
listener or multicast/automatic peer connection. Topology gossip is exchanged
only on configured links for routing and gateway de-duplication; automatic
connection to advertised peers is disabled. Zenoh 1.10 client-mode recovery otherwise restores only
one link after the remote bridge restarts. Ordinary non-lane clients are unchanged.
TF/control use express scheduling. Batching is bounded to **1 millisecond**;
this is not the microsecond unit used by `wait_before_drop`. TCP send buffers
are bounded per link and transport compression is lossless. Separate links
avoid a single application queue, not shared Wi-Fi capacity or TCP loss recovery.

The opt-in `HORUS_ZENOH_SESSION_ISOLATION=1` profile instead runs three separate
sessions on `ZENOH_PORT`, `ZENOH_PORT+1` and `ZENOH_PORT+2`. Enable the same
profile at the hub and both endpoints, and make all three TCP ports reachable.
Hub endpoints use client mode; direct endpoints use peer mode and the listener
uses router mode. This distinction matters: these sessions disable gossip, so
peer-to-router connections alone do not establish the required hub routes.
The ordinary single-session priority-link profile above retains peer mode for
its existing multi-link reconnection behavior. Session isolation is not a
guarantee against a congested physical uplink; qualify it with TF age and
worst delivery gaps as well as average camera FPS.
Use `./horus doctor robot` or `./horus doctor machine` to check each of
the three destination ports. An open port is transport reachability, not
proof that subscriptions are matched or data is flowing.

For an Internet hub without a VPN, set `HORUS_ZENOH_SESSION_MTLS=1`
with `HORUS_ZENOH_SESSION_ISOLATION=1` and `ZENOH_TRANSPORT=tcp`
across the hub and its clients. Each role's `ZENOH_CONFIG` must set
`transport.link.tls.enable_mtls=true` and an absolute
`root_ca_certificate`. Supply `listen_certificate` and
`listen_private_key` at the hub; supply `connect_certificate` and
`connect_private_key` on each client. Use separate site identities,
with the hub's public address in its certificate SAN. Name verification
cannot be disabled in this profile; there is no plaintext fallback.
Use a new run directory/port set for an evaluation or stop the old
session group before replacing its settings. A trusted client certificate
joins the transport, not a lease or topic permission.

Optional per-socket network markings can be set with
`HORUS_ZENOH_SESSION_DSCP=46,18,8` on both endpoints and the hub. These are
six-bit codepoints for real-time, sensing and bulk respectively; empty/unset
keeps existing unmarked sockets. The native Zenoh endpoint API applies them
without changing host/interface QoS, router rules or privileges. It expects a
full TOS byte internally, so HORUS shifts the validated codepoint and leaves
ECN bits clear. These hints do not reserve bandwidth: ISPs, VPNs and access
points may strip, remap or ignore them. Use an actual packet capture and
repeated delivery-gap measurements before relying on them at a new site.

With the stock bridge, a blocking bulk DDS callback can still stall unrelated
readers. The optional HORUS native patch gives priority 6-7 readers independent
draining workers while preserving their original DDS history/reliability.
Build the pinned source and locked dependencies without replacing your release:

```bash
python3 scripts/build_priority_bridge.py --help
python3 scripts/build_priority_bridge.py --work-dir /large/drive/zenoh-build \
  --output /large/drive/zenoh-priority/zenoh-bridge-ros2dds --toolchain stable
```

Select that verified artifact using `ZENOH_BRIDGE_BINARY` on both endpoints.
`config/zenoh-source.json` pins the upstream commit and patch hash; the builder
writes a provenance sidecar. `./horus environment` distinguishes patched/native
artifacts. No system bridge, firewall, interface or MTU setting is replaced.

`HORUS_ZENOH_BULK_BYTES_PER_SECOND` optionally paces aggregate outbound bulk
publication on the patched bridge (default 0, disabled). Set it **at the source**
below measured available throughput, leaving headroom for cameras and state.
It is not an automatic guarantee of bandwidth or low latency. For validated
HMSH-v2 full replacement pages on `/indexed` UInt8MultiArray topics, the bounded
queue retains the newest pending revision per chunk with FIFO fairness. It
preserves epoch resets/deletions and all transmitted geometry/colour; it never
coalesces arbitrary ROS deltas. `HORUS_ZENOH_MESH_QUEUE_BYTES` defaults to 128 MiB.
Other messages retain FIFO/backpressure. Finite DDS history can still overflow;
incremental producers need checkpoint/retry semantics and coverage checks.

If a site uses a modest-rate live depth stream and large map snapshots, test
assigning that specific depth topic priority `5` and the map topic `6` or
`7` on both endpoints. This keeps depth out of the bulk queue in
three-session mode without changing cloud/mesh geometry. It is an explicit
route policy, not a global default; verify sensing capacity before moving
high-rate depth there. A bulk cap is not a promise that every full snapshot
can arrive at its source rate. Verify checksum-matched progress and final
map coverage, and bound the source snapshot rate to the measured capacity.

### Compatibility ROS Video Adapter

This subsection describes `HORUS_MEDIA_RELAY_ENGINE=ros`. Use the native
packet relay below for the normal low-latency HORUS-to-Quest path. The
compatibility adapter intentionally exposes local encoded ROS topics.

The optional source sender uses the WebRTC-enabled ROS bridge executable
`horus_media_node`, built with `ENABLE_WEBRTC=ON`. Set
`HORUS_SOURCE_MEDIA_ENABLED=1` and `HORUS_SOURCE_MEDIA_BINARY` on the robot.
Optional encoder, bitrate, FPS, RMW and Fast DDS profile fields are listed in
`.env.example`. This process provides media only, not a second lease authority.
Control/state and WebRTC signaling remain ordinary connector routes.

Direct source-to-Quest WebRTC needs reachable ICE candidates. Tailscale on the
two PCs alone does not make a Quest a Tailscale peer. When direct ICE is not
available, set `HORUS_MEDIA_RELAY_CONFIG` on the machine to a JSON file:

```json
{
  "client_signal_topic": "/horus/remote/webrtc/client_signal",
  "server_signal_topic": "/horus/remote/webrtc/server_signal",
  "streams": [{
    "id": "front",
    "input_topic": "/robot/front/image_raw",
    "image_type": "raw",
    "output_topic": "/horus/media/front/h264",
    "frame_id": "robot/front_optical",
    "bitrate_kbps": 1500,
    "framerate": 15
  }]
}
```

Use the same signal topics on the source. The managed receiver starts on local
subscriber demand, receives source H.264 over WebRTC, depacketizes it and
publishes unchanged Annex-B access units locally as CompressedImage (`h264`).
The local native bridge forwards those units to Quest without a decoder or
encoder on the laptop. Configure its explicit `webrtc.preencoded_topics` list
with these outputs, and register those topics in the SDK as WebRTC cameras
with `image_type="compressed"`. Source image pixels and configured bitrate/FPS
are not silently reduced. Source encoding is shared by local consumers of the
same relay output; a different operator PC still needs its own source session.

Only then explicitly exclude redundant ROS camera WAN payloads with
`webrtc_only_topics` in `HORUS_ROUTES_CONFIG` on the robot. Include both raw and
JPEG topics if both would otherwise be forwarded. CameraInfo/TF are not
excluded. This is a configured media path, not discovery or proof of delivery.
The relay has bounded prediction-safe queues, keyframe forwarding, generation
guards and supervised retry/backoff. Health reports encoded publication, not
Quest display FPS. Relay timestamps are local receipt times, not acquisition
timestamps; do not use them as cross-host capture-to-display latency.

Source RTP fragments are limited to 1100 bytes to leave VPN/SRTP headroom.
Repeated identical offers reuse their answer, and source encoding starts only
when the peer/track is actually ready, not at ICE connectivity alone.
GStreamer SDP handling expands inherited transport attributes where required;
it does not invent credentials or disable certificate fingerprint checks.

Version-2 stream fields include:

| Field | Default | Meaning |
|---|---|---|
| `preserve_source_header` | `false` | Opt into original acquisition stamp/frame; compatibility mode uses receiver ROS receipt time. |
| `metadata_wait_ms` | `80` | Bounded wait for matching metadata, allowed 0-250 ms. No different frame's header is substituted. |
| `resize_mode` | `letterbox` | Explicit content transform; `stretch` is also supported. |
| `input_camera_info_topic` | empty | Source calibration topic. Volatile and latched reliable/transient-local publishers are supported. |
| `output_camera_info_topic` | empty | Republished calibration transformed to the actual output pixels. |
| `priority` | `1` | Relative weight in feasible bitrate allocation, allowed 1-100. |
| `transport` | `compatible` | `webrtc_only` allows the explicit routing policy above. |

ROI, binning, padding and stereo projection translation are accounted for in
K/P. A resized frame without a known pixel transform is not accompanied by
invented calibration. Packed ROS `yuv422` is UYVY; `yuv422_yuy2` is YUY2.

Decoded frames leave GStreamer's callback through a bounded worker handoff.
ROS publication cannot accumulate an unlimited decoded-frame backlog. A mode
change/reconnect invalidates pending generations. Metadata expiry, replacement
and publication failures are separate counters. Stopped cameras are reported
stale, not repeatedly treated as broken encoders.

Set `session.bitrate_budget_kbit` or `WEBRTC_SESSION_BUDGET_KBIT` for an aggregate
budget per operator. A budget below the sum of track minima is rejected before
launch, not silently exceeded or achieved by dropping a camera. Applied bitrate
is read back per encoder; failed changes attempt rollback and report actual
remaining state.

## Recovery and Readiness

`./horus status --json` distinguishes service processes, supervisors and media
stages. A listener or signaling connection is not video delivery. Configured
track IDs, fresh status and the correct child process must all agree. On ROS
output, useful delivery means ROS-published frames, not just decoder callbacks.
The connector cannot prove that a Quest has displayed those frames.

For the native operator RTP relay, `jitter_buffer_ms` can be set once in
the relay JSON or overridden per stream. It defaults to 30 ms. Compare the
worst decode silence and the source-to-decoder age before increasing it.
The relay's `ice_servers` configure its local ICE agent. Set
`source_ice_servers` explicitly when the source needs different servers,
for example an operator-side TURN allocation with a public STUN server
on the source. An empty list means no ICE servers on the source. Omitting
the field preserves the existing behavior of forwarding `ice_servers`
in the camera offer. To test Oracle/TURN rather than just Zenoh signaling
through the hub, verify that the selected media pair includes a candidate
from the expected TURN server, or use relay-only ICE and verify reception.

TCP startup checks use the configured Zenoh or metrics bind address. A
wildcard listener is probed on loopback for its address family. The Python
WebRTC signaling client requires a registration acknowledgement on each new
socket; if it does not arrive within five seconds, the client reconnects
without releasing the queued offer to an unregistered connection.

| Setting | Default | Purpose |
|---|---|---|
| `WEBRTC_DURATION` | `0` | Run until stopped. A positive value is an intentional finite session. |
| `HORUS_SERVICE_MAX_RESTARTS` | `5` | Retry budget within the restart window. |
| `HORUS_SERVICE_RESTART_WINDOW` | `300` | Seconds in that rolling window. |
| `HORUS_SERVICE_START_ATTEMPTS` | `100` | Readiness checks, with 100 ms pauses plus check overhead. |
| `WEBRTC_TRACK_REPAIR_RETRY_SEC` | `3` | Minimum interval between track repair attempts. |
| `WEBRTC_TRACK_REPAIR_LIMIT` | `1` | Decoder-local attempts before whole-peer fallback. |
| `WEBRTC_MAX_PIPELINE_REBUILDS` | `4` | Supervised native process recycling after repeated graph rebuilds. |

The local decoder is repaired first where possible. Persistent ICE/peer failure
requires renegotiating that operator's session. Repeated failures back off;
they do not promise uninterrupted video. This host's GStreamer/libnice can
retain resources during repeated teardown, including asynchronous router
discovery requests. Explicit callback/probe cleanup and periodic supervised
process replacement bound that native lifetime; this is containment, not a
claim to have fixed every upstream native-library issue. Standalone scripts
do not recycle themselves without a supervisor.

`./horus reprobe robot` or `./horus reprobe machine` refreshes codec selection.
Cache identity includes the interpreter, environment, plugin versions/files,
drivers, devices and workload profiles. Failed managed codec profiles are
invalidated and reprobed on the next supervised process launch. Manually
provided codec profiles are never overwritten by that mechanism.

## Capacity and Measurement

### DDS Compatibility

Qualify topics, services and actions separately with the robot's actual RMW.
The pinned ROS2DDS implementation uses Cyclone-specific service request headers;
successful topic forwarding from a Fast DDS source does not prove generic
cross-vendor service/action interoperability. A topic-based task adapter that
calls Nav2 locally does not exercise cross-vendor service/action transport.

In the compatibility Python media path each operator room has independent encoders. Bundling shares the
peer connection, not encoded video between operators. Inspect
`.run/media_capacity.json` (or the chosen instance directory) before scaling.
Optional `WEBRTC_MAX_OPERATORS` and `WEBRTC_MAX_ENCODED_PIXELS_PER_SEC` reject
workloads above an explicit operator-supplied budget. Zero means no configured
limit, not unlimited or measured hardware capacity. Shared encoding/SFU is a
 separate architecture from that adapter; the native path below supports it.

`./horus environment` prints a non-secret software/version report.
Metrics report rolling rates and interval CPU for owned process trees. CPU may
exceed 100% across cores. Memory is the sum of process RSS, including shared
pages, not unique physical memory. Optional NVIDIA metrics are **device totals**
including other applications, and unsupported fields remain unavailable.
Current media latency is sender RTP packetization to decoded output, not
capture-to-display. Cross-host latency requires a measured clock offset.
Supply `WEBRTC_CLOCK_OFFSET_JSON` from a clock probe or an explicitly validated
`WEBRTC_CLOCK_OFFSET_MS`; without it corrected latency is unavailable, not a
plausible-looking unsynchronized number. Local acceptance checks explicitly
declare zero offset because both processes share the same host clock.

## Qualification

Use the managed Python runtime created by bootstrap:

```bash
source /opt/ros/jazzy/setup.bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HORUS_FIELD_ROS_TEST=1 .venv-webrtc/bin/python -m pytest tests -q
.venv-webrtc/bin/python tests/check_release_dds_launch.py --output .run/checks/dds-launch
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bulk --custom-routes --output .run/checks/routes
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bridge-binary /path/to/zenoh-priority/zenoh-bridge-ros2dds --priority-links --bulk --custom-routes --constrained-mbps 5 --output .run/checks/weak-link
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bridge-binary /path/to/zenoh-priority/zenoh-bridge-ros2dds --priority-links --bulk --custom-routes --constrained-mbps 5 --bulk-budget-bps 500000 --output .run/checks/shared-governor
.venv-webrtc/bin/python tests/check_priority_reconnect.py --bridge-binary /path/to/zenoh-priority/zenoh-bridge-ros2dds --output .run/checks/priority-reconnect
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bulk --hub --output .run/checks/hub
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bridge-binary /path/to/zenoh-priority/zenoh-bridge-ros2dds --hub --session-lanes --bulk --custom-routes --output .run/checks/hub-three-sessions
.venv-webrtc/bin/python tests/check_zenoh_routes.py --bridge-binary /path/to/zenoh-priority/zenoh-bridge-ros2dds --hub --session-lanes --bulk --custom-routes --restart-bulk --output .run/checks/hub-bulk-restart
.venv-webrtc/bin/python tests/check_ros_webrtc.py --letterbox --latched-info --output .run/checks/camera
.venv-webrtc/bin/python tests/check_connector_launch.py --output .run/checks/launcher
.venv-webrtc/bin/python tests/check_media_recovery.py --track-only --output .run/checks/track
.venv-webrtc/bin/python tests/check_supervised_media_recovery.py --output .run/checks/recovery
.venv-webrtc/bin/python tests/check_multi_operator_media.py --tracks 8 --soak-seconds 3600 --output .run/checks/eight-cameras
```

Use a new output directory per trial. These are local, disposable endpoints and
synthetic acceptance inputs, not physical-robot, WAN or Quest benchmarks. They
do not alter host firewall/interface settings. Native ICE can perform its own
router discovery. The unrestricted in-process recovery check also retains a
resource-growth assertion to expose native lifetime problems; use the
supervised check to qualify the deployment path rather than weakening it.

This change was checked locally on Ubuntu 24.04 x86_64, ROS 2 Jazzy, Python
3.12 and GStreamer 1.24.2. CI defines Humble/Jazzy and ARM64 checks; an unrun CI
definition is not an executed compatibility result. H.264 requires GStreamer
1.20+, bundled camera identity requires 1.22+. Legacy JPEG remains an explicit
compatibility option, not a claim of identical multicamera recovery/features.
Run long mixed-load, real WAN/TURN and physical hardware tests before claiming
a supported production capacity or a release-wide zero-bug guarantee.

### Remote Trial Preflight

1. Run `./horus doctor robot` and `./horus doctor machine` on their respective
   hosts (and `./horus doctor cloud` on the hub when used). Save
   `./horus status --json` once the services are up. Confirm that the source
   and operator binaries include WebRTC when using the native media path.
   For a native-only run, set `HORUS_STREAMS_CONFIG` to a file containing
   `{"version":2,"streams":[]}` instead of leaving it unset. This prevents
   the compatibility launcher from starting a fallback camera alongside
   the native path. Check Zenoh camera routing separately for duplicates.
2. Record the selected transport, not just the configuration. For Tailscale,
   check whether `tailscale ping` reports direct or DERP. For WebRTC, use
   selected ICE pair evidence. Cloud signaling alone does not prove relay use.
3. With motion disabled, exercise all expected cameras, TF and a representative
   map load for at least 90 seconds before recording a trial. Measure per-camera
   receive/decode FPS and worst delivery gap across the full window, alongside TF
   freshness and gaps. Check the same streams in HORUS on the headset. If FPS
   is low, confirm frames reach the encoder before changing the WAN profile.
4. Keep route comparisons matched and retain stalls, failed trials and route
   changes in the results. Restarting a decoder or receiving a late burst is
   not proof of uninterrupted delivery.

## Native Packet Relay And Fleet Control

The native relay removes the operator-PC video round trip through DDS. The
path is source GPU encoding -> WebRTC -> native RTP forwarding -> Quest decode.
It preserves H.264 picture bytes, source RTP timestamp intervals and sequence
gaps, translating hop-local identifiers. It is not a decoder/transcoder. The
legacy `HORUS_MEDIA_RELAY_ENGINE=ros` adapter remains explicit for tools needing
local encoded ROS topics. Logical native output aliases are not ROS publishers.

Build the pinned GCC/TWCC pacing plugin without replacing system GStreamer:

```bash
python3 scripts/build_media_runtime.py --work-dir /path/with/space-for-build/media-gcc
```

The default output is `.runtime/media-gcc/plugins`; `HORUS_MEDIA_GST_PLUGIN_DIR`
can select an external installation. The source commit, Cargo lock hash and
binary digest are recorded beside the plugin, with its MPL-2.0 licence.
The existing project-local NICE plugin and user `GST_PLUGIN_PATH` are retained.
GCC is used only when TWCC is negotiated and the plugin is present. Native
transport telemetry identifies the actual engine; fixed-bitrate fallback is
not reported as congestion-controlled.

On the operator PC, set `HORUS_MEDIA_RELAY_CONFIG` to the camera JSON,
`HORUS_MEDIA_RELAY_ENGINE=native`, `HORUS_MEDIA_RELAY_BINARY` to the WebRTC-enabled
`horus_unity_bridge_node`, and optionally `HORUS_MEDIA_RELAY_BRIDGE_CONFIG` to
its YAML settings. The connector owns this bridge; SDK registrations reuse it
instead of starting another. Each JSON stream specifies `id`, `input_topic`,
`output_topic`, optional `image_type`, `bitrate_kbps`, `framerate` and
`jitter_buffer_ms`. There is one upstream subscription per camera in that
operator bridge, shared by its local consumers and stopped after idle grace.
The JSON root or individual streams may also specify `ice_servers` (a list of
STUN/TURN URL strings) and `ice_transport_policy` (`all`, the unchanged default,
or `relay`). For GStreamer, TURN URLs use
`turn://username:password@host:port` or `turns://username:password@host:port`.
Keep actual credentials in private runtime profiles, not committed examples.
`relay` requires at least one TURN URL and forces the upstream video through
that relay; pointing ROS signaling at a hub does not itself force this route.
Stream-level settings override root settings. The native status includes ICE
policy and local candidate counts, but these are not selected-pair proof;
verify the actual path with selected-pair statistics or bounded packet captures.
The relay detects forward sequence gaps after upstream jitter reordering and
schedules the existing keyframe request, limited to one request per 500 ms per
upstream camera. A downstream retransmission cache cannot repair a packet that
the upstream relay never received. This requests a fresh source picture without
transcoding or hiding the missing sequence numbers. Late repairs, duplicates,
sequence wrap and SSRC changes do not create false gap requests.
`recover_on_packet_gap` defaults to `true`; a root or stream-level boolean can
disable it for comparison. `upstream_sequence_gap_events`,
`upstream_missing_sequence_numbers` and `keyframe_requests` expose the decision.
They count observed post-jitter discontinuities and attempted requests, not raw
wire loss or confirmed encoder keyframes. Loss recovery can request additional
IDRs, so assess actual bitrate, picture quality and worst gaps together.
At the fleet source, `HORUS_SOURCE_MEDIA_SHARE_ENCODERS=true` shares encoding
across operators requesting the same camera/profile. Different requested FPS
or congestion budgets use separate bounded groups, not a global weakest-viewer
setting. Native parameters `webrtc.max_shared_encoder_groups` (24) and
`webrtc.max_shared_encoder_groups_per_camera` (3) bound codec use. A profile
change that exceeds capacity is retained at the current profile with a warning;
it does not create unlimited encoders. `/horus/remote/media/encoders/status`
reports the active group/consumer count.

For repeated views of identical cameras, `HORUS_SOURCE_MEDIA_STABLE_GROUPS=true`
optionally keeps a group per requested camera/codec/bitrate/FPS/priority
instead of splitting it whenever per-viewer bandwidth estimates diverge.
Its existing encoder adjusts to the lowest allowance among viewers in
that specific group, with faster reductions than increases. Operators
with distinct immersive and overview profiles do not share that limit.
This trades per-viewer quality independence for fewer encoder restarts.
It does not remove per-viewer network copies: three operators requesting
all three cameras still create nine outbound tracks. Use the
[cloud packet fan-out profile](configuration.md#pinned-experiment-runtime)
to move those copies off the fleet uplink. Three-camera visibility at
every site is the primary load arm; one assigned camera per operator is
only a diagnostic.

For independent fleet-side control, explicitly select
`HORUS_SOURCE_CONTROL_MODE=authority` at the robot site and
`HORUS_OPERATOR_CONTROL_MODE=proxy` at every operator relay. Leave these `local`
for the established single-PC path. Do not run another lease authority at the
same fleet edge. Small ROS signaling topics negotiate a persistent native
control peer; continuous motion then uses an unordered, non-retransmitted data
channel, and catalog/lease/task/stop transactions use a reliable channel.
Only the fleet authority publishes the final robot command. It checks positive
lease ownership, sequence and monotonic-clock deadline; expired Twist motion
receives a zero command. These freshness/watchdog checks are operational
correctness, not a replacement for robot-local safety controllers.

Control and video have separate ICE peers. For a site without a VPN,
set `HORUS_CONTROL_ICE_CONFIG` to a private JSON file on both native
control endpoints. An operator that must use TURN can select
`{"ice_transport_policy":"relay","ice_servers":["turn://user:credential@hub.example:3478"]}`.
For a relay-only qualification, use separate TURN credentials at the
authority too. With relay policy the control link refuses to exchange
commands and clock probes unless the selected pair contains a TURN
candidate; an ICE state of connected by itself does not qualify the route.
Provision actual per-site credentials outside the repository; only the
configured local process reads them. Leave the setting unset to preserve
host-only control ICE for existing direct/private deployments. Use an
authenticated signaling/telemetry path for real control. The current native
control backend supports UDP TURN; do not assume a UDP-blocked site works
on TCP/TLS just because video or the signaling port connects. Verify
`clock_valid` and the selected control candidate types per operator.

Status topics are `/horus/remote/media/native/status` and
`/horus/remote/control/{proxy,authority}/status`. Inspect actual packet/frame
counts, packet age, clock uncertainty, rejection counts and paced-buffer state;
process-alive or configured FPS alone is not proof of delivered video/control.
No resolution, map geometry, colour or headset text quality is reduced by this
transport mode. Bandwidth adaptation changes compression bitrate explicitly;
acquisition FPS still limits what can be delivered.

Private and shared workspaces both publish their command catalogue. A client
replaces only its own contribution; disconnecting it cannot clear another
operator's catalogue. A lost control link clears the proxy's lease view and
requires a fresh release/acquire before motion resumes. Reliable acknowledgements
mean a transaction was handled; the lease-state response determines whether
ownership was granted. Current Quest clients negotiate optional timed input:
an independent monotonic clock probe maps the Quest publication time to the
operator bridge, which rejects expired local samples and carries only their
remaining lifetime across the WAN. Unknown/stale clock estimates suppress
continuous motion; the source watchdog still stops motion. Legacy clients
retain receipt-based expiry. Publication time is not physical controller
acquisition time, and neither is a measured full input latency.

Video repair is age-limited to 250 ms, byte-budgeted, and passes through the
negotiated pacer. The repair allowance also follows the current bitrate, so a
low-capacity stream does not receive a fixed multi-megabit repair burst.

### Local Camera Ingress

Watch `/horus/remote/media/encoders/status` and compare its input/push counters
with source publication. WebRTC bandwidth adaptation cannot recover raw
images that did not reach the robot-side DDS subscriber.

`HORUS_SOURCE_MEDIA_CAMERA_DOMAIN_ID` optionally isolates the native sender's
camera subscriptions, auxiliary images, and camera keyframe requests from
its normal ROS graph. The main `ROS_DOMAIN_ID` still carries signaling,
state and control; Zenoh does not enter the camera domain. Empty keeps the
existing behavior. A sender built without this feature will not pass
launcher readiness when it is requested.

Publish the raw or encoded camera topics on the selected domain, with fully
qualified topic names. To use local Cyclone DDS TCP, keep the primary domain
profile and add a dedicated camera entry to the publisher's and native
sender's `CYCLONEDDS_URI`. For example, this is the sender-side entry:

```xml
<Domain id="174">
  <General>
    <Interfaces><NetworkInterface name="lo"/></Interfaces>
    <Transport>tcp</Transport><AllowMulticast>false</AllowMulticast>
  </General>
  <Discovery><Peers><Peer Address="127.0.0.1:58200"/></Peers></Discovery>
  <TCP><Port>58201</Port></TCP>
</Domain>
```

Swap 58200 and 58201 in the camera publisher's profile. Place both domain
entries under `<CycloneDDS>`; scope any existing catch-all profile to the
normal domain. For these processes use
`ROS_AUTOMATIC_DISCOVERY_RANGE=SYSTEM_DEFAULT` with the explicit interfaces
and unset the deprecated `ROS_LOCALHOST_ONLY`. Do not move an existing
publisher until its intended subscribers can use the same domain and
transport. This local TCP hop is not the WAN video transport.

### Shared Link Budget

At each fleet egress, optionally set:

```bash
HORUS_LINK_BUDGET_KBPS=20000
HORUS_LINK_RESERVED_KBPS=512
HORUS_LINK_BULK_FLOOR_KBPS=256
HORUS_ZENOH_BULK_BUDGET_FILE=/absolute/writable/runtime/bulk-budget.txt
```

This is a configured **application budget**, not automatic link discovery or a
hard wire-rate guarantee. Select it below measured available throughput, with
headroom for RTP/SRTP/TCP/Zenoh/VPN overhead, repairs, other applications and
network variability. A conservative 20 Mbit/s example is not a universal default.
Zero disables aggregate governance and preserves independent per-stream GCC.

The native source must use shared encoding; the patched Zenoh bridge at that
source must receive the same budget-file path. The governor reserves state/
control headroom, allocates remaining video capacity with weighted max-min
sharing capped by each consumer's request/GCC estimate, then returns unused
allowance to bulk. MR defaults are overview weight1 and teleop weight4, with
SDK overrides1-16 in Python/C++/Rust. Impossible session admission is rejected
explicitly; the native default session ceiling is64. No geometry is removed.
The file is atomically refreshed5Hz. Invalid/missing/stale files use a bounded
fallback no greater than the last safe rate, never unlimited bulk.
`HORUS_LINK_BULK_LIMIT_KBPS` optionally caps bulk even as GCC reduces video
bitrate. Its default 0 keeps the existing behavior. A positive cap must be at
least the bulk floor and requires an enabled shared link budget. It preserves
headroom for video recovery on weak links but can delay bulk delivery. It is
not an automatic link-capacity measurement or a change to map resolution.
`/horus/remote/media/bandwidth_budget` reports allocations and actual demand
priorities. Independent robots sharing an ISP need one common egress governor
or explicitly partitioned budgets; separate source processes do not magically
coordinate their physical link.

### Frame Trace And Recent Statistics

Native camera sources embed `horus_camera_frame_v1` H.264 SEI metadata: stream
epoch, adapter frame ID, original ROS stamp/frame and source monotonic adapter
time. Exact GStreamer segment-running-time matching associates metadata with
encoded pictures, including encoder PTS offsets. A missing exact match is
unavailable, never replaced with another camera frame's FIFO identity. The
packet relay preserves those bytes. Existing external preencoded video may not
provide this metadata and must not be assigned invented capture information.

Quest exposes `LatestSourceFrameTrace`, its local encoded-arrival monotonic
timestamp, and `RecentVideo` windowed decoded FPS/bitrate/decode time/jitter-buffer
delay/drops. These distinguish current spikes from lifetime averages. ROS
stamps explicitly remain `ros_unsynchronized`; cross-clock subtraction is
invalid without measured offset/uncertainty. Unity's receiver texture callback
does not expose an exact presented frame ID, so encoded arrival and decoder
progress are **not** synchronized glass-to-glass or exact presentation latency.

## Independent Traffic Sessions

`HORUS_ZENOH_SESSION_ISOLATION=1` opts into three independently supervised native
TCP bridges: real-time priorities0-1, sensing priorities2-5, and bulk priorities6-7.
Unlike three TCP links in one Zenoh session, a bulk session timeout/reconnect
does not tear down the TF/control session, and bulk DDS delivery has its own
participant and runtime. Services/actions use the real-time session. Unclassified
topics use the sensing session; geometry topics need an appropriate priority6/7
rule. No source geometry or colour is removed by session isolation.

Build the verified binary with `scripts/build_priority_bridge.py` on both
endpoints, select it with `ZENOH_BRIDGE_BINARY`, and stop the current connector
before changing modes. Use `ZENOH_TRANSPORT=tcp`. Both endpoints must enable
the mode and use the **same topic-priority classification policy**, including
custom capability-derived routes. Inconsistent policies can leave a route
unmatched; automatic cross-host policy synchronization is not implemented.
The legacy single-session mode remains the default.

For map-heavy links, use independent sessions when TF delivery pauses during
full map or point-cloud publications. A higher topic priority cannot overtake
bytes already queued ahead of it on a shared TCP connection. Keep the same
map data and put TF on a real-time route (for example, priority `1:express`)
and point clouds or geometry on a bulk route (priority `6` or `7`). Verify
that all three connections are present on both endpoints, and compare TF
gaps under concurrent load, not just average TF rate.

Build an artifact for each endpoint's architecture. A Docker build on ARM64
needs Python with the `tarfile` extraction filter (for example, Python 3.12),
Rust, Git, CMake and the Cyclors native build tools. Keep the compiled bridge
and its checksum-bearing `.source.json` together. An upstream ARM64 release
binary can run the single-session mode but cannot replace the verified HORUS
build for independent sessions.

The base port and the next two ports are used (`7447`, `7448`, `7449` by default).
HORUS does not alter host firewall, router, MTU or Wi-Fi settings. Each session
has a separate process, DDS participant, Zenoh identifier and reconnection
lifetime. Status lists sensing/bulk sessions separately rather than treating
a working TF connection as proof that all three are connected. Stopping the
tracked `zenoh` service also stops its two companion sessions in that instance.

The source build includes two pinned fixes: scoped Cyclors serialized-slice
copies respect the requested offset/length, and publisher matching listeners
are retired with their routes. The latter prevents ghost readers after robot
publisher/operator subscriber recreation. The native lifecycle regression
requires exactly30 deliveries for30 publications on every cycle; the old
runtime's30/60/90/120 behaviour is a failure, not higher throughput.

The serializer also permits the DDS contract's four-byte alignment padding;
padding is zero-filled without reading beyond the payload. Its private Cargo
dependency tracks the C source directory so an incremental build cannot retain
an older serializer after a patch update. The archive, patch and private source
tree have separate validation receipts; system DDS and Cargo registry sources
are not modified.

Qualification commands, using an installed Jazzy environment:

```bash
python3 tests/check_route_lifecycle.py --bridge /absolute/path/zenoh-bridge-ros2dds --output /new/output/lifecycle
python3 tests/check_zenoh_routes.py --session-lanes --bulk --constrained-mbps 5 --bulk-budget-bps 500000 \
  --bridge-binary /absolute/path/zenoh-bridge-ros2dds --output /new/output/session-test
```

The second test uses an application-only shared-capacity TCP proxy, not host
traffic shaping. It checks TF, return routes, static transforms, reliable bulk
hashes and final indexed mesh state. It also fails on a gap of 250 ms or more
in any expected dynamic TF stream after warmup, including silence at the end
of the receiver window. These are controlled local tests, not WAN
latency guarantees. The real-recording JPEG/WebRTC comparison is documented in
the SDK's `python/examples/REAL_CAMERA_STREAM_EXPERIMENT.md`; retain bad runs,
whole-window silences and source-matched quality, not just average FPS.

Current qualification caveat: the 250,000 byte/s stress profile has passed
some runs and failed others, leaving old mesh revisions or missing integrity
payloads even with low TF latency. The same 5 Mbit/s fixture passed at
500,000 byte/s with all 32 final mesh hashes intact. This does not establish a
cause or a universal allocation for deployments. Keep session isolation
opt-in and retain final-state/retry checks for incremental maps.
