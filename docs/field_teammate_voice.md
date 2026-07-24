# Field-Teammate Live Voice

HORUS live voice follows the same connection model as the rest of the application. The operator connects HORUS MR to the HORUS ROS 2 bridge once; no voice URL, room, identity, or token is entered on either headset.

## Development flow

1. HORUS MR connects to the normal HORUS bridge host.
2. The SDK registers a teammate such as `field_teammate_1` with the `communicative` capability.
3. Quest derives `ws://<bridge-host>:8765` and uses the teammate name as its voice room.
4. `./horus launch teammate` starts the local signaling service when no remote signaling override is configured.
5. The field relay sends the same endpoint and room to HORUS Field over the existing HoloLens UMQ control connection.
6. The operator presses **Start live voice**; the field teammate presses **Accept**.

The normal development configuration is therefore:

```dotenv
HORUS_VOICE_ENABLED=1
HORUS_VOICE_TRUSTED_DEVELOPMENT=1
FIELD_TEAMMATE_VOICE_SIGNALING_HOST=
FIELD_TEAMMATE_VOICE_TOKEN=
```

The committed `horus_voice_config.json` files are fallback templates for standalone component development. The auto-bootstrapped Quest and HoloLens applications defer voice startup until the HORUS runtime supplies their configuration.

## Transport boundary

The connector relays call state, SDP, and ICE. It does not decode or record audio. The clients negotiate 48 kHz mono Opus over WebRTC DTLS-SRTP, normally peer-to-peer on a LAN. ROS 2 and HorusLink may carry study metrics, but never microphone audio.

Voice rooms contain exactly two roles:

- `operator`: HORUS MR on Quest;
- `field_teammate`: HORUS Field on HoloLens.

The relay enforces the invitation/acceptance sequence, one active call per room, session and call identifiers, message size limits, and role pairing. Quest separately requires a complete teammate registration with the `communicative` capability before it enables the call button.

## Remote operation and production security

Remote operation keeps the headset workflow unchanged. Run signaling beside the bridge or set `FIELD_TEAMMATE_VOICE_SIGNALING_HOST` to the bridge-side public host, then configure WSS and coturn centrally for NAT traversal. The headsets still learn the route through their existing HORUS connections.

When production authentication is needed:

1. set `HORUS_VOICE_TRUSTED_DEVELOPMENT=0` on the signaling host;
2. configure `HORUS_WEBRTC_CREDENTIALS_FILE` with per-client room and `communicative` grants;
3. use WSS and time-limited coturn credentials;
4. issue short-lived session credentials from the bridge/runtime instead of manually copying long-lived token files to headsets.

If a credential file is configured, it takes precedence even when trusted-development mode is enabled. This makes the current tokenless workflow explicit while preserving a fail-closed production path.

## Call sequence

```text
operator                         field_teammate
   |--------- call_invite ------------>|
   |<-------- call_ringing -------------|
   |<-------- call_accept --------------|
   |------ session_description -------->|
   |<----- session_description ---------|
   |<------ ice_candidate ------------->|
   |<------ call_connected ------------>|
   |<------- mute_changed ------------->|
   |<---------- call_end -------------->|
```

Physical Quest-to-HoloLens acceptance testing must still measure microphone permission, device routing, echo, latency, reconnect behavior, network handover, and thermal impact.
