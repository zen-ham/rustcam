`rustcam`
===

[![PyPI](https://img.shields.io/pypi/v/rustcam?logo=pypi&color=blue)](https://pypi.org/project/rustcam/) [![Downloads](https://static.pepy.tech/badge/rustcam)](https://pypi.org/project/rustcam/) [![GitHub](https://img.shields.io/badge/GitHub-rustcam-blue?logo=github)](https://github.com/zen-ham/rustcam) [![stars](https://img.shields.io/github/stars/zen-ham/rustcam?style=social)](https://github.com/zen-ham/rustcam)

Fast DXGI Desktop Duplication screen capture for Windows, in Rust.

I made this because every "fast" screen capture package on PyPI runs its hot loop in Python. `bettercam` is a fork of `dxcam`, `dxcam` calls `AcquireNextFrame` through `comtypes` on every frame under the GIL, and the GDI-based ones (`mss`, `PIL.ImageGrab`) aren't even using DDA. They all top out around 130-140 fps on a 180 Hz monitor for the same reason: per-frame Python overhead misses compositor ticks. rustcam runs the whole `AcquireNextFrame` -> `CopyResource` -> `Map` -> memcpy cycle in native Rust with the GIL released, so it actually rides the refresh rate.

```py
import rustcam

cap = rustcam.Capturer(output=0, cursor=True)
frame = cap.grab()        # numpy ndarray (H, W, 4) BGRA, or None on timeout
```

Prebuilt Windows wheels for Python 3.9 through 3.13 (a single abi3 wheel that covers them all). `pip install rustcam` never compiles anything.

Install
---

```
pip install rustcam
```

Windows only. DDA is the `IDXGIOutputDuplication` interface, which is Win8+. There is no Linux or macOS equivalent. If you need cross-platform capture, look at `mss` (slower, GDI-based).

Performance
---

![benchmark](https://raw.githubusercontent.com/zen-ham/rustcam/master/docs/benchmark.png)

`benches/compare_competitors.py` runs each library against the same moving stimulus (a tkinter canvas redrawing every tick) for 6 seconds, on a 1920×1080 / 180 Hz monitor backed by a GTX 1660 Ti. The headline number is **unique frames captured per second**, the only thing that matters for recording or ML pipelines. Calls that just return the previous buffer dont count.

| Capturer | unique fps | valid fps | calls / s | wasted calls |
| --- | --- | --- | --- | --- |
| **rustcam (cursor=True)** | **125.5** | 125.5 | 125.5 | **0 %** |
| rustcam (cursor=False) | 122.1 | 122.1 | 122.1 | 0 % |
| bettercam | 125.8 | 179.0 | 222.0 | 43 % |
| dxcam | 119.6 | 179.8 | 231.9 | 48 % |
| mss | 5.5 | 31.7 | 31.7 | most |

The DDA-based capturers (rustcam, bettercam, dxcam) all saturate the stimulus's ~125 fps unique-content rate, so the unique-fps column ties. But look at calls per second: bettercam and dxcam burn 220+ python-level grab calls per second to get those 125 uniques, ie almost half their work returns a cached duplicate frame. rustcam's call rate exactly equals its unique rate, every call blocks waiting for a fresh frame, so theres no wasted CPU.

On real higher-refresh content (a 180 fps game or a window that moves every refresh), the design notes show rustcam riding the full panel rate (~180 unique fps) while bettercam and friends stall around 130-140 because their Python loop cant keep up. The current benchmark stimulus tops out around 125 fps because tkinters event-loop pacing limits it, so the bigger gap doesn't show up here. A wider-stimulus benchmark using a custom Direct2D/Qt frameless mover is on the todo list.

Why this is faster
---

`libzpaq`... wait wrong project. Same idea though: every existing PyPI screen-cap library does the DDA loop FROM PYTHON. They acquire each frame through `comtypes` proxies, allocate a numpy array per call, do format conversion through `cv2.cvtColor` (bettercam pulls OpenCV in just for that), and hold the GIL the whole time. The native rate the OS can give you (one frame per compositor tick) gets eaten by all of that.

rustcam does the entire `AcquireNextFrame` -> `CopyResource` -> `Map` -> RowPitch-aware memcpy in a single Rust function call, releases the GIL around it, and reuses the same BGRA + staging textures across calls. Format conversion (BGR / RGB / RGBA / grayscale) is a tight scalar Rust loop that LLVM auto-vectorizes, no OpenCV dependency. Theres nothing clever, its just doing the same DXGI calls bettercam does without the per-frame Python overhead.

Additions vs bettercam:
- proper cursor compositing via `IDXGISurface1::GetDC` + `DrawIconEx(DI_NORMAL)`, which handles the inverting I-beam over text correctly (DrawIconEx does mask + XOR blending natively)
- a `region` argument that crops on the way out of the staging-texture map (no extra alloc)
- five output formats (`bgra`/`bgr`/`rgba`/`rgb`/`gray`) with no `cv2` dependency
- a context manager so `with rustcam.Capturer(...) as cap:` releases COM state on exit
- structured exceptions (`AccessLost`, `DeviceError`, `DuplicationError`, `CaptureTimeout`, `CaptureError`) carrying the underlying HRESULT

API
---

```py
import rustcam

cap = rustcam.Capturer(
    output=0,            # IDXGIOutput index, 0 = primary on single-GPU systems
    cursor=True,         # composite the OS cursor into each captured frame
    region=None,         # persistent (l, t, r, b) crop; None = full output
    device=0,            # IDXGIAdapter index, 0 = first adapter
)

# state
cap.width, cap.height            # output resolution
cap.region                       # current persistent region (full if None)
cap.output_idx, cap.device_idx
cap.cursor, cap.format, cap.rotation

# one-shot capture
frame = cap.grab(
    timeout_ms=1000,                  # wait up to this long; 0 = poll
    fmt="bgra",                       # bgra / bgr / rgba / rgb / gray
    region=None,                      # per-call crop, doesn't mutate cap.region
)
# returns numpy ndarray (H, W, C) uint8, or None on DXGI_ERROR_WAIT_TIMEOUT

# context manager
with rustcam.Capturer(output=0) as cap:
    frame = cap.grab()

# module helpers
rustcam.list_outputs()               # list of dicts (one per output across all adapters)
rustcam.device_info()                # bettercam-style multi-line string
rustcam.output_info()                # same
```

Exceptions (all subclasses of `rustcam.CaptureError`):

- `CaptureError` - base; catches every DXGI-origin failure
- `DeviceError` - device removed / reset
- `DuplicationError` - DuplicateOutput failed (often: another process already capturing this output)
- `AccessLost` - exclusive fullscreen took over the display; rustcam retries duplication once internally
- `CaptureTimeout` - reserved for the streaming APIs landing in v0.0.2; `grab()` returns None on timeout

Each carries a `.hresult` attribute with the raw HRESULT when relevant.

What's not here yet
---

v0.0.1 ships the one-shot `grab()` path. The remaining surfaces from the design are stubbed and raise `NotImplementedError`:

- `start()` / `stop()` / `get_latest_frame()` - background-thread capture with a ring buffer (bettercam parity)
- `frames(fps=N)` - paced CFR iterator yielding `(frame, slot_wallclock_ts)` for recording / streaming
- `grab_gpu()` - zero-copy `GpuTexture` returning a shared NT handle + keyed mutex so downstream code (CUDA, Vulkan, custom D3D11) can stay on GPU

These all land in v0.0.2. The design doc for them (CFR pacer architecture, GPU shared-handle protocol, etc) is fully specced; the work is implementation.

Compatibility notes
---

A `Capturer` is bound to the OS thread that created it. Use one per thread. The Rust extension is `#[pyclass(unsendable)]`, so passing a Capturer between threads raises a `RuntimeError`.

The first DDA frame after construction is sometimes black. rustcam discards two warmup frames internally so the first user-visible `grab()` returns real content.

DDA cant see HDCP-protected content (Netflix, Disney+, etc) - that's the DRM working as designed, and you get a black texture. UWP apps with the protected-content flag set behave the same way. There is no way around this without going through different APIs (WGC + ContentDeliveryManager) which are out of scope here.

License
---

MIT. See `LICENSE`.
