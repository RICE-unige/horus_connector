#!/usr/bin/env python3
"""Pure WebRTC network-stat extraction and bitrate decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def _items(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        return value.items()
    n_fields = getattr(value, "n_fields", None)
    nth_field_name = getattr(value, "nth_field_name", None)
    get_value = getattr(value, "get_value", None)
    if callable(n_fields) and callable(nth_field_name) and callable(get_value):
        return (
            (name, get_value(name))
            for index in range(n_fields())
            if (name := nth_field_name(index)) is not None
        )
    return ()


def _type_name(value: Any) -> str:
    text = getattr(value, "value_nick", None) or getattr(value, "value_name", None) or str(value or "")
    return str(text).lower().replace("_", "-")


def _number(structure: Any, name: str) -> float | None:
    for field, value in _items(structure):
        if field == name:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


@dataclass(frozen=True)
class NetworkSample:
    packet_loss_fraction: float | None = None
    round_trip_time_ms: float | None = None
    jitter_ms: float | None = None
    nack_count: int = 0
    pli_count: int = 0

    @property
    def known(self) -> bool:
        return self.packet_loss_fraction is not None or self.round_trip_time_ms is not None


def extract_network_sample(report: Any) -> NetworkSample:
    losses: list[float] = []
    rtts: list[float] = []
    jitters: list[float] = []
    nack_count = 0
    pli_count = 0

    for _identifier, structure in _items(report):
        fields = dict(_items(structure))
        kind = str(fields.get("kind") or "").lower()
        if kind and kind != "video":
            continue
        stats_type = _type_name(fields.get("type"))
        if "remote-inbound-rtp" in stats_type:
            loss = _number(structure, "fraction-lost")
            rtt = _number(structure, "round-trip-time")
            if loss is not None:
                losses.append(max(0.0, min(1.0, loss)))
            if rtt is not None:
                rtts.append(max(0.0, rtt * 1000.0))
        if "inbound-rtp" in stats_type:
            jitter = _number(structure, "jitter")
            if jitter is not None:
                jitters.append(max(0.0, jitter * 1000.0))
        if "outbound-rtp" in stats_type:
            nack_count += int(_number(structure, "nack-count") or 0)
            pli_count += int(_number(structure, "pli-count") or 0)

    return NetworkSample(
        packet_loss_fraction=max(losses) if losses else None,
        round_trip_time_ms=max(rtts) if rtts else None,
        jitter_ms=max(jitters) if jitters else None,
        nack_count=nack_count,
        pli_count=pli_count,
    )


class BitrateController:
    """Uses loss/RTT with hysteresis; unknown network health never increases bitrate."""

    def __init__(
        self,
        minimum_kbit: int,
        maximum_kbit: int,
        step_kbit: int,
        bad_loss: float = 0.08,
        good_loss: float = 0.02,
        bad_rtt_ms: float = 350.0,
        good_rtt_ms: float = 150.0,
        good_samples_required: int = 3,
    ):
        self.minimum_kbit = max(1, int(minimum_kbit))
        self.maximum_kbit = max(self.minimum_kbit, int(maximum_kbit))
        self.step_kbit = max(1, int(step_kbit))
        self.bad_loss = max(0.0, float(bad_loss))
        self.good_loss = max(0.0, float(good_loss))
        self.bad_rtt_ms = max(1.0, float(bad_rtt_ms))
        self.good_rtt_ms = max(1.0, float(good_rtt_ms))
        self.good_samples_required = max(1, int(good_samples_required))
        self.good_samples = 0
        self.last_nack_count = 0
        self.last_pli_count = 0

    def target(self, current_kbit: int, sample: NetworkSample) -> tuple[int, str]:
        current = max(self.minimum_kbit, min(int(current_kbit), self.maximum_kbit))
        nack_delta = max(0, sample.nack_count - self.last_nack_count)
        pli_delta = max(0, sample.pli_count - self.last_pli_count)
        self.last_nack_count = sample.nack_count
        self.last_pli_count = sample.pli_count

        bad = (
            (sample.packet_loss_fraction is not None and sample.packet_loss_fraction >= self.bad_loss)
            or (sample.round_trip_time_ms is not None and sample.round_trip_time_ms >= self.bad_rtt_ms)
            or pli_delta > 0
            or nack_delta >= 10
        )
        if bad:
            self.good_samples = 0
            return max(self.minimum_kbit, current - self.step_kbit), "network-congested"

        good = (
            sample.known
            and (sample.packet_loss_fraction is None or sample.packet_loss_fraction <= self.good_loss)
            and (sample.round_trip_time_ms is None or sample.round_trip_time_ms <= self.good_rtt_ms)
            and nack_delta == 0
            and pli_delta == 0
        )
        self.good_samples = self.good_samples + 1 if good else 0
        if self.good_samples >= self.good_samples_required:
            self.good_samples = 0
            return min(self.maximum_kbit, current + self.step_kbit), "network-healthy"
        return current, "hold"
