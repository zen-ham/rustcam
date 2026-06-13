//! Core DDA capture path: Capturer pyclass + module-level enumeration helpers.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use numpy::{IntoPyArray, PyArray3, PyArrayMethods};
use once_cell::sync::OnceCell;
use parking_lot::Mutex;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use windows::core::Interface;
use windows::Win32::Foundation::HMODULE;
use windows::Win32::Graphics::Direct3D::{D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL_11_0};
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
    D3D11_BIND_RENDER_TARGET, D3D11_BIND_SHADER_RESOURCE, D3D11_CPU_ACCESS_READ,
    D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAPPED_SUBRESOURCE, D3D11_MAP_READ,
    D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC,
    D3D11_USAGE_DEFAULT, D3D11_USAGE_STAGING,
};
use windows::Win32::Graphics::Dxgi::Common::{
    DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC,
};
use windows::Win32::Graphics::Dxgi::{
    IDXGIAdapter1, IDXGIDevice, IDXGIFactory1, IDXGIOutput, IDXGIOutput1,
    IDXGIOutputDuplication, IDXGIResource, CreateDXGIFactory1, DXGI_ERROR_WAIT_TIMEOUT,
    DXGI_OUTDUPL_FRAME_INFO, DXGI_OUTPUT_DESC,
};
use windows::Win32::UI::HiDpi::{
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};

use crate::convert::{bgra_to, Fmt};
use crate::cursor::{composite_cursor_into_bgra, CursorState};
use crate::errors::{map_dxgi, RustcamError};
use crate::gpu::{GpuProducerState, GpuTexture};
use crate::pacing::{self, BackgroundHandle, FramesIter, StartOpts};
use crate::region::{crop_copy_bgra, Region};
use std::time::Duration;

type CapResult<T> = std::result::Result<T, RustcamError>;

static DPI_INIT: OnceCell<()> = OnceCell::new();

fn ensure_dpi() {
    DPI_INIT.get_or_init(|| unsafe {
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    });
}

/// Encapsulates one D3D11 device + duplication + the two textures we need for CPU readback.
///
/// Not `Send` — all D3D11 COM access must stay on the creating thread.
pub struct CaptureState {
    pub device: ID3D11Device,
    pub context: ID3D11DeviceContext,
    pub output1: IDXGIOutput1,
    pub dup: IDXGIOutputDuplication,
    /// BGRA capture target (DDA copies into this; cursor draws into this when enabled).
    pub capture_tex: ID3D11Texture2D,
    /// BGRA CPU-readable staging copy.
    pub staging_tex: ID3D11Texture2D,
    pub width: u32,
    pub height: u32,
    pub rotation: u32,
    pub cursor: bool,
    pub cursor_state: CursorState,
    pub adapter_luid: (u32, i32),
    /// Lazy GPU-shared producer state for `grab_gpu()`.
    pub gpu: Option<GpuProducerState>,
    /// Reusable BGRA scratch buffer for non-BGRA grab paths (format
    /// conversion / cursor composite work in this buffer before the final
    /// convert step writes into the user-facing numpy array). Sized to
    /// the full output at construction; not reallocated per call.
    pub scratch_bgra: Vec<u8>,
}

impl CaptureState {
    pub fn new(device_idx: u32, output_idx: u32, cursor: bool) -> CapResult<Self> {
        unsafe {
            ensure_dpi();

            let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
            let adapter: IDXGIAdapter1 = factory
                .EnumAdapters1(device_idx)
                .map_err(|_| RustcamError::Value(format!("device index {} out of range", device_idx)))?;

            let adapter_desc = adapter.GetDesc1()?;
            let adapter_luid = (
                adapter_desc.AdapterLuid.LowPart,
                adapter_desc.AdapterLuid.HighPart,
            );

            let mut device: Option<ID3D11Device> = None;
            let mut context: Option<ID3D11DeviceContext> = None;
            let feature_levels = [D3D_FEATURE_LEVEL_11_0];
            D3D11CreateDevice(
                &adapter,
                windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_UNKNOWN,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                Some(&feature_levels),
                D3D11_SDK_VERSION,
                Some(&mut device),
                None,
                Some(&mut context),
            )
            .map_err(|e| {
                RustcamError::from(e)
            })?;
            let device = device.ok_or_else(|| {
                RustcamError::Value("D3D11CreateDevice returned no device".into())
            })?;
            let context = context.ok_or_else(|| {
                RustcamError::Value("D3D11CreateDevice returned no context".into())
            })?;

            let output: IDXGIOutput = adapter.EnumOutputs(output_idx).map_err(|e| {
                let h = e.code().0 as u32;
                RustcamError::Value(format!(
                    "output index {} out of range (HRESULT 0x{:08X})",
                    output_idx, h
                ))
            })?;
            let output_desc: DXGI_OUTPUT_DESC = output.GetDesc()?;
            let output1: IDXGIOutput1 = output.cast()?;

            let dup: IDXGIOutputDuplication = output1
                .DuplicateOutput(&device)
                .map_err(RustcamError::Dxgi)?;

            let dup_desc = dup.GetDesc();
            let width = dup_desc.ModeDesc.Width;
            let height = dup_desc.ModeDesc.Height;
            let rotation = dup_desc.Rotation.0 as u32;

            let mut capture_tex: Option<ID3D11Texture2D> = None;
            // No MISC_GDI_COMPATIBLE: we no longer go through GDI's GetDC for
            // cursor compositing. Cursor is composited in software on the
            // mapped staging buffer in `grab()` (see cursor.rs).
            let capture_desc = D3D11_TEXTURE2D_DESC {
                Width: width,
                Height: height,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            device.CreateTexture2D(&capture_desc, None, Some(&mut capture_tex))?;
            let capture_tex = capture_tex.unwrap();

            let staging_desc = D3D11_TEXTURE2D_DESC {
                Usage: D3D11_USAGE_STAGING,
                BindFlags: 0,
                CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
                MiscFlags: 0,
                ..capture_desc
            };
            let mut staging_tex: Option<ID3D11Texture2D> = None;
            device.CreateTexture2D(&staging_desc, None, Some(&mut staging_tex))?;
            let staging_tex = staging_tex.unwrap();

            let mut state = CaptureState {
                device,
                context,
                output1,
                dup,
                capture_tex,
                staging_tex,
                width,
                height,
                rotation,
                cursor,
                cursor_state: CursorState::default(),
                adapter_luid,
                gpu: None,
                scratch_bgra: vec![0u8; (width as usize) * (height as usize) * 4],
            };

            // Discard the first DDA frame (often black on warm-up).
            for _ in 0..2 {
                let _ = state.try_acquire_into_capture(50);
            }

            let _ = output_desc; // silence unused warning
            Ok(state)
        }
    }

    /// Acquire one frame from DDA into `capture_tex`. Returns:
    ///   Ok(true)  -> a frame was acquired and (optionally) cursor was drawn
    ///   Ok(false) -> WAIT_TIMEOUT (no new frame)
    ///   Err(...)  -> DXGI error (ACCESS_LOST handled internally with one retry)
    pub unsafe fn try_acquire_into_capture(&mut self, timeout_ms: u32) -> CapResult<bool> {
        let mut fi = DXGI_OUTDUPL_FRAME_INFO::default();
        let mut res: Option<IDXGIResource> = None;
        match self.dup.AcquireNextFrame(timeout_ms, &mut fi, &mut res) {
            Ok(_) => {
                if let Some(res) = res.as_ref() {
                    if let Ok(frame_tex) = res.cast::<ID3D11Texture2D>() {
                        self.context.CopyResource(&self.capture_tex, &frame_tex);
                    }
                }
                // Pull cursor position + shape from DDA before releasing the
                // frame. DDA only fills these when LastMouseUpdateTime != 0
                // (position) or PointerShapeBufferSize > 0 (shape change), so
                // the cached state survives across frames where nothing
                // changed.
                if self.cursor {
                    let _ = self.cursor_state.update_from_frame(
                        &self.dup,
                        fi.LastMouseUpdateTime,
                        &fi.PointerPosition,
                        fi.PointerShapeBufferSize,
                    );
                }
                let _ = self.dup.ReleaseFrame();
                Ok(true)
            }
            Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => Ok(false),
            Err(e) => {
                // Try to recreate duplication once (handles ACCESS_LOST from mode change /
                // brief exclusive-FS takeover). If recreation also fails, surface the error.
                match self.output1.DuplicateOutput(&self.device) {
                    Ok(new_dup) => {
                        self.dup = new_dup;
                        Ok(false)
                    }
                    Err(_) => Err(RustcamError::Dxgi(e)),
                }
            }
        }
    }

    /// CopyResource(staging, capture) then return mapped subresource for the caller to
    /// memcpy out of. Caller MUST call `context.Unmap` when done with the returned pointer.
    pub unsafe fn map_staging(&self) -> CapResult<D3D11_MAPPED_SUBRESOURCE> {
        self.context.CopyResource(&self.staging_tex, &self.capture_tex);
        let mut m = D3D11_MAPPED_SUBRESOURCE::default();
        self.context
            .Map(&self.staging_tex, 0, D3D11_MAP_READ, 0, Some(&mut m))?;
        Ok(m)
    }

    pub unsafe fn unmap_staging(&self) {
        self.context.Unmap(&self.staging_tex, 0);
    }
}

/// Capturer is the user-facing pyclass. Holds the live D3D11 state and orchestrates grabs.
#[pyclass(unsendable, module = "rustcam._rustcam")]
pub struct Capturer {
    state: Option<CaptureState>,
    region: Region,
    device_idx: u32,
    output_idx: u32,
    /// Cached info available even when state has moved into a bg thread.
    cached_width: u32,
    cached_height: u32,
    cached_rotation: u32,
    cached_cursor: bool,
    /// Set on close(); any further method call raises RuntimeError.
    closed: bool,
    /// Set while a frames() iterator is alive; blocks grab/start/grab_gpu.
    pub(crate) busy: Arc<AtomicBool>,
    /// Background-capture handle for start()/stop() mode.
    bg: Option<BackgroundHandle>,
}

#[pymethods]
impl Capturer {
    #[new]
    #[pyo3(signature = (output=0, *, cursor=true, region=None, device=0))]
    fn new(
        output: u32,
        cursor: bool,
        region: Option<(i64, i64, i64, i64)>,
        device: u32,
    ) -> PyResult<Self> {
        let state = CaptureState::new(device, output, cursor)?;
        let full = Region::full(state.width, state.height);
        let region = match region {
            Some(t) => Region::from_tuple(t, state.width, state.height)
                .map_err(crate::errors::RustcamError::from)?,
            None => full,
        };
        let cached_width = state.width;
        let cached_height = state.height;
        let cached_rotation = state.rotation;
        let cached_cursor = state.cursor;
        Ok(Self {
            state: Some(state),
            region,
            device_idx: device,
            output_idx: output,
            cached_width,
            cached_height,
            cached_rotation,
            cached_cursor,
            closed: false,
            busy: Arc::new(AtomicBool::new(false)),
            bg: None,
        })
    }

    // --- read-only state (cached so they remain readable while bg thread owns the state) ---
    #[getter]
    fn width(&self) -> PyResult<u32> {
        self.check_open()?;
        Ok(self.cached_width)
    }
    #[getter]
    fn height(&self) -> PyResult<u32> {
        self.check_open()?;
        Ok(self.cached_height)
    }
    #[getter]
    fn output_idx(&self) -> u32 {
        self.output_idx
    }
    #[getter]
    fn device_idx(&self) -> u32 {
        self.device_idx
    }
    #[getter]
    fn cursor(&self) -> PyResult<bool> {
        self.check_open()?;
        Ok(self.cached_cursor)
    }
    #[getter]
    fn rotation(&self) -> PyResult<u32> {
        self.check_open()?;
        Ok(self.cached_rotation)
    }
    #[getter]
    fn region(&self) -> (u32, u32, u32, u32) {
        self.region.as_tuple()
    }
    #[getter]
    fn format(&self) -> &'static str {
        "bgra"
    }
    #[getter]
    fn is_capturing(&self) -> bool {
        self.bg.is_some() || self.busy.load(Ordering::Acquire)
    }

    /// DEBUG: read the capture thread's current published seq counter.
    /// Used to differentiate "capture thread is at rate X" vs "consumer is
    /// the bottleneck at rate Y" when diagnosing pacing issues. Only
    /// meaningful while a background capture (`start()` or `frames()`) is
    /// running; returns 0 otherwise.
    fn _debug_publish_seq(&self) -> u64 {
        if let Some(bg) = self.bg.as_ref() {
            let slot = bg.mailbox.buf.lock();
            return slot.as_ref().map(|(f, _)| f.seq).unwrap_or(0);
        }
        0
    }

    /// DEBUG: read the current cursor state (visibility, position, shape kind).
    /// Returns a dict so we can inspect what DDA has told us so far.
    fn _debug_cursor<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        use pyo3::types::PyDict;
        let d = PyDict::new(py);
        if let Some(s) = self.state.as_ref() {
            d.set_item("visible", s.cursor_state.visible)?;
            d.set_item("pos_x", s.cursor_state.pos_x)?;
            d.set_item("pos_y", s.cursor_state.pos_y)?;
            match &s.cursor_state.shape {
                Some(shape) => {
                    d.set_item("shape_kind", shape.kind)?;
                    d.set_item("shape_w", shape.width)?;
                    d.set_item("shape_h", shape.height)?;
                    d.set_item("shape_pitch", shape.pitch)?;
                    d.set_item("hotspot_x", shape.hotspot_x)?;
                    d.set_item("hotspot_y", shape.hotspot_y)?;
                    d.set_item("buf_len", shape.pixels.len())?;
                }
                None => {
                    d.set_item("shape", py.None())?;
                }
            }
        }
        Ok(d.into_any())
    }

    /// One-shot capture. Returns a (H, W, C) uint8 ndarray, or None on WAIT_TIMEOUT.
    #[pyo3(signature = (timeout_ms=1000, fmt="bgra", region=None))]
    fn grab<'py>(
        &mut self,
        py: Python<'py>,
        timeout_ms: u32,
        fmt: &str,
        region: Option<(i64, i64, i64, i64)>,
    ) -> PyResult<Option<Bound<'py, PyArray3<u8>>>> {
        self.check_not_busy()?;
        let fmt = Fmt::parse(fmt).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "unknown fmt {:?}, expected one of bgra/bgr/rgba/rgb/gray",
                fmt
            ))
        })?;
        let state = self.state_mut()?;
        let region = match region {
            Some(t) => Region::from_tuple(t, state.width, state.height)
                .map_err(crate::errors::RustcamError::from)?,
            None => self.region,
        };

        // Create the user-facing numpy array as UNINITIALIZED on the GIL
        // side. We'll memcpy from the staging texture directly into its
        // storage during the GIL-released window. Avoids the 8 MB zero-
        // init that `vec![0u8; ...]` did per call (~0.5 ms wasted at
        // 1080p) before the memcpy overwrote every byte anyway.
        let w = region.width() as usize;
        let h = region.height() as usize;
        let channels = fmt.channels();
        let arr: Bound<'py, PyArray3<u8>> =
            unsafe { numpy::PyArray::<u8, _>::new(py, [h, w, channels], false) };

        // Raw pointer into numpy's storage. The pointer is valid for the
        // lifetime of `arr`, which extends past the py.detach window.
        // pyo3's Ungil bound rejects closures that capture `*mut T` even
        // when wrapped in a Send-marked type, so we pass the address as
        // `usize` and cast back inside the closure body.
        let dst_ptr_addr = unsafe { arr.data() } as usize;
        let dst_len = h * w * channels;

        let got = py.detach(move || -> CapResult<bool> {
            let dst_ptr = dst_ptr_addr as *mut u8;
            unsafe {
                let s = self.state_unchecked_mut();
                let got = s.try_acquire_into_capture(timeout_ms)?;
                if !got {
                    return Ok(false);
                }
                let m = s.map_staging()?;
                let dst_slice = std::slice::from_raw_parts_mut(dst_ptr, dst_len);

                match fmt {
                    Fmt::Bgra => {
                        // Direct staging -> numpy memcpy.
                        crop_copy_bgra(
                            dst_slice,
                            m.pData as *const u8,
                            m.RowPitch as usize,
                            region,
                        );
                        s.unmap_staging();
                        if s.cursor {
                            composite_cursor_into_bgra(
                                dst_slice,
                                region.width(),
                                region.height(),
                                region.left as i32,
                                region.top as i32,
                                &s.cursor_state,
                            );
                        }
                    }
                    _ => {
                        // Crop staging into the reusable scratch BGRA buffer,
                        // composite the cursor into it, then format-convert
                        // into the user-facing numpy array.
                        let needed = w * h * 4;
                        if s.scratch_bgra.len() < needed {
                            s.scratch_bgra.resize(needed, 0);
                        }
                        crop_copy_bgra(
                            &mut s.scratch_bgra[..needed],
                            m.pData as *const u8,
                            m.RowPitch as usize,
                            region,
                        );
                        s.unmap_staging();
                        if s.cursor {
                            composite_cursor_into_bgra(
                                &mut s.scratch_bgra[..needed],
                                region.width(),
                                region.height(),
                                region.left as i32,
                                region.top as i32,
                                &s.cursor_state,
                            );
                        }
                        bgra_to(fmt, &s.scratch_bgra[..needed], dst_slice, w, h);
                    }
                }
                Ok(true)
            }
        })?;

        if !got {
            return Ok(None);
        }
        Ok(Some(arr))
    }

    /// Spawn a background capture thread fed by AcquireNextFrame.
    /// Subsequent calls to get_latest_frame() block until a new frame arrives.
    #[pyo3(signature = (target_fps=60, region=None, video_mode=false))]
    fn start(
        &mut self,
        target_fps: u32,
        region: Option<(i64, i64, i64, i64)>,
        video_mode: bool,
    ) -> PyResult<()> {
        if self.bg.is_some() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is already capturing; call stop() first",
            ));
        }
        if self.busy.load(Ordering::Acquire) {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is busy: a frames() iterator is active",
            ));
        }
        let state = self
            .state
            .take()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed"))?;
        let region = match region {
            Some(t) => match Region::from_tuple(t, state.width, state.height) {
                Ok(r) => r,
                Err(e) => {
                    // restore state on validation failure
                    self.state = Some(state);
                    return Err(crate::errors::RustcamError::from(e).into());
                }
            },
            None => self.region,
        };
        let opts = StartOpts { target_fps, region, video_mode };
        self.bg = Some(pacing::spawn(state, opts));
        Ok(())
    }

    fn stop(&mut self) -> PyResult<()> {
        if let Some(bg) = self.bg.take() {
            let state = bg.stop();
            self.state = Some(state);
        }
        Ok(())
    }

    /// Blocking read of the latest published frame. Always BGRA in v0.0.3.
    #[pyo3(signature = (timeout_ms=None))]
    fn get_latest_frame<'py>(
        &mut self,
        py: Python<'py>,
        timeout_ms: Option<u32>,
    ) -> PyResult<Option<Bound<'py, PyArray3<u8>>>> {
        let bg = self.bg.as_ref().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Capturer is not capturing; call start() first")
        })?;
        let mb = bg.mailbox.clone();
        let to = timeout_ms.map(|ms| Duration::from_millis(ms as u64));
        let h = bg.height as usize;
        let w = bg.width as usize;

        let took = py.detach(|| pacing::get_latest(&mb, to))?;
        let buf = match took {
            Some((_, b)) => pacing::arc_into_vec(b),
            None => return Ok(None),
        };
        let arr = buf.into_pyarray(py);
        Ok(Some(arr.reshape([h, w, 4])?))
    }

    /// Paced CFR iterator yielding (ndarray, slot_wallclock_seconds).
    #[pyo3(signature = (fps, fmt="bgra", region=None, timeout_ms=5000))]
    fn frames(
        &mut self,
        fps: u32,
        fmt: &str,
        region: Option<(i64, i64, i64, i64)>,
        timeout_ms: u32,
    ) -> PyResult<FramesIter> {
        if fps == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "frames(fps=) must be > 0",
            ));
        }
        if self.bg.is_some() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is in background mode; call stop() first",
            ));
        }
        if self.busy.load(Ordering::Acquire) {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is busy: a frames() iterator is already active",
            ));
        }
        let fmt = Fmt::parse(fmt).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "unknown fmt {:?}, expected one of bgra/bgr/rgba/rgb/gray",
                fmt
            ))
        })?;
        let state = self
            .state
            .take()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed"))?;
        let region = match region {
            Some(t) => match Region::from_tuple(t, state.width, state.height) {
                Ok(r) => r,
                Err(e) => {
                    self.state = Some(state);
                    return Err(crate::errors::RustcamError::from(e).into());
                }
            },
            None => self.region,
        };
        // Capture-thread runs unthrottled (target_fps=0) — the emit side does the pacing.
        let opts = StartOpts { target_fps: 0, region, video_mode: false };
        let handle = pacing::spawn(state, opts);
        self.busy.store(true, Ordering::Release);
        Ok(FramesIter::new(
            handle,
            self.busy.clone(),
            fps,
            fmt,
            region,
            Duration::from_millis(timeout_ms as u64),
        ))
    }

    /// Zero-copy GPU mode. Returns a `GpuTexture` opaque handle, or None on timeout.
    #[pyo3(signature = (timeout_ms=1000))]
    fn grab_gpu(&mut self, py: Python<'_>, timeout_ms: u32) -> PyResult<Option<GpuTexture>> {
        self.check_not_busy()?;
        let state = self.state_mut()?;

        // Lazily create the shared GPU producer texture.
        if state.gpu.is_none() {
            let prod = GpuProducerState::create(&state.device, state.width, state.height)?;
            state.gpu = Some(prod);
        }
        let width = state.width;
        let height = state.height;
        let luid = state.adapter_luid;

        let result: Option<usize> = py.detach(|| -> Result<Option<usize>, RustcamError> {
            unsafe {
                let s = self.state_unchecked_mut();
                let got = s.try_acquire_into_capture(timeout_ms)?;
                if !got {
                    return Ok(None);
                }
                let prod = s.gpu.as_ref().unwrap();
                let published = prod.copy_in(&s.context, &s.capture_tex, timeout_ms)?;
                if !published {
                    return Ok(None);
                }
                // Hand the consumer a private dup'd copy of the handle so
                // they can close it independently of the producer.
                let consumer_handle = prod.duplicate_handle_for_consumer()?;
                Ok(Some(consumer_handle))
            }
        })?;

        match result {
            Some(handle) => Ok(Some(GpuTexture::new(handle, luid, width, height))),
            None => Ok(None),
        }
    }

    fn close(&mut self) {
        if let Some(bg) = self.bg.take() {
            let _ = bg.stop();
        }
        self.state = None;
        self.closed = true;
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(
        &mut self,
        _exc_type: Bound<'_, PyAny>,
        _exc: Bound<'_, PyAny>,
        _tb: Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        self.close();
        Ok(false)
    }

    fn __repr__(&self) -> String {
        match self.state.as_ref() {
            Some(s) => format!(
                "<rustcam.Capturer output={} device={} {}x{}{}>",
                self.output_idx,
                self.device_idx,
                s.width,
                s.height,
                if s.cursor { " cursor" } else { "" },
            ),
            None => format!("<rustcam.Capturer output={} closed>", self.output_idx),
        }
    }
}

impl Capturer {
    fn check_open(&self) -> PyResult<()> {
        if self.closed {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is closed",
            ));
        }
        Ok(())
    }

    /// Returns the live CaptureState, lazily recreating the device if it was
    /// consumed by a now-finished frames() iterator.
    fn ensure_state(&mut self) -> PyResult<&mut CaptureState> {
        self.check_open()?;
        if self.state.is_none() && self.bg.is_none() {
            let state = CaptureState::new(
                self.device_idx,
                self.output_idx,
                self.cached_cursor,
            )?;
            self.state = Some(state);
        }
        self.state.as_mut().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("Capturer is in background mode")
        })
    }

    #[allow(dead_code)]
    fn state(&self) -> PyResult<&CaptureState> {
        self.state
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed"))
    }

    fn state_mut(&mut self) -> PyResult<&mut CaptureState> {
        self.ensure_state()
    }

    /// Like state_mut but doesn't borrow self mutably (caller asserts they hold an exclusive ref
    /// elsewhere — used from inside `py.detach` closures where mutex contention is moot).
    unsafe fn state_unchecked_mut(&mut self) -> &mut CaptureState {
        // SAFETY: in practice we only call this inside a single grab() invocation, where the
        // caller has already obtained a `&mut self`. The `state_mut()` check would re-borrow
        // self mutably which conflicts with the closure capture.
        self.state.as_mut().unwrap()
    }

    fn check_not_busy(&self) -> PyResult<()> {
        if self.busy.load(Ordering::Acquire) {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is busy: a frames() iterator is active",
            ));
        }
        Ok(())
    }
}

// --- Module-level enumeration helpers ---

/// Returns a list of dicts (one per output across all adapters).
#[pyfunction]
pub fn list_outputs(py: Python<'_>) -> Result<Py<PyAny>, RustcamError> {
    use pyo3::types::PyDict;
    use pyo3::types::PyList;
    unsafe {
        ensure_dpi();
        let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
        let list = PyList::empty(py);
        let mut a_idx: u32 = 0;
        loop {
            let adapter: IDXGIAdapter1 = match factory.EnumAdapters1(a_idx) {
                Ok(a) => a,
                Err(_) => break,
            };
            let adesc = adapter.GetDesc1()?;
            let aname: String = decode_wide(&adesc.Description);
            let mut o_idx: u32 = 0;
            loop {
                let output: IDXGIOutput = match adapter.EnumOutputs(o_idx) {
                    Ok(o) => o,
                    Err(_) => break,
                };
                let od: DXGI_OUTPUT_DESC = output.GetDesc()?;
                let oname: String = decode_wide(&od.DeviceName);
                let w = (od.DesktopCoordinates.right - od.DesktopCoordinates.left) as u32;
                let h = (od.DesktopCoordinates.bottom - od.DesktopCoordinates.top) as u32;
                let d = PyDict::new(py);
                d.set_item("device_idx", a_idx).map_err(pyerr_to_rust)?;
                d.set_item("output_idx", o_idx).map_err(pyerr_to_rust)?;
                d.set_item("name", &aname).map_err(pyerr_to_rust)?;
                d.set_item("output_name", &oname).map_err(pyerr_to_rust)?;
                d.set_item("width", w).map_err(pyerr_to_rust)?;
                d.set_item("height", h).map_err(pyerr_to_rust)?;
                d.set_item("rotation", od.Rotation.0 as u32).map_err(pyerr_to_rust)?;
                d.set_item("is_primary", od.AttachedToDesktop.as_bool()).map_err(pyerr_to_rust)?;
                list.append(d).map_err(pyerr_to_rust)?;
                o_idx += 1;
            }
            a_idx += 1;
        }
        Ok(list.into())
    }
}

fn pyerr_to_rust(e: PyErr) -> RustcamError {
    RustcamError::Value(e.to_string())
}

#[pyfunction]
pub fn device_info() -> Result<String, RustcamError> {
    unsafe {
        ensure_dpi();
        let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
        let mut out = String::new();
        let mut i = 0u32;
        loop {
            let adapter: IDXGIAdapter1 = match factory.EnumAdapters1(i) {
                Ok(a) => a,
                Err(_) => break,
            };
            let d = adapter.GetDesc1()?;
            let name = decode_wide(&d.Description);
            out.push_str(&format!(
                "Device[{}]:<Name:{} VendorId:{:#06X} DeviceId:{:#06X} VRAM:{}MB>\n",
                i,
                name,
                d.VendorId,
                d.DeviceId,
                d.DedicatedVideoMemory / (1024 * 1024),
            ));
            i += 1;
        }
        Ok(out)
    }
}

#[pyfunction]
pub fn output_info() -> Result<String, RustcamError> {
    unsafe {
        ensure_dpi();
        let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
        let mut out = String::new();
        let mut a_idx = 0u32;
        loop {
            let adapter: IDXGIAdapter1 = match factory.EnumAdapters1(a_idx) {
                Ok(a) => a,
                Err(_) => break,
            };
            let mut o_idx = 0u32;
            loop {
                let output: IDXGIOutput = match adapter.EnumOutputs(o_idx) {
                    Ok(o) => o,
                    Err(_) => break,
                };
                let od: DXGI_OUTPUT_DESC = output.GetDesc()?;
                let w = od.DesktopCoordinates.right - od.DesktopCoordinates.left;
                let h = od.DesktopCoordinates.bottom - od.DesktopCoordinates.top;
                out.push_str(&format!(
                    "Device[{}] Output[{}]: Res:({}, {}) Rot:{} Primary:{}\n",
                    a_idx,
                    o_idx,
                    w,
                    h,
                    od.Rotation.0,
                    od.AttachedToDesktop.as_bool(),
                ));
                o_idx += 1;
            }
            a_idx += 1;
        }
        Ok(out)
    }
}

fn decode_wide(buf: &[u16]) -> String {
    let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf16_lossy(&buf[..end])
}

// Silence an unused-import warning when start/frames/grab_gpu are stubs.
#[allow(dead_code)]
fn _suppress() {
    let _ = (
        std::marker::PhantomData::<Mutex<()>>,
        std::marker::PhantomData::<PyTuple>,
    );
}
