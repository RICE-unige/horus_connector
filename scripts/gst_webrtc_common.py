#!/usr/bin/env python3
"""Shared helpers for GStreamer WebRTC media scripts."""

import json
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import gi

try:
    from websockets.sync.client import connect
    from websockets.sync.server import serve
except Exception:
    connect = None
    serve = None

try:
    import websocket
except Exception:
    websocket = None

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402


def load_env_file(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def first_ice_server(servers: str, prefix: str) -> str:
    for server in [item.strip() for item in servers.split(",") if item.strip()]:
        if server.startswith(prefix):
            return server
    return ""


def stun_property(servers: str) -> str:
    server = first_ice_server(servers, "stun:")
    if not server:
        return ""
    if server.startswith("stun://"):
        return server
    return "stun://" + server[len("stun:") :]


def turn_property(servers: str) -> str:
    server = first_ice_server(servers, "turn:")
    if not server:
        return ""
    if server.startswith("turn://") or server.startswith("turns://"):
        return server
    return "turn://" + server[len("turn:") :]


def webrtcbin_properties(ice_servers: str) -> str:
    props = ["bundle-policy=max-bundle"]
    stun = stun_property(ice_servers)
    turn = turn_property(ice_servers)
    if stun:
        props.append(f"stun-server={stun}")
    if turn:
        props.append(f"turn-server={turn}")
    return " ".join(props)


def configure_webrtcbin(element, ice_servers: str):
    if element.find_property("bundle-policy"):
        element.set_property("bundle-policy", "max-bundle")
    stun = stun_property(ice_servers)
    turn = turn_property(ice_servers)
    if stun and element.find_property("stun-server"):
        element.set_property("stun-server", stun)
    if turn and element.find_property("turn-server"):
        element.set_property("turn-server", turn)


def ensure_webrtc_runtime():
    missing: List[str] = []
    if Gst.ElementFactory.find("webrtcbin") is None:
        missing.append("gstreamer1.0-plugins-bad (webrtcbin)")
    if Gst.Registry.get().find_plugin("nice") is None:
        missing.append("gstreamer1.0-nice (ICE transport)")
    if missing:
        raise RuntimeError(
            "Native GStreamer WebRTC runtime is incomplete. Install: "
            + ", ".join(missing)
            + ". Run ./horus bootstrap <role> after installing system packages."
        )


def make_session_description(kind: str, sdp_text: str):
    result, sdp = GstSdp.SDPMessage.new()
    if result != GstSdp.SDPResult.OK:
        raise RuntimeError("failed to allocate SDP message")
    result = GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), sdp)
    if result != GstSdp.SDPResult.OK:
        raise RuntimeError(f"failed to parse remote SDP: {result}")
    sdp_type = {
        "offer": GstWebRTC.WebRTCSDPType.OFFER,
        "answer": GstWebRTC.WebRTCSDPType.ANSWER,
    }[kind]
    return GstWebRTC.WebRTCSessionDescription.new(sdp_type, sdp)


class JsonSignaling:
    def __init__(self, on_message, on_connected=None):
        self.on_message = on_message
        self.on_connected = on_connected
        self.ws = None
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.send_lock = threading.Lock()
        self.pending: queue.Queue[str] = queue.Queue()

    def send(self, payload: dict):
        text = json.dumps(payload, separators=(",", ":"))
        if not self.connected.wait(timeout=30):
            self.pending.put(text)
            return
        with self.send_lock:
            self.ws.send(text)

    def _flush_pending(self):
        while not self.pending.empty():
            with self.send_lock:
                self.ws.send(self.pending.get_nowait())

    def _read_loop(self):
        self.connected.set()
        self._flush_pending()
        if self.on_connected is not None:
            self.on_connected()
        try:
            for text in self.ws:
                try:
                    message = json.loads(text)
                    if message.get("type") == "register":
                        print(
                            f"WebRTC peer registered: role={message.get('role', 'unknown')} room={message.get('room', 'default')}",
                            flush=True,
                        )
                    self.on_message(message)
                except Exception as exc:
                    print(f"ignoring signaling message: {exc}", flush=True)
        finally:
            print("WebRTC signaling disconnected", flush=True)
            self.connected.clear()


class ClientSignaling(JsonSignaling):
    def __init__(self, url: str, role: str, room: str, on_message, on_connected=None):
        super().__init__(on_message, on_connected)
        self.url = url
        self.role = role
        self.room = room

    def start(self):
        if connect is None and websocket is None:
            raise RuntimeError("Install websockets>=12 or websocket-client for WebRTC signaling.")

        def run():
            while not self.closed.is_set():
                try:
                    if connect is not None:
                        self.ws = connect(self.url, open_timeout=10)
                        self.ws.send(json.dumps({"type": "register", "role": self.role, "room": self.room}))
                        print(
                            f"WebRTC signaling connected: role={self.role} room={self.room} url={self.url}",
                            flush=True,
                        )
                        self._read_loop()
                    else:
                        self.ws = websocket.create_connection(self.url, timeout=10)
                        self.ws.settimeout(None)
                        self.ws.send(json.dumps({"type": "register", "role": self.role, "room": self.room}))
                        print(
                            f"WebRTC signaling connected: role={self.role} room={self.room} url={self.url}",
                            flush=True,
                        )
                        self._legacy_read_loop()
                except Exception as exc:
                    print(f"signaling reconnect after error: {exc}", flush=True)
                    time.sleep(1)

        threading.Thread(target=run, daemon=True).start()

    def _legacy_read_loop(self):
        self.connected.set()
        self._flush_pending()
        if self.on_connected is not None:
            self.on_connected()
        try:
            while not self.closed.is_set():
                text = self.ws.recv()
                if not text:
                    break
                try:
                    message = json.loads(text)
                    if message.get("type") == "register":
                        print(
                            f"WebRTC peer registered: role={message.get('role', 'unknown')} room={message.get('room', 'default')}",
                            flush=True,
                        )
                    self.on_message(message)
                except Exception as exc:
                    print(f"ignoring signaling message: {exc}", flush=True)
        finally:
            print("WebRTC signaling disconnected", flush=True)
            self.connected.clear()


class ServerSignaling(JsonSignaling):
    def __init__(self, host: str, port: int, on_message, on_connected=None):
        super().__init__(on_message, on_connected)
        self.host = host
        self.port = port
        self.server = None

    def start(self):
        if serve is None:
            raise RuntimeError("Install websockets>=12 to run the local WebRTC signaling server.")

        def handler(ws):
            self.ws = ws
            remote = getattr(ws, "remote_address", None)
            if isinstance(remote, tuple):
                remote_text = f"{remote[0]}:{remote[1]}"
            else:
                remote_text = str(remote or "unknown")
            print(f"WebRTC signaling peer connected: {remote_text}", flush=True)
            try:
                self._read_loop()
            finally:
                print(f"WebRTC signaling peer disconnected: {remote_text}", flush=True)

        def run():
            with serve(handler, self.host, self.port) as server:
                self.server = server
                print(f"WebRTC signaling listening on ws://{self.host}:{self.port}", flush=True)
                server.serve_forever()

        threading.Thread(target=run, daemon=True).start()
