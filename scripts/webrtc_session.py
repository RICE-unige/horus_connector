#!/usr/bin/env python3
"""Identity and credential helpers for HORUS WebRTC sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import quote


SIGNALING_PROTOCOL_VERSION = 2


@dataclass(frozen=True)
class ClientCredential:
    client_id: str
    token: str
    roles: tuple[str, ...]
    rooms: tuple[str, ...]
    capabilities: tuple[str, ...] = ()

    def permits(self, role: str, room: str, required_capability: str = "") -> bool:
        role_allowed = not self.roles or "*" in self.roles or role in self.roles
        room_allowed = not self.rooms or any(fnmatchcase(room, pattern) for pattern in self.rooms)
        capability_allowed = (
            not required_capability
            or "*" in self.capabilities
            or required_capability in self.capabilities
        )
        return role_allowed and room_allowed and capability_allowed


class CredentialStore:
    """Loads per-client signaling credentials without executing configuration."""

    def __init__(self, credentials: dict[str, ClientCredential] | None = None):
        self.credentials = credentials or {}

    @classmethod
    def from_file(cls, path: str) -> "CredentialStore":
        if not path:
            return cls()
        source = Path(path).expanduser()
        payload = json.loads(source.read_text(encoding="utf-8"))
        raw_clients = payload.get("clients", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_clients, dict):
            raise ValueError("WebRTC credentials file must contain a clients object")
        credentials: dict[str, ClientCredential] = {}
        for client_id, raw in raw_clients.items():
            if not isinstance(raw, dict):
                raise ValueError(f"Credential for {client_id!r} must be an object")
            token = str(raw.get("token") or "").strip()
            if not token:
                raise ValueError(f"Credential for {client_id!r} has no token")
            roles = tuple(str(value) for value in raw.get("roles", []))
            rooms = tuple(str(value) for value in raw.get("rooms", []))
            capabilities = tuple(str(value) for value in raw.get("capabilities", []))
            credentials[str(client_id)] = ClientCredential(
                str(client_id),
                token,
                roles,
                rooms,
                capabilities,
            )
        return cls(credentials)

    def configured(self) -> bool:
        return bool(self.credentials)

    def authorized(
        self,
        client_id: str,
        token: str,
        role: str,
        room: str,
        required_capability: str = "",
    ) -> bool:
        credential = self.credentials.get(str(client_id or ""))
        if credential is None or not credential.permits(role, room, required_capability):
            return False
        return hmac.compare_digest(credential.token, str(token or ""))


@dataclass(frozen=True)
class TurnCredential:
    username: str
    password: str
    expires_at: int
    urls: tuple[str, ...]

    def signaling_payload(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "credential": self.password,
            "expires_at": self.expires_at,
            "urls": list(self.urls),
        }


class TurnCredentialIssuer:
    """Creates coturn REST API credentials scoped to one session identity."""

    def __init__(
        self,
        host: str = "",
        secret: str = "",
        port: int = 3478,
        ttl_sec: int = 3600,
        transports: tuple[str, ...] = ("udp", "tcp"),
        secure: bool = False,
    ):
        self.host = str(host or "").strip()
        self.secret = str(secret or "")
        self.port = int(port)
        self.ttl_sec = max(60, int(ttl_sec))
        self.transports = tuple(value for value in transports if value in {"udp", "tcp"}) or ("udp",)
        self.secure = bool(secure)

    def configured(self) -> bool:
        return bool(self.host and self.secret)

    def issue(self, client_id: str, now: float | None = None) -> TurnCredential | None:
        if not self.configured():
            return None
        expires_at = int(now if now is not None else time.time()) + self.ttl_sec
        identity = str(client_id or "anonymous").replace(":", "_")
        username = f"{expires_at}:{identity}"
        digest = hmac.new(self.secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1).digest()
        password = base64.b64encode(digest).decode("ascii")
        scheme = "turns" if self.secure else "turn"
        encoded_user = quote(username, safe="")
        encoded_password = quote(password, safe="")
        urls = tuple(
            f"{scheme}://{encoded_user}:{encoded_password}@{self.host}:{self.port}?transport={transport}"
            for transport in self.transports
        )
        return TurnCredential(username, password, expires_at, urls)


def generate_client_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(max(16, int(bytes_count)))
