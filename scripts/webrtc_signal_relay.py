#!/usr/bin/env python3
"""WebRTC signaling relay for hub deployments.

The relay forwards JSON signaling messages between one robot peer and one
machine peer per room. It does not encode, decode, inspect, or terminate media.
Actual camera/control media remains end-to-end between robot and machine, or
through the configured ICE/TURN path.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import ssl
import time
from collections import defaultdict
from pathlib import Path

import websockets

from webrtc_session import CredentialStore, SIGNALING_PROTOCOL_VERSION, TurnCredentialIssuer
from horus_voice_session import VOICE_PEER_ROLES, VOICE_ROLES, VoiceCallRegistry


class Relay:
    def __init__(
        self,
        state_file: str = "",
        auth_token: str = "",
        max_pending_per_peer: int = 128,
        max_pending_bytes: int = 4 * 1024 * 1024,
        pending_ttl_sec: float = 30.0,
        credentials: CredentialStore | None = None,
        turn_issuer: TurnCredentialIssuer | None = None,
        voice_registry: VoiceCallRegistry | None = None,
        allow_unauthenticated: bool = False,
        allow_unauthenticated_voice: bool = False,
    ):
        self.peers = {}
        self.peer_meta = {}
        self.pending = defaultdict(list)
        self.lock = asyncio.Lock()
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.auth_token = auth_token
        self.max_pending_per_peer = max(1, int(max_pending_per_peer))
        self.max_pending_bytes = max(1024, int(max_pending_bytes))
        self.pending_ttl_sec = max(0.0, float(pending_ttl_sec))
        self.credentials = credentials or CredentialStore()
        self.turn_issuer = turn_issuer or TurnCredentialIssuer()
        self.voice_registry = voice_registry or VoiceCallRegistry()
        self.allow_unauthenticated = bool(allow_unauthenticated)
        self.allow_unauthenticated_voice = bool(allow_unauthenticated_voice)

    @staticmethod
    def target_role(role: str) -> str:
        if role in VOICE_PEER_ROLES:
            return VOICE_PEER_ROLES[role]
        return "machine" if role == "robot" else "robot"

    def write_state(self):
        if self.state_file is None:
            return
        peers = []
        for room, role in sorted(self.peers.keys()):
            websocket = self.peers[(room, role)]
            metadata = self.peer_meta.get(websocket, {})
            peers.append(
                {
                    "room": room,
                    "role": role,
                    "client_id": metadata.get("client_id", "legacy"),
                    "session_id": metadata.get("session_id", ""),
                }
            )
        payload = {"updated_at": time.time(), "peers": peers}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            print(f"warning: could not write signaling state: {exc}", flush=True)

    def _queued_signal_is_current(self, room: str, target_role: str, text: str) -> bool:
        source = self.peers.get((room, self.target_role(target_role)))
        if source is None:
            return False
        source_session = self.peer_meta.get(source, {}).get("session_id", "")
        try:
            message_session = str(json.loads(text).get("session_id") or "")
        except Exception:
            return False
        return not source_session or not message_session or source_session == message_session

    async def register(
        self,
        websocket,
        role: str,
        room: str,
        client_id: str = "legacy",
        session_id: str = "",
    ):
        key = (room, role)
        notify_peer = None
        reset_peer = None
        old_peer = None
        async with self.lock:
            old_peer = self.peers.get(key)
            if old_peer is websocket:
                old_peer = None
            self.peers[key] = websocket
            self.peer_meta[websocket] = {
                "client_id": client_id,
                "session_id": session_id,
                "role": role,
                "room": room,
            }
            self.write_state()
            queued = [
                text
                for _created_at, text in self.pending.pop(key, [])
                if self._queued_signal_is_current(room, role, text)
            ]
            notify_peer = self.peers.get((room, self.target_role(role)))
            if (
                role == "machine"
                and notify_peer is not None
                and os.environ.get("HORUS_WEBRTC_RELAY_RESET_ROBOT_ON_MACHINE_JOIN", "0") != "0"
            ):
                reset_peer = notify_peer
        if old_peer is not None:
            try:
                await old_peer.close(reason="endpoint session replaced")
            except Exception as exc:
                print(f"warning: could not close replaced role={role} room={room}: {exc}", flush=True)
        credential = self.turn_issuer.issue(client_id)
        accepted = {
            "type": "registered",
            "protocol_version": SIGNALING_PROTOCOL_VERSION,
            "role": role,
            "room": room,
            "client_id": client_id,
            "session_id": session_id,
            "peer_ready": notify_peer is not None,
        }
        if credential is not None:
            accepted["turn"] = credential.signaling_payload()
        await websocket.send(json.dumps(accepted, separators=(",", ":")))
        for text in queued:
            await websocket.send(text)
        if notify_peer is not None:
            try:
                await notify_peer.send(json.dumps({"type": "peer-ready", "role": role, "room": room}))
            except Exception as exc:
                print(f"warning: could not notify peer-ready role={role} room={room}: {exc}", flush=True)
        if reset_peer is not None:
            print(f"resetting role=robot room={room} reason=machine-joined", flush=True)
            await reset_peer.close(reason="machine peer joined")
        print(f"registered role={role} room={room}", flush=True)

    async def unregister(self, websocket):
        notifications = []
        async with self.lock:
            for key, peer in list(self.peers.items()):
                if peer is websocket:
                    del self.peers[key]
                    self.peer_meta.pop(websocket, None)
                    self.write_state()
                    notify_peer = self.peers.get((key[0], self.target_role(key[1])))
                    if notify_peer is not None:
                        notifications.append((notify_peer, key[1], key[0]))
                    print(f"unregistered role={key[1]} room={key[0]}", flush=True)
            self.peer_meta.pop(websocket, None)
        for peer, role, room in notifications:
            try:
                await peer.send(json.dumps({"type": "peer-left", "role": role, "room": room}))
            except Exception as exc:
                print(f"warning: could not notify peer-left role={role} room={room}: {exc}", flush=True)

    async def forward(self, room: str, role: str, text: str):
        target = (room, self.target_role(role))
        try:
            kind = json.loads(text).get("type", "unknown")
        except Exception:
            kind = "invalid-json"
        async with self.lock:
            peer = self.peers.get(target)
            if peer is None:
                self._queue_pending_locked(target, text)
                if os.environ.get("HORUS_WEBRTC_SIGNAL_DEBUG") == "1":
                    print(f"queued type={kind} from={role} room={room} target={target[1]}", flush=True)
                return
        if os.environ.get("HORUS_WEBRTC_SIGNAL_DEBUG") == "1":
            print(f"forward type={kind} from={role} room={room} target={target[1]}", flush=True)
        await peer.send(text)

    def _is_authorized(self, message: dict) -> bool:
        role = str(message.get("role") or "")
        if self.credentials.configured():
            return self.credentials.authorized(
                str(message.get("client_id") or ""),
                str(message.get("token") or ""),
                role,
                str(message.get("room") or "default"),
                required_capability="communicative" if role in VOICE_ROLES else "",
            )
        if role in VOICE_ROLES:
            # Trusted development is deliberately separate from camera/control
            # authorization. Adding a credential file automatically restores
            # per-client voice authentication without changing device builds.
            return self.allow_unauthenticated_voice
        if self.auth_token:
            return secrets_compare(str(message.get("token") or ""), self.auth_token)
        return self.allow_unauthenticated

    @staticmethod
    def _pending_size(entries) -> int:
        return sum(len(text.encode("utf-8")) for _created_at, text in entries)

    def _queue_pending_locked(self, target, text: str):
        now = time.monotonic()
        queue = self.pending[target]
        if self.pending_ttl_sec > 0.0:
            cutoff = now - self.pending_ttl_sec
            queue[:] = [(created_at, item) for created_at, item in queue if created_at >= cutoff]
        try:
            message = json.loads(text)
        except Exception:
            message = {}
        if message.get("type") in {"offer", "answer"}:
            session_id = str(message.get("session_id") or "")
            retained = []
            for created_at, item in queue:
                try:
                    queued = json.loads(item)
                except Exception:
                    continue
                same_session = not session_id or str(queued.get("session_id") or "") == session_id
                if same_session and queued.get("type") in {"offer", "answer", "ice"}:
                    continue
                retained.append((created_at, item))
            queue[:] = retained
        queue.append((now, text))
        while len(queue) > self.max_pending_per_peer or self._pending_size(queue) > self.max_pending_bytes:
            queue.pop(0)

    async def handle(self, websocket):
        role = None
        room = None
        try:
            async for text in websocket:
                message = json.loads(text)
                if message.get("type") == "register":
                    if not self._is_authorized(message):
                        await websocket.close(reason="invalid signaling token")
                        return
                    role = message.get("role")
                    room = message.get("room", "default")
                    client_id = str(message.get("client_id") or f"legacy:{role}:{room}")
                    session_id = str(message.get("session_id") or "")
                    if role not in {"robot", "machine", *VOICE_ROLES}:
                        await websocket.close(
                            reason="role must be robot, machine, operator, or field_teammate"
                        )
                        return
                    await self.register(websocket, role, room, client_id, session_id)
                    continue
                if role is None or room is None:
                    await websocket.close(reason="first message must be register")
                    return
                current_session = str(self.peer_meta.get(websocket, {}).get("session_id") or "")
                message_session = str(message.get("session_id") or "")
                if current_session and message_session and current_session != message_session:
                    await websocket.close(reason="signaling session mismatch")
                    return
                if role in VOICE_ROLES:
                    decision = self.voice_registry.validate_and_apply(room, role, message)
                    if not decision.accepted:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "signal_rejected",
                                    "reason": decision.reason,
                                },
                                separators=(",", ":"),
                            )
                        )
                        continue
                await self.forward(room, role, text)
        except websockets.ConnectionClosed:
            pass
        finally:
            await self.unregister(websocket)


async def main_async(args):
    if (
        not args.allow_unauthenticated
        and not getattr(args, "allow_unauthenticated_voice", False)
        and not args.auth_token
        and not getattr(args, "credentials_file", "")
        and not is_loopback_host(args.host)
    ):
        raise SystemExit(
            "Refusing to start unauthenticated WebRTC relay on a non-loopback host. "
            "Set HORUS_WEBRTC_RELAY_TOKEN, configure credentials, or enable an explicit trusted test mode."
        )
    credentials = CredentialStore.from_file(getattr(args, "credentials_file", ""))
    turn_issuer = TurnCredentialIssuer(
        host=getattr(args, "turn_host", ""),
        secret=getattr(args, "turn_secret", ""),
        port=getattr(args, "turn_port", 3478),
        ttl_sec=getattr(args, "turn_ttl_sec", 3600),
        transports=tuple(
            value.strip()
            for value in getattr(args, "turn_transports", "udp,tcp").split(",")
            if value.strip()
        ),
        secure=getattr(args, "turn_secure", False),
    )
    relay = Relay(
        args.state_file,
        auth_token=args.auth_token,
        max_pending_per_peer=args.max_pending_per_peer,
        max_pending_bytes=args.max_pending_bytes,
        pending_ttl_sec=args.pending_ttl_sec,
        credentials=credentials,
        turn_issuer=turn_issuer,
        allow_unauthenticated=args.allow_unauthenticated or is_loopback_host(args.host),
        allow_unauthenticated_voice=getattr(args, "allow_unauthenticated_voice", False),
    )
    relay.write_state()
    tls_context = None
    tls_cert = getattr(args, "tls_cert", "")
    tls_key = getattr(args, "tls_key", "")
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            raise SystemExit("Both --tls-cert and --tls-key are required for WSS signaling")
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(tls_cert, tls_key)
    async with websockets.serve(
        relay.handle,
        args.host,
        args.port,
        max_size=args.max_message_bytes,
        ssl=tls_context,
    ):
        scheme = "wss" if tls_context is not None else "ws"
        print(f"WebRTC signaling relay listening on {scheme}://{args.host}:{args.port}", flush=True)
        await asyncio.Future()


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in {"127.0.0.1", "::1", "localhost"}


def secrets_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-message-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--state-file", default="", help="write active room membership for the live monitor")
    parser.add_argument("--auth-token", default=os.environ.get("HORUS_WEBRTC_RELAY_TOKEN", ""))
    parser.add_argument("--credentials-file", default=os.environ.get("HORUS_WEBRTC_CREDENTIALS_FILE", ""))
    parser.add_argument("--allow-unauthenticated", action="store_true")
    parser.add_argument(
        "--allow-unauthenticated-voice",
        action="store_true",
        default=os.environ.get("HORUS_VOICE_TRUSTED_DEVELOPMENT", "1") == "1",
        help="Allow tokenless operator/field_teammate registration when no credential file is configured.",
    )
    parser.add_argument("--tls-cert", default=os.environ.get("HORUS_WEBRTC_SIGNAL_TLS_CERT", ""))
    parser.add_argument("--tls-key", default=os.environ.get("HORUS_WEBRTC_SIGNAL_TLS_KEY", ""))
    parser.add_argument("--turn-host", default=os.environ.get("HORUS_WEBRTC_TURN_PUBLIC_HOST", ""))
    parser.add_argument("--turn-secret", default=os.environ.get("HORUS_WEBRTC_TURN_SECRET", ""))
    parser.add_argument("--turn-port", type=int, default=int(os.environ.get("TURN_PORT", "3478")))
    parser.add_argument("--turn-ttl-sec", type=int, default=int(os.environ.get("HORUS_WEBRTC_TURN_TTL_SEC", "3600")))
    parser.add_argument("--turn-transports", default=os.environ.get("HORUS_WEBRTC_TURN_TRANSPORTS", "udp,tcp"))
    parser.add_argument("--turn-secure", action="store_true", default=os.environ.get("HORUS_WEBRTC_TURN_SECURE", "0") == "1")
    parser.add_argument("--max-pending-per-peer", type=int, default=int(os.environ.get("HORUS_WEBRTC_RELAY_MAX_PENDING_PER_PEER", "128")))
    parser.add_argument("--max-pending-bytes", type=int, default=int(os.environ.get("HORUS_WEBRTC_RELAY_MAX_PENDING_BYTES", str(4 * 1024 * 1024))))
    parser.add_argument("--pending-ttl-sec", type=float, default=float(os.environ.get("HORUS_WEBRTC_RELAY_PENDING_TTL_SEC", "30.0")))
    return parser.parse_args()


def main():
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
