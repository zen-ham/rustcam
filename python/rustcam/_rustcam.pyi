from __future__ import annotations
from typing import Iterator
import numpy as np

__version__: str

class CaptureError(Exception):
    hresult: int | None
class DeviceError(CaptureError): ...
class DuplicationError(CaptureError): ...
class AccessLost(CaptureError): ...
class CaptureTimeout(CaptureError): ...

class Capturer:
    width: int
    height: int
    output_idx: int
    device_idx: int
    cursor: bool
    rotation: int
    region: tuple[int, int, int, int]
    format: str
    is_capturing: bool

    def __init__(
        self,
        output: int = 0,
        *,
        cursor: bool = True,
        region: tuple[int, int, int, int] | None = None,
        device: int = 0,
    ) -> None: ...

    def grab(
        self,
        timeout_ms: int = 1000,
        fmt: str = "bgra",
        region: tuple[int, int, int, int] | None = None,
    ) -> np.ndarray | None: ...

    def start(
        self,
        target_fps: int = 60,
        region: tuple[int, int, int, int] | None = None,
        video_mode: bool = False,
    ) -> None: ...

    def stop(self) -> None: ...
    def get_latest_frame(self, timeout_ms: int | None = None) -> np.ndarray: ...

    def frames(
        self,
        fps: int,
        fmt: str = "bgra",
        region: tuple[int, int, int, int] | None = None,
        timeout_ms: int = 5000,
    ) -> Iterator[tuple[np.ndarray, float]]: ...

    def grab_gpu(self, timeout_ms: int = 1000) -> object | None: ...

    def close(self) -> None: ...
    def __enter__(self) -> "Capturer": ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool: ...

def list_outputs() -> list[dict]: ...
def device_info() -> str: ...
def output_info() -> str: ...
