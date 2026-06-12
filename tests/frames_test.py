"""frames(fps=N) paced CFR iterator."""
import time

import numpy as np
import pytest

import rustcam


@pytest.fixture
def cap():
    c = rustcam.Capturer(output=0, cursor=False)
    yield c
    c.close()


def test_frames_iterator_yields_tuples(cap):
    it = cap.frames(fps=60, timeout_ms=2000)
    n = 0
    for frame, ts in it:
        n += 1
        assert isinstance(frame, np.ndarray)
        assert frame.shape == (cap.height, cap.width, 4)
        assert frame.dtype == np.uint8
        assert isinstance(ts, float)
        if n >= 30:
            it.close()
            break
    assert n == 30


def test_frames_pacing_matches_target(cap):
    fps = 60
    it = cap.frames(fps=fps, timeout_ms=2000)
    timestamps = []
    n = 0
    start = time.perf_counter()
    for _, ts in it:
        n += 1
        timestamps.append(ts)
        if n >= 60:
            it.close()
            break
    elapsed = time.perf_counter() - start
    # 60 frames at 60 fps -> ~1.0s wallclock
    assert 0.9 < elapsed < 1.4

    diffs = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
    avg = sum(diffs) / len(diffs)
    period = 1.0 / fps
    assert abs(avg - period) < 0.001


def test_frames_with_fmt_rgb(cap):
    it = cap.frames(fps=30, fmt="rgb", timeout_ms=2000)
    n = 0
    for frame, _ in it:
        n += 1
        assert frame.shape == (cap.height, cap.width, 3)
        if n >= 5:
            it.close()
            break
    assert n == 5


def test_grab_blocked_while_frames_iterating(cap):
    it = cap.frames(fps=30, timeout_ms=2000)
    next_pair = next(iter(it))
    assert next_pair[0].shape == (cap.height, cap.width, 4)
    with pytest.raises(RuntimeError):
        cap.grab()
    it.close()
    # After close, grab() works again (lazy state recreation).
    f = cap.grab(timeout_ms=1000)
    assert f is not None


def test_frames_context_manager(cap):
    n = 0
    with cap.frames(fps=30, timeout_ms=2000) as it:
        for _, _ in it:
            n += 1
            if n >= 3:
                break
    assert n == 3
    f = cap.grab(timeout_ms=1000)
    assert f is not None


def test_frames_zero_fps_raises(cap):
    with pytest.raises(ValueError):
        cap.frames(fps=0)
