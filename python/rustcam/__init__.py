"""rustcam — Rust-backed Windows DXGI Desktop Duplication API screen capture."""

from rustcam._rustcam import (
    Capturer,
    CaptureError,
    DeviceError,
    DuplicationError,
    AccessLost,
    CaptureTimeout,
    list_outputs,
    device_info,
    output_info,
    __version__,
)

__all__ = [
    "Capturer",
    "CaptureError",
    "DeviceError",
    "DuplicationError",
    "AccessLost",
    "CaptureTimeout",
    "list_outputs",
    "device_info",
    "output_info",
    "__version__",
]
