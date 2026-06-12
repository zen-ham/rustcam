"""grab_gpu() returning a GpuTexture (shared NT handle + keyed mutex)."""
import pytest

import rustcam


@pytest.fixture
def cap():
    c = rustcam.Capturer(output=0, cursor=False)
    yield c
    c.close()


def _try_grab_gpu(cap, tries=20):
    for _ in range(tries):
        try:
            tex = cap.grab_gpu(timeout_ms=300)
        except rustcam.CaptureError:
            continue
        if tex is not None:
            return tex
    return None


def test_grab_gpu_returns_texture(cap):
    tex = _try_grab_gpu(cap)
    if tex is None:
        pytest.skip("grab_gpu() returned None or raised for all attempts (env-dependent)")
    assert tex.width == cap.width
    assert tex.height == cap.height
    assert tex.format == "DXGI_FORMAT_B8G8R8A8_UNORM"
    assert tex.shared_handle > 0
    luid = tex.luid
    assert isinstance(luid, tuple) and len(luid) == 2
    # v0.0.3 ships without an active keyed-mutex; both keys are 0.
    assert tex.keyed_mutex_acquire_key == 0
    assert tex.keyed_mutex_release_key == 0
    tex.close()


def test_close_twice_idempotent(cap):
    tex = _try_grab_gpu(cap)
    if tex is None:
        pytest.skip("no GPU frame within timeout window")
    tex.close()
    tex.close()  # no exception


def test_repr_does_not_crash(cap):
    tex = _try_grab_gpu(cap)
    if tex is None:
        pytest.skip("no GPU frame within timeout window")
    r = repr(tex)
    assert "rustcam.GpuTexture" in r
    tex.close()
