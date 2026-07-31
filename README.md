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

![benchmark](https://gitlab.com/zenham/rustcam/-/raw/master/docs/benchmark.png)

`benches/flip_demo_bench.py` runs each library against two stimuli, on a 1920x1080 / 180 Hz monitor backed by a GTX 1660 Ti:

- **`flip_demo`** is a native Rust D3D11 app from the zentape project. It presents a flip-model swapchain with a unique full-screen colour every refresh, so the source emits ~180 unique fps and any honest capturer should be able to read at panel rate. This is the *controlled* benchmark.
- **`mover.py`** is a borderless PyQt window that orbits across the screen continuously. This is the *realistic* benchmark, what users actually capture, dragging a window around or recording a moving UI.

The metric is **valid fps** — how many non-None frames the lib returns per second, with `%changed` annotating how often consecutive returned frames actually differ. Earlier versions of this README compared rustcam's normal API against bettercam's `.grab()` mode, which is a fast-but-unrealistic tight-loop pattern nobody actually codes against. The numbers below use **bettercam.start()/.get_latest_frame()** and **dxcam.start()/.get_latest_frame()** — the only modes either library is actually used in.

The harness for these numbers is `benches/controlled_bench.py`. It pops a status window on the top monitor, gives you 3 seconds to finish whatever you're doing, minimizes all your visible windows (it remembers each HWND so it can restore them after, no Win+D guesswork), runs each capturer in a subprocess so DDA state doesn't leak between trials, takes the median of 3 runs per cell, and restores your windows at the end. Run it yourself, the numbers should reproduce within a few percent.

| capturer | flip_demo (valid fps) | mover.py (valid fps) | mover.py (% changed) |
| --- | --- | --- | --- |
| **rustcam grab(cursor=False)** | **180+** | **179** | **100 %** |
| rustcam grab(cursor=True) | **180+** | **179** | 100 % |
| rustcam start/get_latest_frame | 180+ | 179 | 100 % |
| bettercam `.start()/.get_latest_frame()` | 157 | 146 | 100 % |
| dxcam `.start()/.get_latest_frame()` | 129 | 125 | 100 % |
| mss | 44 | 44 | 100 % |

Medians of 3 trials per cell from the controlled-bench harness; error bars on rustcam are well under 1 fps in every cell, often under 0.1 fps. The chart's y-axis is capped at the 180 Hz monitor refresh and annotates a `+` suffix on any bar where DDA delivered measurably more than that. That's the panel ceiling honored visually, not a software limit. flip_demo presents without strict vsync, so DXGI Desktop Duplication can hand us frames slightly faster than the panel can display them; the panel still only shows 180 frames/sec, but rustcam pulls every one DDA hands over (currently ~196 fps on flip_demo) so the comparison against the other capturers only makes sense capped to refresh rate. When `+` doesn't appear, the capturer settled exactly at the panel ceiling.

On both stimuli `grab()` saturates the panel refresh. Three changes across v0.0.7-v0.0.8 made that happen:

- The user-facing numpy array gets allocated **uninitialized** on the Rust side (`numpy::PyArray::new`) and the staging texture memcpys directly into its storage. Skips the 8 MB `vec![0u8; ...]` zero-init that used to cost 0.5 ms per call before the memcpy overwrote every byte anyway.
- `start(target_fps=N)` and `frames(fps=N)` use a Win32 `CreateWaitableTimerExW(HIGH_RESOLUTION)` for sleep instead of `std::thread::sleep`. The default thread-sleep on Windows inherits a ~15 ms timer granularity that's why every Python screen-capture lib's bg-thread mode used to stall ~58 fps when you asked for 60. The high-res waitable timer fixes that without bumping the global timer resolution.
- `cursor=True` now resamples the cursor position only when DDA reports a NEW desktop present (`LastPresentTime` advances), not on every pointer-only DDA wake-up. Without this, a 1 kHz polling mouse jitters the cached cursor position between ~5 ms `AcquireNextFrame` calls on the *same* desktop frame, the compositor honestly redraws at the new pixel, and a downstream hash dedup sees phantom unique frames above the panel rate. Real cursor motion across actual present boundaries still registers at panel granularity, which is the most the panel can ever display anyway. Cursor shape updates (`PointerShapeBufferSize > 0`) still apply on pointer-only wake-ups via a shape-only refresh path that doesn't touch position, so animated cursors and shape-changes-mid-app still render correctly. The result is cursor=True at panel rate without the 200+ "fake unique" inflation v0.0.7 had.

bettercam in `.start()` mode tops out around 149-156 (the same lib that says "world's fastest", the showy `.grab()` mode hits 180 because it bypasses its own bg thread). dxcam around 128-129. mss at 44, the GDI path can't keep up with DDA-based capture on a high-refresh monitor.

Why this is faster
---

Every existing PyPI screen-cap library does the DDA loop FROM PYTHON. They acquire each frame through `comtypes` proxies, allocate a numpy array per call, do format conversion through `cv2.cvtColor` (bettercam pulls OpenCV in just for that), and hold the GIL the whole time. The native rate the OS can give you (one frame per compositor tick) gets eaten by all of that.

rustcam does the entire `AcquireNextFrame` -> `CopyResource` -> `Map` -> RowPitch-aware memcpy in a single Rust function call, releases the GIL around it, and reuses the same BGRA + staging textures across calls. Format conversion (BGR / RGB / RGBA / grayscale) is a tight scalar Rust loop that LLVM auto-vectorizes, no OpenCV dependency. Theres nothing clever, its just doing the same DXGI calls bettercam does without the per-frame Python overhead.

Additions vs bettercam:
- proper cursor compositing via `IDXGISurface1::GetDC` + `DrawIconEx(DI_NORMAL)`, which handles the inverting I-beam over text correctly (DrawIconEx does mask + XOR blending natively)
- a `region` argument that crops on the way out of the staging-texture map (no extra alloc)
- five output formats (`bgra`/`bgr`/`rgba`/`rgb`/`gray`) with no `cv2` dependency
- a paced CFR `frames(fps=N)` iterator that yields `(ndarray, slot_wallclock_ts)` for video recording, slot-clock pacing in native Rust, no Python-side timer drift
- a `start()/stop()/get_latest_frame()` background-thread mode (bettercam-parity API), Rust capture loop, mailbox, blocking pull
- `grab_gpu()` returns a `GpuTexture` wrapping a shared NT handle around a BGRA D3D11 texture, zero CPU readback, downstream code (CUDA, Vulkan, custom D3D11) can open the handle on its own device
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
cap.is_capturing                 # True between start() and stop(), or during frames() iteration

# one-shot capture
frame = cap.grab(
    timeout_ms=1000,                  # wait up to this long; 0 = poll
    fmt="bgra",                       # bgra / bgr / rgba / rgb / gray
    region=None,                      # per-call crop, doesn't mutate cap.region
)
# returns numpy ndarray (H, W, C) uint8, or None on DXGI_ERROR_WAIT_TIMEOUT

# background capture (bettercam-parity)
cap.start(target_fps=60, video_mode=True)
frame = cap.get_latest_frame(timeout_ms=500)   # blocks until new frame; raises CaptureTimeout on deadline
cap.stop()

# paced CFR stream — yields (ndarray, slot_wallclock_seconds), exact 1/fps spacing
for frame, ts in cap.frames(fps=60, fmt="bgr"):
    encoder.write(frame, pts=ts)
    if ts > 10.0:
        break

# zero-copy GPU handle (consumer opens with OpenSharedResource1 on its own D3D11 device)
tex = cap.grab_gpu(timeout_ms=200)
if tex is not None:
    print(tex.width, tex.height, hex(tex.shared_handle), tex.luid)
    tex.close()    # release the duplicated NT handle

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
- `CaptureTimeout` - raised by the streaming APIs (`get_latest_frame`, `frames`) when their deadline expires; `grab()` returns None on timeout instead

Each carries a `.hresult` attribute with the raw HRESULT when relevant.

Compatibility notes
---

A `Capturer` is bound to the OS thread that created it. Use one per thread. The Rust extension is `#[pyclass(unsendable)]`, so passing a Capturer between threads raises a `RuntimeError`.

`grab()`, `start()`, `frames()`, and `grab_gpu()` are mutually exclusive while a long-running operation is active. While the Capturer is in background mode (`start()`) or iterating frames, calling `grab()` or `grab_gpu()` raises `RuntimeError`. Call `stop()` (or close the `frames()` iterator) first.

The first DDA frame after construction is sometimes black. rustcam discards two warmup frames internally so the first user-visible `grab()` returns real content.

DDA cant see HDCP-protected content (Netflix, Disney+, etc) - that's the DRM working as designed, and you get a black texture. UWP apps with the protected-content flag set behave the same way. There is no way around this without going through different APIs (WGC + ContentDeliveryManager) which are out of scope here.

`grab_gpu()` returns a shared NT handle. The consumer opens it on its own D3D11 device via `OpenSharedResource1`. v0.0.3 ships without a strict keyed-mutex protocol on the shared texture; the consumer should copy the texture into its own resource before issuing GPU work that depends on the content. A future release will add an opt-in strict mode.

Future work
---

- Optional strict keyed-mutex mode for `grab_gpu()` so a single-writer single-reader pipeline can rely on producer/consumer ordering.
- WGC (Windows.Graphics.Capture) backend as a fallback for per-window capture and HDR sources where DDA can't help.
- 10-bit / HDR backbuffer support.
- ARM64 Windows wheels.

Shared `dda_capture` crate
---

The DDA-specific bits (cursor compositor, region/crop, error type) live in a shared Rust crate at [github.com/zen-ham/dda_capture](https://github.com/zen-ham/dda_capture) so this package and [zentape](https://github.com/zen-ham/zentape) (a native NV12 video encoder that uses the same DDA capture path) can share one implementation. The cursor=True fix in particular was the kind of subtle bug nobody wants to debug twice — having it in one place means a fix to rustcam ports straight to zentape and vice versa. The crate is a normal cargo git dep, no path tricks needed.

License
---

MIT. See `LICENSE`.
