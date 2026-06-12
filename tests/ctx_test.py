"""Context manager + close() semantics."""
import pytest

import rustcam


def test_context_manager_runs():
    with rustcam.Capturer(output=0, cursor=False) as cap:
        assert cap.width > 0
    # After __exit__, access raises
    with pytest.raises(RuntimeError):
        cap.width  # noqa: B018


def test_close_then_grab_raises():
    cap = rustcam.Capturer(output=0, cursor=False)
    cap.close()
    with pytest.raises(RuntimeError):
        cap.grab()


def test_close_is_idempotent():
    cap = rustcam.Capturer(output=0, cursor=False)
    cap.close()
    cap.close()  # no exception
