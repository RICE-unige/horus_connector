#!/usr/bin/env python3
"""Shared CSV metrics helpers for HORUS connector experiments."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

COMMON_FIELDS = ("timestamp_ns", "run_id", "experiment", "condition", "source")


def now_ns() -> int:
    return time.time_ns()


def env_results_dir() -> Path | None:
    value = os.environ.get("HORUS_EXPERIMENT_RESULTS_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


class CsvMetricWriter:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str = "",
        experiment: str = "",
        condition: str = "",
        source: str = "horus_connector",
        fieldnames: Sequence[str] = (),
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or os.environ.get("HORUS_EXPERIMENT_RUN_ID", "")
        self.experiment = experiment or os.environ.get("HORUS_EXPERIMENT", "")
        self.condition = condition or os.environ.get("HORUS_EXPERIMENT_CONDITION", "")
        self.source = source
        fields = list(COMMON_FIELDS)
        for field in fieldnames:
            if field not in fields:
                fields.append(field)
        write_header = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=fields, extrasaction="ignore")
        if write_header:
            self._writer.writeheader()

    def write(self, row: Mapping[str, Any]) -> None:
        payload = dict(row)
        payload.setdefault("timestamp_ns", now_ns())
        payload.setdefault("run_id", self.run_id)
        payload.setdefault("experiment", self.experiment)
        payload.setdefault("condition", self.condition)
        payload.setdefault("source", self.source)
        self._writer.writerow(payload)

    def close(self) -> None:
        self._handle.flush()
        self._handle.close()

    def __enter__(self) -> "CsvMetricWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def default_metrics_path(file_name: str) -> Path | None:
    root = env_results_dir()
    if root is None:
        return None
    return root / file_name
