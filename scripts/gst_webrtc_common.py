#!/usr/bin/env python3
"""Shared helpers for GStreamer WebRTC media scripts."""

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import gi

try:
    from websockets.sync.client import connect
    from websockets.sync.server import serve
except ImportError:
    connect = None
    serve = None

try:
    import websocket
except ImportError:
    websocket = None

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402

logger = logging.getLogger(__name__)


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


def normalize_ice_transport_policy(value: str) -> str:
    policy = (value or "all").strip().lower()
    return policy if policy in {"all", "relay"} else "all"


def webrtcbin_properties(ice_servers: str, ice_transport_policy: str = "all") -> str:
    props = ["bundle-policy=max-bundle"]
    policy = normalize_ice_transport_policy(ice_transport_policy)
    if policy != "all":
        props.append(f"ice-transport-policy={policy}")
    stun = stun_property(ice_servers)
    turn = turn_property(ice_servers)
    if stun:
        props.append(f"stun-server={stun}")
    if turn:
        props.append(f"turn-server={turn}")
    return " ".join(props)


def configure_webrtcbin(element, ice_servers: str, ice_transport_policy: str = "all"):
    if element.find_property("bundle-policy"):
        element.set_property("bundle-policy", "max-bundle")
    policy = normalize_ice_transport_policy(ice_transport_policy)
    if element.find_property("ice-transport-policy"):
        element.set_property("ice-transport-policy", policy)
    stun = stun_property(ice_servers)
    turn = turn_property(ice_servers)
    turn_added = None
    if stun and element.find_property("stun-server"):
        element.set_property("stun-server", stun)
    if turn and element.find_property("turn-server"):
        turn_added = False
        try:
            turn_added = bool(element.emit("add-turn-server", turn))
        except Exception:
            logger.debug("Failed to add TURN server through webrtcbin signal; falling back to property", exc_info=True)
            turn_added = False
        if not turn_added:
            element.set_property("turn-server", turn)
    if os.environ.get("HORUS_WEBRTC_DEBUG_ICE") == "1":
        print(
            "WebRTC ICE config: "
            f"policy={policy} stun={'on' if stun else 'off'} "
            f"turn={'on' if turn else 'off'} turn_registered={turn_added}",
            flush=True,
        )


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


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


class JsonSignaling:
    def __init__(self, on_message, on_connected=None):
        self.on_message = on_message
        self.on_connected = on_connected
        self.ws = None
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.send_lock = threading.Lock()
        self.pending: queue.Queue[str] = queue.Queue(maxsize=env_int("HORUS_WEBRTC_SIGNAL_PENDING_MAX", 256))

    @staticmethod
    def _log_control_message(message):
        kind = message.get("type")
        if kind == "register":
            print(
                f"WebRTC peer registered: role={message.get('role', 'unknown')} room={message.get('room', 'default')}",
                flush=True,
            )
        elif kind == "peer-ready":
            print(
                f"WebRTC peer ready: role={message.get('role', 'unknown')} room={message.get('room', 'default')}",
                flush=True,
            )
        elif kind == "peer-left":
            print(
                f"WebRTC peer left: role={message.get('role', 'unknown')} room={message.get('room', 'default')}",
                flush=True,
            )

    def send(self, payload: dict):
        text = json.dumps(payload, separators=(",", ":"))
        if not self.connected.wait(timeout=30):
            self._queue_pending(text)
            return
        with self.send_lock:
            self.ws.send(text)

    def _queue_pending(self, text: str):
        if self.pending.full():
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            print("dropping oldest pending WebRTC signaling message", flush=True)
        try:
            self.pending.put_nowait(text)
        except queue.Full:
            print("dropping WebRTC signaling message because pending queue is full", flush=True)

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
                    self._log_control_message(message)
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

    def _registration_text(self) -> str:
        payload = {"type": "register", "role": self.role, "room": self.room}
        token = os.environ.get("HORUS_WEBRTC_RELAY_TOKEN", "").strip()
        if token:
            payload["token"] = token
        return json.dumps(payload, separators=(",", ":"))

    def start(self):
        if connect is None and websocket is None:
            raise RuntimeError("Install websockets>=12 or websocket-client for WebRTC signaling.")

        def run():
            while not self.closed.is_set():
                try:
                    if connect is not None:
                        self.ws = connect(self.url, open_timeout=10)
                        self.ws.send(self._registration_text())
                        print(
                            f"WebRTC signaling connected: role={self.role} room={self.room} url={self.url}",
                            flush=True,
                        )
                        self._read_loop()
                    else:
                        self.ws = websocket.create_connection(self.url, timeout=10)
                        self.ws.settimeout(None)
                        self.ws.send(self._registration_text())
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
                    self._log_control_message(message)
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
