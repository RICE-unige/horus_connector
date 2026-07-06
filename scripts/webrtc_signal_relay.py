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
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import websockets


class Relay:
    def __init__(
        self,
        state_file: str = "",
        auth_token: str = "",
        max_pending_per_peer: int = 128,
        max_pending_bytes: int = 4 * 1024 * 1024,
        pending_ttl_sec: float = 30.0,
    ):
        self.peers = {}
        self.pending = defaultdict(list)
        self.lock = asyncio.Lock()
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.auth_token = auth_token
        self.max_pending_per_peer = max(1, int(max_pending_per_peer))
        self.max_pending_bytes = max(1024, int(max_pending_bytes))
        self.pending_ttl_sec = max(0.0, float(pending_ttl_sec))

    @staticmethod
    def target_role(role: str) -> str:
        return "machine" if role == "robot" else "robot"

    def write_state(self):
        if self.state_file is None:
            return
        peers = [
            {"room": room, "role": role}
            for room, role in sorted(self.peers.keys())
        ]
        payload = {"updated_at": time.time(), "peers": peers}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            print(f"warning: could not write signaling state: {exc}", flush=True)

    async def register(self, websocket, role: str, room: str):
        key = (room, role)
        notify_peer = None
        async with self.lock:
            old = self.peers.get(key)
            if old and old is not websocket:
                await old.close()
            self.peers[key] = websocket
            self.write_state()
            queued = [text for _created_at, text in self.pending.pop(key, [])]
            notify_peer = self.peers.get((room, self.target_role(role)))
        for text in queued:
            await websocket.send(text)
        if notify_peer is not None:
            await notify_peer.send(json.dumps({"type": "peer-ready", "role": role, "room": room}))
        print(f"registered role={role} room={room}", flush=True)

    async def unregister(self, websocket):
        notifications = []
        async with self.lock:
            for key, peer in list(self.peers.items()):
                if peer is websocket:
                    del self.peers[key]
                    self.write_state()
                    notify_peer = self.peers.get((key[0], self.target_role(key[1])))
                    if notify_peer is not None:
                        notifications.append((notify_peer, key[1], key[0]))
                    print(f"unregistered role={key[1]} room={key[0]}", flush=True)
        for peer, role, room in notifications:
            try:
                await peer.send(json.dumps({"type": "peer-left", "role": role, "room": room}))
            except Exception as exc:
                print(f"warning: could not notify peer-left role={role} room={room}: {exc}", flush=True)

    async def forward(self, room: str, role: str, text: str):
        target = (room, self.target_role(role))
        async with self.lock:
            peer = self.peers.get(target)
            if peer is None:
                self._queue_pending_locked(target, text)
                return
        await peer.send(text)

    def _is_authorized(self, message: dict) -> bool:
        if not self.auth_token:
            return True
        return message.get("token") == self.auth_token

    @staticmethod
    def _pending_size(entries) -> int:
        return sum(len(text.encode("utf-8")) for _created_at, text in entries)

    def _queue_pending_locked(self, target, text: str):
        now = time.monotonic()
        queue = self.pending[target]
        if self.pending_ttl_sec > 0.0:
            cutoff = now - self.pending_ttl_sec
            queue[:] = [(created_at, item) for created_at, item in queue if created_at >= cutoff]
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
                    if role not in {"robot", "machine"}:
                        await websocket.close(reason="role must be robot or machine")
                        return
                    await self.register(websocket, role, room)
                    continue
                if role is None or room is None:
                    await websocket.close(reason="first message must be register")
                    return
                await self.forward(room, role, text)
        finally:
            await self.unregister(websocket)


async def main_async(args):
    if not args.allow_unauthenticated and not args.auth_token and not is_loopback_host(args.host):
        raise SystemExit(
            "Refusing to start unauthenticated WebRTC relay on a non-loopback host. "
            "Set HORUS_WEBRTC_RELAY_TOKEN or pass --allow-unauthenticated for trusted test networks."
        )
    relay = Relay(
        args.state_file,
        auth_token=args.auth_token,
        max_pending_per_peer=args.max_pending_per_peer,
        max_pending_bytes=args.max_pending_bytes,
        pending_ttl_sec=args.pending_ttl_sec,
    )
    relay.write_state()
    async with websockets.serve(relay.handle, args.host, args.port, max_size=args.max_message_bytes):
        print(f"WebRTC signaling relay listening on ws://{args.host}:{args.port}", flush=True)
        await asyncio.Future()


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in {"127.0.0.1", "::1", "localhost"}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-message-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--state-file", default="", help="write active room membership for the live monitor")
    parser.add_argument("--auth-token", default=os.environ.get("HORUS_WEBRTC_RELAY_TOKEN", ""))
    parser.add_argument("--allow-unauthenticated", action="store_true")
    parser.add_argument("--max-pending-per-peer", type=int, default=int(os.environ.get("HORUS_WEBRTC_RELAY_MAX_PENDING_PER_PEER", "128")))
    parser.add_argument("--max-pending-bytes", type=int, default=int(os.environ.get("HORUS_WEBRTC_RELAY_MAX_PENDING_BYTES", str(4 * 1024 * 1024))))
    parser.add_argument("--pending-ttl-sec", type=float, default=float(os.environ.get("HORUS_WEBRTC_RELAY_PENDING_TTL_SEC", "30.0")))
    return parser.parse_args()


def main():
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
