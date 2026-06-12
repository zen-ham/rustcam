"""Module-level surface: imports, list_outputs, device_info, basic Capturer construction."""
import rustcam


def test_module_attributes():
    assert isinstance(rustcam.__version__, str)
    assert rustcam.__version__.count(".") >= 2

    for name in [
        "Capturer",
        "CaptureError", "DeviceError", "DuplicationError",
        "AccessLost", "CaptureTimeout",
        "list_outputs", "device_info", "output_info",
    ]:
        assert hasattr(rustcam, name), f"missing rustcam.{name}"


def test_list_outputs_structure():
    outs = rustcam.list_outputs()
    assert isinstance(outs, list)
    assert len(outs) > 0, "must report at least one output"
    o = outs[0]
    for k in ("device_idx", "output_idx", "name", "output_name",
              "width", "height", "rotation", "is_primary"):
        assert k in o, f"missing key {k} in list_outputs() entry"
    assert isinstance(o["width"], int) and o["width"] > 0
    assert isinstance(o["height"], int) and o["height"] > 0


def test_info_strings():
    d = rustcam.device_info()
    assert isinstance(d, str) and "Device[" in d
    o = rustcam.output_info()
    assert isinstance(o, str) and "Output[" in o


def test_capturer_construction():
    cap = rustcam.Capturer(output=0, cursor=False)
    assert cap.width > 0 and cap.height > 0
    assert cap.output_idx == 0
    assert cap.device_idx == 0
    assert cap.cursor is False
    assert cap.format == "bgra"
    assert cap.region == (0, 0, cap.width, cap.height)
    assert cap.is_capturing is False
    assert "rustcam.Capturer" in repr(cap)
    cap.close()


def test_exception_hierarchy():
    assert issubclass(rustcam.DeviceError, rustcam.CaptureError)
    assert issubclass(rustcam.DuplicationError, rustcam.CaptureError)
    assert issubclass(rustcam.AccessLost, rustcam.CaptureError)
    assert issubclass(rustcam.CaptureTimeout, rustcam.CaptureError)
