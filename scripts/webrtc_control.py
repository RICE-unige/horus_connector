"""Shared WebRTC control-channel authorization helpers."""

from __future__ import annotations

import hmac


def normalize_token(value) -> str:
    return str(value or "").strip()


def command_token(command: dict) -> str:
    for key in ("control_token", "lease_token", "session_token", "token"):
        token = normalize_token(command.get(key))
        if token:
            return token
    return ""


def command_authorized(
    command: dict,
    expected_token: str = "",
    allow_unauthenticated: bool = False,
) -> bool:
    if allow_unauthenticated:
        return True
    expected = normalize_token(expected_token)
    if not expected:
        return False
    return hmac.compare_digest(command_token(command), expected)
