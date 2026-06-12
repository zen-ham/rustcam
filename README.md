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

`benches/flip_demo_bench.py` runs each library against two stimuli, on a 1920x1080 / 180 Hz monitor backed by a GTX 1660 Ti:

- **`flip_demo`** is a native Rust D3D11 app from the zentape project. It presents a flip-model swapchain with a unique full-screen colour every refresh, so the source emits ~180 unique fps and any honest capturer should be able to read at panel rate. This is the *controlled* benchmark.
- **`mover.py`** is a borderless PyQt window that orbits across the screen continuously. This is the *realistic* benchmark, what users actually capture, dragging a window around or recording a moving UI.

The metric is **unique frames per second**, measured by md5-hashing a sparse sample of each returned frame and counting distinct hashes. Container fps lies; a library can return the same buffer over and over and look fast. Unique fps cant be faked.

| capturer | flip_demo | mover.py | %changed (mover) |
| --- | --- | --- | --- |
| **rustcam grab(cursor=False)** | **163.2** | **93.2** | **100 %** |
| rustcam grab(cursor=True) | 149.3 | 40.9 | 100 % |
| rustcam start/get_latest_frame | 132.0 | (n/a) | 100 % |
| rustcam grab_gpu (sustained) | ~175 | ~180 | (sentinel hash) |
| bettercam | 162.5 | **0.2** | 0 % |
| dxcam | 156.7 | **0.0** | 0 % |
| mss | 8.0 | 6.0 | 21 % |

On the controlled flip-model source, rustcam, bettercam and dxcam all hit the panel rate (DDA is the bottleneck, not the library). But the moment the stimulus is a window thats just moving across the desktop, **bettercam captures 0.2 unique frames per second and dxcam captures 0**. rustcam still rides the refresh. Thats the whole reason this package exists.

bettercams own header advertises ~135 fps "fastest in the world", and on flip_demo it actually does deliver that, but only because the source is hitting it through a fast DXGI flip path. On real composited content (the case 99% of users care about) its Python per-frame loop misses every refresh and you get one frame every 5 seconds.

The `cursor=True` cell on mover.py is currently 40 fps. The GDI cursor compositing path (`DrawIconEx` on the GDI-compatible BGRA texture) syncs with the GPU every frame, and that sync costs more when the desktop is also recomposing under it. Use `cursor=False` for the fastest hot path; turn cursor on when you actually need it composited.

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

License
---

MIT. See `LICENSE`.
