import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
pytest.importorskip("gi")
from ros_image_io import Gst, GstVideo, RosImagePublisher, RosImageAppSrc, contiguous_image_bytes, scale_intrinsics, source_header


@pytest.mark.parametrize("step,height", [(0, 2), (14, 2), (16, 0)])
def test_invalid_row_dimensions_are_rejected(step, height):
    with pytest.raises(ValueError):
        contiguous_image_bytes(bytes(32), step, 15, height)


def test_padded_rows_are_packed_without_padding():
    rows = bytes(range(15)) + b"x" + bytes(range(15, 30)) + b"y"
    assert contiguous_image_bytes(rows, 16, 15, 2) == bytes(range(30))
    with pytest.raises(ValueError):
        contiguous_image_bytes(rows[:30], 16, 15, 2)


def _publisher():
    publisher = RosImagePublisher("/test/image", "rgb8", "camera", "default")
    messages = []
    publisher.publisher = SimpleNamespace(publish=messages.append)
    publisher.node = SimpleNamespace(
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: None)),
        destroy_node=lambda: None,
    )
    publisher.image_type = lambda: SimpleNamespace(header=SimpleNamespace())
    return publisher, messages


def _sample(width=5, height=2, payload=None):
    Gst.init(None)
    caps = Gst.Caps.from_string(f"video/x-raw,format=RGB,width={width},height={height},framerate=30/1")
    layout = GstVideo.VideoInfo.new_from_caps(caps)
    payload = payload if payload is not None else bytes(layout.size)
    buffer = Gst.Buffer.new_allocate(None, len(payload), None)
    buffer.fill(0, payload)
    return Gst.Sample.new(buffer, caps, None, None)


def test_decoded_rgb_odd_width_preserves_pixels_and_ros_metadata():
    publisher, messages = _publisher()
    sample = _sample(payload=bytes(range(15)) + b"x" + bytes(range(15, 30)) + b"y")
    assert publisher.publish_sample(sample)
    assert len(messages) == 1
    assert messages[0].step == 15
    assert messages[0].width == 5
    assert messages[0].height == 2
    assert bytes(messages[0].data) == bytes(range(30))


def test_short_decode_and_callback_after_close_are_dropped():
    publisher, messages = _publisher()
    assert not publisher.publish_sample(_sample(payload=bytes(29)))
    publisher.close()
    assert not publisher.publish_sample(_sample())
    assert messages == []


def test_camera_calibration_scales_pixel_rows_only():
    assert scale_intrinsics([100, 0, 50, 0, 200, 40, 0, 0, 1], 3, .5, .25) == [50, 0, 25, 0, 50, 10, 0, 0, 1]
    with pytest.raises(ValueError):
        scale_intrinsics([float("nan")] * 9, 3, 1, 1)


def test_source_header_is_opt_in_and_never_mixes_clock_domains():
    publisher, messages = _publisher()
    metadata = {"sec": 100, "nanosec": 20, "frame_id": "source_camera"}
    assert publisher.publish_sample(_sample(), metadata)
    assert messages[-1].header.frame_id == "camera"
    publisher.preserve_source_header = True
    assert not publisher.publish_sample(_sample())
    assert publisher.metadata_missing_frames == 1


@pytest.mark.parametrize("metadata", [None, [], {}, {"sec": 1, "nanosec": 10**9, "frame_id": "camera"},
                                      {"sec": 2**31, "nanosec": 0, "frame_id": "camera"},
                                      {"sec": 1.0, "nanosec": 0, "frame_id": "camera"}])
def test_invalid_source_headers_are_dropped_without_callback_failure(metadata):
    publisher, messages = _publisher()
    publisher.preserve_source_header = True
    assert not publisher.publish_sample(_sample(), metadata)
    assert messages == []
    assert publisher.metadata_missing_frames == 1


def test_valid_source_header_keeps_ros_range_and_frame_identity():
    assert source_header({"sec": -1, "nanosec": 999999999, "frame_id": "camera"}) == (-1, 999999999, "camera")


def test_unsupported_cropped_calibration_clears_old_intrinsics():
    source = RosImageAppSrc(None, "/test", 15, "auto")
    source.camera_info = {"width": 640, "height": 480}
    source._on_camera_info(SimpleNamespace(binning_x=2, binning_y=0,
                                          roi=SimpleNamespace(width=0, height=0, x_offset=0, y_offset=0)))
    assert source.camera_info is None


def test_invalid_remote_calibration_does_not_stop_valid_images():
    publisher, messages = _publisher()
    publisher.camera_info_publisher = SimpleNamespace(publish=lambda _: pytest.fail("invalid calibration published"))
    assert publisher.publish_sample(_sample(), {"camera_info": {"width": 0, "height": 0}})
    assert len(messages) == 1


def test_high_rate_input_is_paced_before_copy(monkeypatch):
    import ros_image_io
    Gst.init(None)
    appsrc = Gst.ElementFactory.make("appsrc")
    source = RosImageAppSrc(appsrc, "/test", 15, "auto")
    stamp = SimpleNamespace(sec=1, nanosec=1)
    msg = SimpleNamespace(width=2, height=2, step=6, encoding="rgb8", data=bytes(12),
                          header=SimpleNamespace(stamp=stamp, frame_id="camera"))
    copied = []
    original = ros_image_io.contiguous_image_bytes
    monkeypatch.setattr(ros_image_io, "contiguous_image_bytes", lambda *args: copied.append(1) or original(*args))
    current = [1.0]
    monkeypatch.setattr(ros_image_io.time, "monotonic", lambda: current[0])
    for index in range(60):
        current[0] = 1 + index / 60
        source._on_image(msg)
    assert 12 <= len(copied) <= 15
    assert source.total_received == 60 and source.total_pushed == len(copied)
    assert source.rate_dropped == 60 - len(copied)
