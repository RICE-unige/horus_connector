"""Assert decoded continuity, not merely creation of an RTP pad."""

import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for track in ("front", "rear"):
    measured = result["video_tracks"][track]
    assert measured["frames"] >= 20, (track, measured)
    assert measured["decoded_fps"] >= 5, (track, measured)
assert result.get("video_latency_samples", 0) >= 10, "No matched-frame timing samples"
assert "not_glass_to_glass" in result["latency_method"]
assert all("rtp_timestamp" in sample for sample in result["samples"])
assert sum(sample.get("timing_valid", False) for sample in result["samples"]) >= 10, "Local test must declare its shared clock"
