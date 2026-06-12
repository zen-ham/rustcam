"""Stub methods should raise NotImplementedError until they land in v0.0.2+."""
import pytest

import rustcam


@pytest.fixture
def cap():
    c = rustcam.Capturer(output=0, cursor=False)
    yield c
    c.close()


def test_start_raises_not_implemented(cap):
    with pytest.raises(NotImplementedError):
        cap.start()


def test_stop_raises_not_implemented(cap):
    with pytest.raises(NotImplementedError):
        cap.stop()


def test_get_latest_frame_raises_not_implemented(cap):
    with pytest.raises(NotImplementedError):
        cap.get_latest_frame()


def test_frames_raises_not_implemented(cap):
    with pytest.raises(NotImplementedError):
        cap.frames(fps=60)


def test_grab_gpu_raises_not_implemented(cap):
    with pytest.raises(NotImplementedError):
        cap.grab_gpu()
