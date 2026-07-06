#!/usr/bin/env python3
"""Render the HORUS camera transport benchmark from run artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / ".run"
DATA_PATH = ROOT / "docs" / "transport_latency_results.json"
SVG_PATH = ROOT / "docs" / "transport_benchmark.svg"

DEADLINE_MS = 150.0
DURATION_SEC = 120.0
TARGET_FPS = 30.0

RESOLUTIONS = {
    "1080p30": {"label": "1080p30", "width": 1920, "height": 1080},
    "720p30": {"label": "720p30", "width": 1280, "height": 720},
}
PATHS = ["lan", "vpn", "cloud"]
PATH_LABELS = {"lan": "LAN", "vpn": "VPN", "cloud": "Cloud hub"}
TRANSPORTS = ["dds", "zenoh", "webrtc"]
TRANSPORT_LABELS = {
    "dds": "ROS 2 DDS",
    "zenoh": "ROS 2 Zenoh bridge",
    "webrtc": "WebRTC H.264",
}
COLORS = {"dds": "#1D1D1F", "zenoh": "#0071E3", "webrtc": "#30D158"}


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def load_samples_payload(path: Path) -> dict:
    payload = read_json(path)
    if not payload:
        return {"samples": []}
    return payload


def corrected_stats(samples: list[dict]) -> dict:
    latencies = [float(sample["latency_ms"]) for sample in samples if sample.get("latency_ms") is not None]
    fresh = 0
    for sample in samples:
        if sample.get("fresh") is True:
            fresh += 1
            continue
        latency = sample.get("latency_ms")
        if latency is not None and float(latency) <= DEADLINE_MS:
            fresh += 1
    return {
        "fresh_samples": fresh,
        "fresh_sample_ratio": fresh / len(latencies) if latencies else 0.0,
        "latency_ms_p50": rounded(percentile(latencies, 0.50)),
        "latency_ms_p95": rounded(percentile(latencies, 0.95)),
        "latency_ms_p99": rounded(percentile(latencies, 0.99)),
    }


def has_measured_clock_offset(*sources: object) -> bool:
    for source in sources:
        value = str(source or "").strip().lower()
        if value and value not in {"none", "unknown", "unmeasured"}:
            return True
    return False


def ros_result(resolution: str, path: str, transport: str) -> dict | None:
    name = f"modeb_{resolution}_{path}_{transport}"
    pub = read_json(RUN_DIR / f"{name}_pub.json")
    sub = read_json(RUN_DIR / f"{name}_sub.json")
    samples_payload = load_samples_payload(RUN_DIR / f"{name}_sub.samples.json")
    samples = samples_payload.get("samples", [])
    if not pub or not sub:
        return None
    if not sub.get("latency_method"):
        return {
            "resolution": resolution,
            "path": path,
            "transport": transport,
            "status": "legacy_method",
            "reason": "Run artifact predates fresh-frame SLA and measured clock-offset metadata; rerun before publication.",
        }
    if not has_measured_clock_offset(sub.get("clock_offset_source"), samples_payload.get("clock_offset_source")):
        return {
            "resolution": resolution,
            "path": path,
            "transport": transport,
            "status": "unmeasured_clock_offset",
            "reason": "Cross-host benchmark artifact has no measured clock-offset source.",
        }
    stats = corrected_stats(samples)
    published = int(sub.get("estimated_published_frames") or pub.get("messages") or round(DURATION_SEC * TARGET_FPS))
    received = int(sub.get("messages") or 0)
    fresh = int(sub.get("fresh_messages") if sub.get("fresh_messages") is not None else min(stats["fresh_samples"], received))
    fresh_sla = float(sub.get("fresh_frame_sla") if sub.get("fresh_frame_sla") is not None else (fresh / published if published else 0.0))
    observed = float(sub.get("observed_sec") or DURATION_SEC)
    return {
        "resolution": resolution,
        "path": path,
        "transport": transport,
        "status": "ok",
        "published_frames": published,
        "received_frames": received,
        "delivery_ratio": rounded(received / published if published else 0.0, 3),
        "fresh_frames": fresh,
        "fresh_frame_sla_percent": rounded(fresh_sla * 100.0),
        "usable_fps": rounded(float(sub.get("usable_fps") if sub.get("usable_fps") is not None else (fresh / observed if observed else 0.0))),
        "decoded_or_received_fps": rounded(float(sub.get("fps") or 0.0)),
        "received_mbps": rounded(float(sub.get("mbps") or 0.0)),
        "payload_bytes": int(pub.get("payload_bytes") or 0),
        "stale_frames": int(sub.get("stale_messages") or 0),
        "dropped_or_skipped_frames": int(sub.get("dropped_or_skipped_frames") or max(0, published - received)),
        "clock_offset_ms": rounded(float(sub.get("clock_offset_ms") or samples_payload.get("clock_offset_ms") or 0.0)),
        "clock_offset_source": sub.get("clock_offset_source") or samples_payload.get("clock_offset_source") or "unknown",
        "freshest_frame_policy": sub.get("freshest_frame_policy") or samples_payload.get("freshest_frame_policy") or "best_effort_keep_last",
        **stats,
    }


def webrtc_result(resolution: str, path: str) -> dict | None:
    name = f"modeb_{resolution}_{path}_webrtc"
    payload = read_json(RUN_DIR / f"{name}_latency.json")
    if not payload:
        return None
    if not payload.get("latency_method"):
        return {
            "resolution": resolution,
            "path": path,
            "transport": "webrtc",
            "status": "legacy_method",
            "reason": "Run artifact predates fresh-frame SLA and measured clock-offset metadata; rerun before publication.",
        }
    if not has_measured_clock_offset(payload.get("clock_offset_source")):
        return {
            "resolution": resolution,
            "path": path,
            "transport": "webrtc",
            "status": "unmeasured_clock_offset",
            "reason": "Cross-host benchmark artifact has no measured clock-offset source.",
        }
    samples = [float(sample["latency_ms"]) for sample in payload.get("samples", [])]
    stats = corrected_stats(payload.get("samples", []))
    expected = int(round(DURATION_SEC * TARGET_FPS))
    received = int(payload.get("video_frames") or 0)
    delivery_ratio = min(received / expected, 1.0) if expected else 0.0
    fresh_samples = int(payload.get("fresh_latency_samples") if payload.get("fresh_latency_samples") is not None else stats["fresh_samples"])
    fresh_sla = min(fresh_samples / expected, 1.0) if expected else 0.0
    return {
        "resolution": resolution,
        "path": path,
        "transport": "webrtc",
        "status": "ok",
        "published_frames": expected,
        "received_frames": received,
        "delivery_ratio": rounded(delivery_ratio, 3),
        "fresh_frames": fresh_samples,
        "fresh_frame_sla_percent": rounded(fresh_sla * 100.0),
        "usable_fps": rounded(float(payload.get("fresh_fps_estimate") if payload.get("fresh_fps_estimate") is not None else TARGET_FPS * fresh_sla)),
        "decoded_or_received_fps": rounded(float(payload.get("video_decoded_fps") or 0.0)),
        "received_mbps": None,
        "payload_bytes": None,
        "stale_frames": int(payload.get("stale_latency_samples") or max(0, len(samples) - fresh_samples)),
        "dropped_or_skipped_frames": int(payload.get("frame_clock_dropped") or max(0, expected - received)),
        "clock_offset_ms": rounded(float(payload.get("clock_offset_ms") or 0.0)),
        "clock_offset_source": payload.get("clock_offset_source") or "unknown",
        "freshest_frame_policy": payload.get("freshest_frame_policy") or "webrtc_jitter_buffer_keep_last",
        **stats,
    }


def build_results() -> dict:
    results: list[dict] = []
    for resolution in RESOLUTIONS:
        for path in PATHS:
            for transport in TRANSPORTS:
                if transport == "dds" and path == "cloud":
                    results.append(
                        {
                            "resolution": resolution,
                            "path": path,
                            "transport": transport,
                            "status": "not_applicable",
                            "reason": "ROS 2 DDS was not tested through the cloud hub because this project uses Zenoh as the hub transport.",
                        }
                    )
                    continue
                result = webrtc_result(resolution, path) if transport == "webrtc" else ros_result(resolution, path, transport)
                if result is not None:
                    results.append(result)
                else:
                    results.append(
                        {
                            "resolution": resolution,
                            "path": path,
                            "transport": transport,
                            "status": "missing",
                        }
                    )
    return {
        "metadata": {
            "title": "Mode B camera transport benchmark",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_sec": DURATION_SEC,
            "target_fps": TARGET_FPS,
            "fresh_deadline_ms": DEADLINE_MS,
            "clock_correction": "Sender/receiver clock offset is measured with scripts/clock_offset_probe.py and applied to each sample when available; no percentile-baseline subtraction is used.",
            "decision_metric": "Fresh-frame SLA: fresh decoded/received frames under the deadline divided by captured frames. Dropped and stale frames both count as failures.",
            "camera_qos_policy": "Camera-like ROS paths use best-effort keep-last QoS, normally depth 1, to model newest-frame semantics.",
            "camera_paths": {
                "dds": "sensor_msgs/CompressedImage over ROS 2 DDS, best-effort keep-last QoS",
                "zenoh": "sensor_msgs/CompressedImage over zenoh-bridge-ros2dds with camera-throughput profile",
                "webrtc": "H.264 video over WebRTC using the detected low-latency GStreamer profile",
            },
            "research_basis": [
                "ROS 2 QoS sensor-data guidance: favor latest samples over guaranteed delivery for sensor streams.",
                "W3C WebRTC statistics: dropped/rendered frames, jitter-buffer delay, and processing delay are standard media diagnostics.",
                "Teleoperation benchmarks should vary bandwidth, latency, jitter, and loss while reporting video freshness, quality, and task-impacting delay.",
            ],
            "topologies": {
                "lan": "WSL robot to remote machine over local LAN",
                "vpn": "WSL robot to remote machine over Tailscale",
                "cloud": "WSL robot to remote machine through Google Cloud hub/signaling",
            },
        },
        "results": results,
    }


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def text(x: float, y: float, value: object, *, size: int = 14, fill: str = "#1D1D1F", weight: int = 400, anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="Inter, SF Pro Display, Segoe UI, Arial, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{esc(value)}</text>'
    )


def line(x1: float, y1: float, x2: float, y2: float, *, stroke: str, width: float = 1.0, opacity: float = 1.0, dash: str = "") -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{width:.1f}" opacity="{opacity:.2f}"{extra}/>'
    )


def rect(x: float, y: float, w: float, h: float, *, fill: str, stroke: str = "none", rx: float = 8) -> str:
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx:.1f}" fill="{fill}" stroke="{stroke}"/>'


def circle(x: float, y: float, r: float, *, fill: str, stroke: str = "#FFFFFF") -> str:
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'


def polyline(points: list[tuple[float, float]], color: str) -> str:
    if not points:
        return ""
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3.6" stroke-linecap="round" stroke-linejoin="round"/>'


def get_result(data: dict, resolution: str, path: str, transport: str) -> dict | None:
    for result in data["results"]:
        if result["resolution"] == resolution and result["path"] == path and result["transport"] == transport:
            return result if result.get("status") == "ok" else None
    return None


def panel_values(data: dict, resolution: str, metric: str) -> list[float]:
    values = []
    for path in PATHS:
        for transport in TRANSPORTS:
            result = get_result(data, resolution, path, transport)
            if result and result.get(metric) is not None:
                values.append(float(result[metric]))
    return values


def y_max_for(data: dict, resolution: str, metric: str) -> float:
    values = panel_values(data, resolution, metric)
    if metric == "fresh_frame_sla_percent":
        return max(25.0, math.ceil((max(values or [0.0]) * 1.20) / 5.0) * 5.0)
    if metric == "usable_fps":
        return max(6.0, math.ceil((max(values or [0.0]) * 1.20) / 2.0) * 2.0)
    return max(3200.0, math.ceil((max(values or [0.0]) * 1.10) / 500.0) * 500.0)


def format_tick(value: float, metric: str) -> str:
    if metric == "fresh_frame_sla_percent":
        return f"{value:.0f}%"
    if metric == "usable_fps":
        return f"{value:.0f}"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"


def draw_panel(data: dict, resolution: str, metric: str, title: str, note: str, x: float, y: float, w: float, h: float) -> list[str]:
    ymax = y_max_for(data, resolution, metric)
    plot_x = x + 58
    plot_y = y + 78
    plot_w = w - 96
    plot_h = h - 132

    def x_for(index: int) -> float:
        return plot_x + index * (plot_w / (len(PATHS) - 1))

    def y_for(value: float) -> float:
        return plot_y + plot_h - max(0.0, min(value, ymax)) / ymax * plot_h

    parts = [
        rect(x, y, w, h, fill="#FFFFFF", stroke="#E5E5EA", rx=10),
        text(x + 24, y + 34, f"{RESOLUTIONS[resolution]['label']} - {title}", size=19, weight=700),
        text(x + 24, y + 58, note, size=12, fill="#667085", weight=600),
    ]
    for tick in [0.0, ymax * 0.25, ymax * 0.50, ymax * 0.75, ymax]:
        ty = y_for(tick)
        parts.append(line(plot_x, ty, plot_x + plot_w, ty, stroke="#E5E5EA", opacity=0.9))
        parts.append(text(plot_x - 12, ty + 4, format_tick(tick, metric), size=11, fill="#86868B", anchor="end"))
    for index, path in enumerate(PATHS):
        px = x_for(index)
        parts.append(line(px, plot_y, px, plot_y + plot_h, stroke="#F2F4F7", opacity=0.9))
        parts.append(text(px, plot_y + plot_h + 28, PATH_LABELS[path], size=12, fill="#667085", weight=700, anchor="middle"))
    parts.append(line(plot_x, plot_y + plot_h, plot_x + plot_w, plot_y + plot_h, stroke="#D0D5DD", width=1.2))

    if not panel_values(data, resolution, metric):
        parts.append(text(plot_x + plot_w / 2, plot_y + plot_h / 2 - 8, "Rerun required", size=18, fill="#667085", weight=800, anchor="middle"))
        parts.append(
            text(
                plot_x + plot_w / 2,
                plot_y + plot_h / 2 + 18,
                "Legacy artifacts are not plotted",
                size=12,
                fill="#98A2B3",
                weight=600,
                anchor="middle",
            )
        )
        return parts

    label_offsets = {"dds": -16, "zenoh": 13, "webrtc": 29}
    for transport in TRANSPORTS:
        points = []
        labels = []
        for index, path in enumerate(PATHS):
            result = get_result(data, resolution, path, transport)
            if not result or result.get(metric) is None:
                continue
            value = float(result[metric])
            px = x_for(index)
            py = y_for(value)
            points.append((px, py))
            labels.append((px, py, value))
        color = COLORS[transport]
        parts.append(polyline(points, color))
        for px, py, value in labels:
            parts.append(circle(px, py, 4.7, fill=color))
            if metric == "fresh_frame_sla_percent":
                label = f"{value:.0f}%"
            elif metric == "latency_ms_p95":
                label = format_tick(value, metric)
            else:
                label = f"{value:.1f}"
            label_y = max(plot_y + 10, min(plot_y + plot_h - 8, py + label_offsets[transport]))
            parts.append(text(px, label_y, label, size=10, fill=color, weight=700, anchor="middle"))
    return parts


def render_svg(data: dict) -> str:
    width = 1800
    height = 1120
    margin = 64
    panel_w = 530
    panel_h = 360
    gap_x = 34
    gap_y = 54
    top = 204
    metrics = [
        ("fresh_frame_sla_percent", "Fresh-frame SLA", f"Higher is better. Deadline {DEADLINE_MS:.0f} ms."),
        ("latency_ms_p95", "P95 latency", "Lower is better. Offset-corrected."),
        ("usable_fps", "Usable FPS", "Higher is better. Target is 30 FPS."),
    ]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="auto" viewBox="0 0 {width} {height}" role="img">',
        "<title>HORUS camera transport benchmark</title>",
        "<desc>Mode B comparison of ROS 2 DDS, ROS 2 Zenoh bridge, and WebRTC H.264 camera transport at 1080p30 and 720p30.</desc>",
        rect(0, 0, width, height, fill="#F5F5F7", rx=0),
        text(margin, 68, "Camera Transport Benchmark", size=40, weight=800),
        text(
            margin,
            106,
            "Mode B best practical paths: compressed ROS images over DDS/Zenoh and H.264 over WebRTC.",
            size=19,
            fill="#515154",
            weight=600,
        ),
        text(
            margin,
            136,
            f"{DURATION_SEC:.0f} s per point, {TARGET_FPS:.0f} FPS source, {DEADLINE_MS:.0f} ms fresh-frame deadline. Lines compare LAN, VPN, and cloud hub.",
            size=14,
            fill="#667085",
        ),
    ]
    legend_x = margin
    for transport in TRANSPORTS:
        parts.append(circle(legend_x + 8, 176, 6, fill=COLORS[transport], stroke=COLORS[transport]))
        parts.append(text(legend_x + 24, 181, TRANSPORT_LABELS[transport], size=14, fill="#344054", weight=700))
        legend_x += 260 if transport != "webrtc" else 230

    for row, resolution in enumerate(RESOLUTIONS):
        for col, (metric, title, note) in enumerate(metrics):
            x = margin + col * (panel_w + gap_x)
            y = top + row * (panel_h + gap_y)
            parts.extend(draw_panel(data, resolution, metric, title, note, x, y, panel_w, panel_h))

    parts.append(
        text(
            margin,
            height - 42,
            "Fresh-frame SLA = frames received under the 150 ms deadline divided by captured frames. Stale and dropped frames count as failures.",
            size=12,
            fill="#667085",
        )
    )
    parts.append(
        text(
            margin,
            height - 22,
            "Clock offset is measured before each run with the probe artifact when available; the renderer does not subtract a percentile baseline.",
            size=12,
            fill="#667085",
        )
    )
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    data = build_results()
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    SVG_PATH.write_text(render_svg(data), encoding="utf-8")
    print(f"wrote {DATA_PATH}")
    print(f"wrote {SVG_PATH}")


if __name__ == "__main__":
    main()
