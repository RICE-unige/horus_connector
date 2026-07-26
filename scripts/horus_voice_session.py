#!/usr/bin/env python3
"""Validated call lifecycle for HORUS operator/field-teammate voice signaling."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any


VOICE_PROTOCOL_VERSION = 1
VOICE_ROLES = frozenset({"operator", "field_teammate"})
VOICE_PEER_ROLES = {"operator": "field_teammate", "field_teammate": "operator"}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_VOICE_MESSAGES = frozenset(
    {
        "call_invite",
        "call_ringing",
        "call_accept",
        "call_decline",
        "call_cancel",
        "session_description",
        "ice_candidate",
        "call_connected",
        "mute_changed",
        "heartbeat",
        "call_end",
        "call_error",
    }
)
_TERMINAL_MESSAGES = frozenset({"call_decline", "call_cancel", "call_end", "call_error"})


@dataclass(frozen=True)
class VoiceSignalDecision:
    accepted: bool
    reason: str = ""


@dataclass
class VoiceCall:
    room: str
    call_id: str
    operator_session_id: str
    field_teammate_session_id: str
    state: str
    created_at: float
    updated_at: float


class VoiceCallRegistry:
    """Fail-closed validation for one active one-to-one voice call per room."""

    def __init__(self, call_ttl_sec: float = 3600.0):
        self.call_ttl_sec = max(30.0, float(call_ttl_sec))
        self.calls: dict[str, VoiceCall] = {}

    def validate_and_apply(
        self,
        room: str,
        role: str,
        message: dict[str, Any],
        now: float | None = None,
    ) -> VoiceSignalDecision:
        current_time = time.monotonic() if now is None else float(now)
        self._expire(current_time)

        if role not in VOICE_ROLES:
            return VoiceSignalDecision(False, "unsupported voice role")
        if not isinstance(message, dict):
            return VoiceSignalDecision(False, "voice signal must be an object")

        kind = str(message.get("type") or "")
        if kind not in _VOICE_MESSAGES:
            return VoiceSignalDecision(False, "unsupported voice signal type")
        try:
            protocol_version = int(message.get("protocol_version") or 0)
        except (TypeError, ValueError):
            return VoiceSignalDecision(False, "voice protocol version mismatch")
        if protocol_version != VOICE_PROTOCOL_VERSION:
            return VoiceSignalDecision(False, "voice protocol version mismatch")
        if str(message.get("room") or "") != room:
            return VoiceSignalDecision(False, "voice room mismatch")

        call_id = str(message.get("call_id") or "")
        session_id = str(message.get("session_id") or "")
        if not _IDENTIFIER.fullmatch(call_id):
            return VoiceSignalDecision(False, "invalid voice call id")
        if not _IDENTIFIER.fullmatch(session_id):
            return VoiceSignalDecision(False, "invalid voice session id")

        payload_decision = self._validate_payload(kind, message)
        if not payload_decision.accepted:
            return payload_decision

        active = self.calls.get(room)
        if active is None:
            if kind != "call_invite" or role != "operator":
                return VoiceSignalDecision(False, "voice call has not been invited")
            self.calls[room] = VoiceCall(
                room=room,
                call_id=call_id,
                operator_session_id=session_id,
                field_teammate_session_id="",
                state="ringing",
                created_at=current_time,
                updated_at=current_time,
            )
            return VoiceSignalDecision(True)

        if active.call_id != call_id:
            return VoiceSignalDecision(False, "another voice call is active in this room")

        expected_session = (
            active.operator_session_id
            if role == "operator"
            else active.field_teammate_session_id
        )
        if expected_session and expected_session != session_id:
            return VoiceSignalDecision(False, "voice sender session mismatch")
        if not expected_session and role == "field_teammate":
            if active.state != "ringing" or kind not in {
                "call_ringing",
                "call_accept",
                "call_decline",
            }:
                return VoiceSignalDecision(False, "field teammate session is not bound to this call")
            active.field_teammate_session_id = session_id

        decision = self._validate_transition(active.state, role, kind)
        if not decision.accepted:
            return decision

        if kind == "call_accept":
            active.state = "connecting"
        elif kind == "call_connected":
            active.state = "connected"
        elif kind == "call_error":
            active.state = "failed"

        active.updated_at = current_time
        if kind in _TERMINAL_MESSAGES:
            del self.calls[room]
        return VoiceSignalDecision(True)

    @staticmethod
    def _validate_payload(kind: str, message: dict[str, Any]) -> VoiceSignalDecision:
        if kind == "session_description":
            description_type = str(message.get("description_type") or "")
            sdp = message.get("sdp")
            if description_type not in {"offer", "answer"}:
                return VoiceSignalDecision(False, "invalid session description type")
            if not isinstance(sdp, str) or not sdp or len(sdp.encode("utf-8")) > 1024 * 1024:
                return VoiceSignalDecision(False, "invalid session description")
        elif kind == "ice_candidate":
            candidate = message.get("candidate")
            if not isinstance(candidate, str) or not candidate or len(candidate.encode("utf-8")) > 16 * 1024:
                return VoiceSignalDecision(False, "invalid ICE candidate")
        elif kind == "mute_changed" and not isinstance(message.get("muted"), bool):
            return VoiceSignalDecision(False, "mute state must be boolean")
        return VoiceSignalDecision(True)

    @staticmethod
    def _validate_transition(state: str, role: str, kind: str) -> VoiceSignalDecision:
        if kind == "call_invite":
            return VoiceSignalDecision(False, "voice call is already active")
        if kind == "call_ringing":
            return VoiceSignalDecision(
                state == "ringing" and role == "field_teammate",
                "only the field teammate may ring an invited call",
            )
        if kind in {"call_accept", "call_decline"}:
            return VoiceSignalDecision(
                state == "ringing" and role == "field_teammate",
                "only the field teammate may answer a ringing call",
            )
        if kind == "call_cancel":
            return VoiceSignalDecision(
                state == "ringing" and role == "operator",
                "only the operator may cancel a ringing call",
            )
        if kind in {"session_description", "ice_candidate"}:
            return VoiceSignalDecision(
                state in {"connecting", "connected"},
                "media negotiation requires an accepted call",
            )
        if kind == "call_connected":
            return VoiceSignalDecision(
                state in {"connecting", "connected"},
                "call cannot connect before it is accepted",
            )
        if kind == "mute_changed":
            return VoiceSignalDecision(
                state == "connected",
                "mute changes require a connected call",
            )
        if kind == "heartbeat":
            return VoiceSignalDecision(True)
        if kind in {"call_end", "call_error"}:
            return VoiceSignalDecision(True)
        return VoiceSignalDecision(False, "invalid voice call transition")

    def remove_room(self, room: str) -> None:
        self.calls.pop(room, None)

    def _expire(self, now: float) -> None:
        cutoff = now - self.call_ttl_sec
        for room, call in list(self.calls.items()):
            if call.updated_at < cutoff:
                del self.calls[room]
