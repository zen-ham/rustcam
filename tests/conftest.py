"""Shared pytest fixtures for rustcam tests.

Tests that need an actual DDA frame use the `moving_cursor` fixture, which
jiggles the OS cursor between calls to satisfy DDA's "wait for content
change" contract.
"""
import ctypes
import time

import pytest


@pytest.fixture
def jiggle():
    """Returns a callable that nudges the OS cursor to a fresh screen position."""
    user32 = ctypes.windll.user32
    state = {"i": 0}

    def _jiggle():
        state["i"] += 7
        user32.SetCursorPos(400 + (state["i"] % 600), 300 + (state["i"] % 400))
        time.sleep(0.001)

    return _jiggle


@pytest.fixture
def grab_one(jiggle):
    """Repeatedly jiggles + grabs until a frame is returned, or fails after `tries`."""

    def _g(cap, tries=30, **kwargs):
        for _ in range(tries):
            jiggle()
            f = cap.grab(timeout_ms=200, **kwargs)
            if f is not None:
                return f
        pytest.fail(f"grab() returned None after {tries} retries with cursor jitter")

    return _g
