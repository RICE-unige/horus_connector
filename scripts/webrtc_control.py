"""Shared WebRTC control-channel authorization helpers."""

from __future__ import annotations

import hmac
import math
import time
from dataclasses import dataclass


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


@dataclass(frozen=True)
class CommandDecision:
    accepted: bool
    reason: str = ""


class CommandGate:
    """Rejects stale, future, duplicate, and out-of-order remote commands."""

    def __init__(self, max_age_ms: float = 1000.0, max_future_ms: float = 5000.0):
        self.max_age_ns = max(0, int(float(max_age_ms) * 1_000_000.0))
        self.max_future_ns = max(0, int(float(max_future_ms) * 1_000_000.0))
        self.last_seq_by_session: dict[str, int] = {}

    def validate(self, command: dict, now_ns: int | None = None) -> CommandDecision:
        if not isinstance(command, dict):
            return CommandDecision(False, "command must be an object")
        command_type = str(command.get("type") or "")
        if command_type not in {"cmd_vel", "estop", "estop_clear"}:
            return CommandDecision(False, "unsupported command type")

        sent_ns = command.get("sent_ns")
        if sent_ns is None:
            return CommandDecision(False, "missing sent_ns")
        try:
            sent_ns = int(sent_ns)
        except (TypeError, ValueError, OverflowError):
            return CommandDecision(False, "invalid sent_ns")
        now = int(now_ns if now_ns is not None else time.time_ns())
        if self.max_age_ns and sent_ns < now - self.max_age_ns:
            return CommandDecision(False, "stale command")
        if self.max_future_ns and sent_ns > now + self.max_future_ns:
            return CommandDecision(False, "future command")

        session_id = str(command.get("session_id") or "legacy")
        try:
            seq = int(command.get("seq"))
        except (TypeError, ValueError, OverflowError):
            return CommandDecision(False, "missing or invalid seq")
        previous = self.last_seq_by_session.get(session_id)
        if previous is not None and seq <= previous:
            return CommandDecision(False, "duplicate or out-of-order command")

        if command_type == "cmd_vel":
            for field in (
                "linear_x",
                "linear_y",
                "linear_z",
                "angular_x",
                "angular_y",
                "angular_z",
            ):
                try:
                    value = float(command.get(field, 0.0))
                except (TypeError, ValueError, OverflowError):
                    return CommandDecision(False, f"invalid {field}")
                if not math.isfinite(value):
                    return CommandDecision(False, f"non-finite {field}")
        self.last_seq_by_session[session_id] = seq
        return CommandDecision(True)
