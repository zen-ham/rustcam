"""Shared pytest fixtures for rustcam tests.

The session-scoped `stim_proc` fixture spawns a tkinter window with
continuous canvas redraws (see `_stim_window.py`). This drives DDA without
moving the user's real cursor, which was the old approach (SetCursorPos)
and was visible / disruptive on the user's desktop.
"""
import os
import subprocess
import sys
import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def stim_proc():
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "_stim_window.py")
    p = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)  # let it open + start rendering
    yield p
    try:
        p.terminate()
        p.wait(timeout=2.0)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


@pytest.fixture
def grab_one():
    """Repeatedly grab until a frame comes back, or fail after `tries`.

    The session-scoped tkinter stimulus is redrawing continuously, so
    timeouts under a few hundred ms get plenty of frames.
    """
    def _g(cap, tries=30, **kwargs):
        for _ in range(tries):
            f = cap.grab(timeout_ms=200, **kwargs)
            if f is not None:
                return f
        pytest.fail(f"grab() returned None after {tries} retries (stimulus stopped?)")

    return _g
