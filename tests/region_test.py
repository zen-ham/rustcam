"""Region: persistent + per-call, validation, edge cases."""
import pytest

import rustcam


def test_persistent_region():
    cap = rustcam.Capturer(output=0, cursor=False, region=(100, 100, 500, 400))
    assert cap.region == (100, 100, 500, 400)
    cap.close()


def test_per_call_region_does_not_mutate(grab_one):
    cap = rustcam.Capturer(output=0, cursor=False)
    full = cap.region
    f = grab_one(cap, region=(0, 0, 200, 150))
    assert f.shape == (150, 200, 4)
    assert cap.region == full, "per-call region must not mutate cap.region"
    cap.close()


@pytest.mark.parametrize(
    "bad",
    [
        (0, 0, 0, 100),       # zero-width
        (0, 0, 100, 0),       # zero-height
        (100, 0, 50, 100),    # right < left
        (-1, 0, 100, 100),    # negative
        (0, 0, 100000, 100),  # exceeds output width
    ],
)
def test_invalid_regions_raise(bad):
    with pytest.raises(ValueError):
        rustcam.Capturer(output=0, cursor=False, region=bad)


def test_invalid_per_call_region_raises():
    cap = rustcam.Capturer(output=0, cursor=False)
    with pytest.raises(ValueError):
        cap.grab(region=(0, 0, 0, 100))
    cap.close()
