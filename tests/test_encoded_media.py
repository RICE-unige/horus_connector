import json
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from encoded_media import EncodedDelivery, contains_idr, load_config

IDR = b"\x00\x00\x00\x01\x65payload"
PFRAME = b"\x00\x00\x01\x41delta"


def test_relay_config_rejects_ambiguous_or_invalid_camera_inputs(tmp_path):
    valid = {"streams": [{"id": "front", "input_topic": "/front/image_raw", "output_topic": "/relay/front"}]}
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(valid))
    assert load_config(path)["streams"][0]["framerate"] == 15
    for overrides in ({"output_topic": "/front/image_raw"}, {"framerate": True},
                      {"image_type": "h265"}, {"id": "unsafe name"}, {"bitrate_kbps": 0},
                      {"framerate": 61}, {"bitrate_kbps": 99}):
        changed = {"streams": [{**valid["streams"][0], **overrides}]}
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError):
            load_config(path)


@pytest.mark.parametrize("value", [[], None, {"streams": [None]}, {"streams": [False]}])
def test_malformed_config_has_an_actionable_validation_error(tmp_path, value):
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_config(path)


def test_cross_stream_topic_loop_is_rejected(tmp_path):
    path = tmp_path / "relay.json"
    path.write_text(json.dumps({"streams": [
        {"id": "left", "input_topic": "/camera/left", "output_topic": "/relay/left"},
        {"id": "right", "input_topic": "/relay/left", "output_topic": "/relay/right"}]}))
    with pytest.raises(ValueError, match="overlap"):
        load_config(path)


def test_h264_delivery_starts_only_from_an_idr_and_preserves_bytes():
    frames = []
    delivery = EncodedDelivery(frames.append, lambda: None)
    try:
        assert not delivery.push(PFRAME)
        assert delivery.push(IDR)
        assert delivery.push(PFRAME)
        deadline = time.monotonic() + 1
        while delivery.stats()["published"] < 2 and time.monotonic() < deadline:
            time.sleep(.005)
        assert frames == [IDR, PFRAME]
        assert delivery.stats()["dropped"] == 1
    finally:
        delivery.close()
    assert not delivery.worker.is_alive()


def test_overflow_discards_prediction_chain_not_an_arbitrary_encoded_frame():
    entered, release = threading.Event(), threading.Event()
    requests, frames = [], []

    def publish(data):
        entered.set()
        assert release.wait(2)
        frames.append(data)

    delivery = EncodedDelivery(publish, lambda: requests.append(1), max_frames=1, max_bytes=1024)
    try:
        delivery.push(IDR)
        assert entered.wait(1)
        delivery.push(PFRAME)
        assert not delivery.push(PFRAME)
        assert delivery.stats()["pending"] == 0
        assert delivery.stats()["waiting_idr"]
        assert requests == [1]
        assert delivery.push(IDR)
        release.set()
        deadline = time.monotonic() + 1
        while len(frames) < 2 and time.monotonic() < deadline:
            time.sleep(.005)
        assert frames == [IDR, IDR]
    finally:
        release.set()
        delivery.close()


def test_idr_detection_accepts_three_and_four_byte_annex_b_delimiters():
    assert contains_idr(IDR)
    assert contains_idr(b"\x00\x00\x01\x67sps" + IDR)
    assert not contains_idr(PFRAME)
    assert not contains_idr(b"\x00\x00\x01")
