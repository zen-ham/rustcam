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
    D3D11_RESOURCE_MISC_GDI_COMPATIBLE, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC,
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
use crate::cursor::{draw_cursor, CursorCache};
use crate::errors::{map_dxgi, RustcamError};
use crate::region::{crop_copy_bgra, Region};

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
    pub cursor_cache: CursorCache,
    pub adapter_luid: (u32, i32),
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
                MiscFlags: if cursor {
                    D3D11_RESOURCE_MISC_GDI_COMPATIBLE.0 as u32
                } else {
                    0
                },
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
                cursor_cache: CursorCache::new(),
                adapter_luid,
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
                let _ = self.dup.ReleaseFrame();
                if self.cursor {
                    draw_cursor(&self.capture_tex, &mut self.cursor_cache);
                }
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
    /// Set while a frames() iterator is alive; blocks grab/start/grab_gpu.
    pub(crate) busy: Arc<AtomicBool>,
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
            Some(t) => Region::from_tuple(t, state.width, state.height)?,
            None => full,
        };
        Ok(Self {
            state: Some(state),
            region,
            device_idx: device,
            output_idx: output,
            busy: Arc::new(AtomicBool::new(false)),
        })
    }

    // --- read-only state ---
    #[getter]
    fn width(&self) -> PyResult<u32> {
        Ok(self.state()?.width)
    }
    #[getter]
    fn height(&self) -> PyResult<u32> {
        Ok(self.state()?.height)
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
        Ok(self.state()?.cursor)
    }
    #[getter]
    fn rotation(&self) -> PyResult<u32> {
        Ok(self.state()?.rotation)
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
        // start()/stop() not implemented in v0.0.1 — wired in pacing.rs.
        false
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
            Some(t) => Region::from_tuple(t, state.width, state.height)?,
            None => self.region,
        };

        // Acquire + map without holding the GIL. Build the dst Vec<u8> in BGRA, then
        // (still without the GIL) format-convert if needed.
        let bgra: Option<Vec<u8>> = py.detach(|| -> CapResult<Option<Vec<u8>>> {
            unsafe {
                let got = self.state_unchecked_mut().try_acquire_into_capture(timeout_ms)?;
                if !got {
                    return Ok(None);
                }
                let m = self.state_unchecked_mut().map_staging()?;
                let w = region.width() as usize;
                let h = region.height() as usize;
                let mut buf = vec![0u8; w * h * 4];
                crop_copy_bgra(&mut buf, m.pData as *const u8, m.RowPitch as usize, region);
                self.state_unchecked_mut().unmap_staging();
                Ok(Some(buf))
            }
        })?;

        let bgra = match bgra {
            Some(v) => v,
            None => return Ok(None),
        };

        let w = region.width() as usize;
        let h = region.height() as usize;

        let array = match fmt {
            Fmt::Bgra => {
                let arr = bgra.into_pyarray(py);
                arr.reshape([h, w, 4])?
            }
            _ => {
                let mut out = vec![0u8; w * h * fmt.channels()];
                py.detach(|| bgra_to(fmt, &bgra, &mut out, w, h));
                let arr = out.into_pyarray(py);
                arr.reshape([h, w, fmt.channels()])?
            }
        };
        Ok(Some(array))
    }

    /// start() / stop() / get_latest_frame() — placeholder; full implementation arrives
    /// when the background-thread pacer lands. v0.0.1 raises NotImplemented.
    #[pyo3(signature = (target_fps=60, region=None, video_mode=false))]
    fn start(
        &mut self,
        target_fps: u32,
        region: Option<(i64, i64, i64, i64)>,
        video_mode: bool,
    ) -> PyResult<()> {
        let _ = (target_fps, region, video_mode);
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "start() lands in v0.0.2; use grab() in a loop for now",
        ))
    }

    fn stop(&mut self) -> PyResult<()> {
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "stop() lands in v0.0.2",
        ))
    }

    #[pyo3(signature = (timeout_ms=None))]
    fn get_latest_frame(&mut self, timeout_ms: Option<u32>) -> PyResult<()> {
        let _ = timeout_ms;
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "get_latest_frame() lands in v0.0.2",
        ))
    }

    #[pyo3(signature = (fps, fmt="bgra", region=None, timeout_ms=5000))]
    fn frames(
        &mut self,
        fps: u32,
        fmt: &str,
        region: Option<(i64, i64, i64, i64)>,
        timeout_ms: u32,
    ) -> PyResult<()> {
        let _ = (fps, fmt, region, timeout_ms);
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "frames() lands in v0.0.2",
        ))
    }

    #[pyo3(signature = (timeout_ms=1000))]
    fn grab_gpu(&mut self, timeout_ms: u32) -> PyResult<()> {
        let _ = timeout_ms;
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "grab_gpu() lands in v0.0.2",
        ))
    }

    fn close(&mut self) {
        self.state = None;
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
    fn state(&self) -> PyResult<&CaptureState> {
        self.state
            .as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed"))
    }

    fn state_mut(&mut self) -> PyResult<&mut CaptureState> {
        self.state
            .as_mut()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed"))
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
