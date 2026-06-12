"""start() / stop() / get_latest_frame() background-capture mode."""
import time

import numpy as np
import pytest

import rustcam


@pytest.fixture
def cap():
    c = rustcam.Capturer(output=0, cursor=False)
    yield c
    try:
        c.stop()
    except Exception:
        pass
    c.close()


def test_start_sets_is_capturing(cap):
    assert cap.is_capturing is False
    cap.start(target_fps=60)
    assert cap.is_capturing is True
    cap.stop()
    assert cap.is_capturing is False


def test_get_latest_frame_returns_shape(cap):
    cap.start(target_fps=60, video_mode=True)
    # Settle: first frame may take a moment
    f = None
    for _ in range(10):
        f = cap.get_latest_frame(timeout_ms=500)
        if f is not None:
            break
    assert f is not None
    assert f.dtype == np.uint8
    assert f.shape == (cap.height, cap.width, 4)


def test_double_start_raises(cap):
    cap.start(target_fps=60)
    with pytest.raises(RuntimeError):
        cap.start(target_fps=30)


def test_stop_is_idempotent(cap):
    cap.start(target_fps=60)
    cap.stop()
    cap.stop()  # no exception


def test_get_latest_frame_without_start_raises(cap):
    with pytest.raises(RuntimeError):
        cap.get_latest_frame()


def test_grab_blocked_while_capturing(cap):
    cap.start(target_fps=60)
    # State is moved into bg thread, so grab() raises.
    with pytest.raises(RuntimeError):
        cap.grab()
    cap.stop()
    # Now grab() works again (state restored from bg).
    f = cap.grab(timeout_ms=1000)
    assert f is not None
