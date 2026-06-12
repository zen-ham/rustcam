//! Zero-copy GPU-resident capture: `Capturer.grab_gpu()` returns a
//! `GpuTexture` wrapping a shared NT handle + keyed mutex around a BGRA D3D11
//! texture. The data never leaves VRAM on the producer side; the consumer
//! opens the handle on its own D3D11 device with `OpenSharedResource1` and
//! reads under the keyed-mutex protocol.
//!
//! Keyed-mutex protocol:
//! - producer creates the texture with `D3D11_RESOURCE_MISC_SHARED_NTHANDLE
//!   | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX`. Initial mutex state is
//!   "unlocked" (any AcquireSync key value works first).
//! - producer per `grab_gpu()`:
//!     1. `AcquireNextFrame` + `CopyResource` into the producer's internal
//!        BGRA capture texture (cursor + region as usual).
//!     2. `IDXGIKeyedMutex::AcquireSync(0, timeout_ms)` on the *shared*
//!        texture. Returns `None` on timeout.
//!     3. `CopyResource(shared, capture)`.
//!     4. `IDXGIKeyedMutex::ReleaseSync(1)`.
//!     5. Build a `GpuTexture` Python object wrapping the NT handle.
//! - consumer per frame:
//!     1. Open the handle on its own D3D11 device with `OpenSharedResource1`.
//!     2. `AcquireSync(1, timeout)` — wait for producer release.
//!     3. Sample the texture.
//!     4. `ReleaseSync(0)` — hand back to producer.

use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;
use windows::core::Interface;
use windows::Win32::Foundation::{
    CloseHandle, DuplicateHandle, DUPLICATE_HANDLE_OPTIONS, DUPLICATE_SAME_ACCESS, HANDLE,
};
use windows::Win32::System::Threading::GetCurrentProcess;
use windows::Win32::Graphics::Direct3D11::{
    ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D, D3D11_BIND_RENDER_TARGET,
    D3D11_BIND_SHADER_RESOURCE, D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX,
    D3D11_RESOURCE_MISC_SHARED_NTHANDLE, D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use windows::Win32::Graphics::Dxgi::{
    IDXGIKeyedMutex, IDXGIResource1, DXGI_SHARED_RESOURCE_READ, DXGI_SHARED_RESOURCE_WRITE,
};

use crate::errors::{map_dxgi, RustcamError};

/// Producer-side shared-texture state. Built lazily on first `grab_gpu()`.
///
/// v0.0.3 does NOT use a keyed mutex. The consumer is responsible for syncing
/// its read against the producer (the simplest approach is to GPU-copy out
/// of the shared texture into a consumer-private target before issuing any
/// other work that depends on the content). A future version may add an
/// opt-in keyed-mutex mode for stricter producer/consumer coordination.
pub struct GpuProducerState {
    pub shared_tex: ID3D11Texture2D,
    pub keyed_mutex: IDXGIKeyedMutex,
    pub shared_handle_value: usize,
    pub width: u32,
    pub height: u32,
}

impl GpuProducerState {
    pub fn create(
        device: &ID3D11Device,
        width: u32,
        height: u32,
    ) -> Result<Self, RustcamError> {
        unsafe {
            let desc = D3D11_TEXTURE2D_DESC {
                Width: width,
                Height: height,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
                CPUAccessFlags: 0,
                MiscFlags: (D3D11_RESOURCE_MISC_SHARED_NTHANDLE.0
                    | D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX.0) as u32,
            };
            let mut tex: Option<ID3D11Texture2D> = None;
            device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .map_err(RustcamError::Dxgi)?;
            let shared_tex = tex.unwrap();

            let keyed_mutex: IDXGIKeyedMutex = shared_tex.cast().map_err(RustcamError::Dxgi)?;
            let dxgi_res: IDXGIResource1 = shared_tex.cast().map_err(RustcamError::Dxgi)?;

            // Create the NT handle. Stable for the lifetime of dxgi_res.
            let access = (DXGI_SHARED_RESOURCE_READ.0 | DXGI_SHARED_RESOURCE_WRITE.0) as u32;
            let h: HANDLE = dxgi_res
                .CreateSharedHandle(None, access, None)
                .map_err(RustcamError::Dxgi)?;

            Ok(Self {
                shared_tex,
                keyed_mutex,
                shared_handle_value: h.0 as usize,
                width,
                height,
            })
        }
    }

    /// Producer-side copy with keyed-mutex sync. Both producer and consumer
    /// release on key 0 so each side can re-acquire without coordination
    /// (simpler than the strict 0/1 alternation; trades strict ordering for
    /// usable producer-without-consumer mode).
    pub unsafe fn copy_in(
        &self,
        ctx: &ID3D11DeviceContext,
        capture_tex: &ID3D11Texture2D,
        timeout_ms: u32,
    ) -> Result<bool, RustcamError> {
        let r = self.keyed_mutex.AcquireSync(0, timeout_ms);
        match r {
            Ok(_) => {
                ctx.CopyResource(&self.shared_tex, capture_tex);
                self.keyed_mutex
                    .ReleaseSync(0)
                    .map_err(RustcamError::Dxgi)?;
                Ok(true)
            }
            Err(e) => {
                // 0x102 = WAIT_TIMEOUT
                if e.code().0 as u32 == 0x102 {
                    Ok(false)
                } else {
                    Err(RustcamError::Dxgi(e))
                }
            }
        }
    }

    /// Make a private copy of the producer's shared handle that the caller
    /// can close independently. The producer's original handle stays valid.
    pub unsafe fn duplicate_handle_for_consumer(&self) -> Result<usize, RustcamError> {
        let process = GetCurrentProcess();
        let mut dup_h = HANDLE::default();
        DuplicateHandle(
            process,
            HANDLE(self.shared_handle_value as *mut _),
            process,
            &mut dup_h,
            0,
            false,
            DUPLICATE_SAME_ACCESS,
        )
        .map_err(RustcamError::Dxgi)?;
        Ok(dup_h.0 as usize)
    }
}

impl Drop for GpuProducerState {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(HANDLE(self.shared_handle_value as *mut _));
        }
    }
}

/// Public Python object handed to the consumer.
#[pyclass(unsendable, module = "rustcam._rustcam")]
pub struct GpuTexture {
    closed: AtomicBool,
    /// Duplicated handle value (independent of the producer's handle).
    handle: usize,
    luid_low: u32,
    luid_high: i32,
    w: u32,
    h: u32,
}

impl GpuTexture {
    pub fn new(handle: usize, luid: (u32, i32), w: u32, h: u32) -> Self {
        Self {
            closed: AtomicBool::new(false),
            handle,
            luid_low: luid.0,
            luid_high: luid.1,
            w,
            h,
        }
    }
}

#[pymethods]
impl GpuTexture {
    #[getter]
    fn shared_handle(&self) -> PyResult<usize> {
        if self.closed.load(Ordering::Acquire) {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "GpuTexture is closed",
            ));
        }
        Ok(self.handle)
    }

    #[getter]
    fn luid(&self) -> (u32, i32) {
        (self.luid_low, self.luid_high)
    }

    #[getter]
    fn format(&self) -> &'static str {
        "DXGI_FORMAT_B8G8R8A8_UNORM"
    }

    #[getter]
    fn width(&self) -> u32 {
        self.w
    }
    #[getter]
    fn height(&self) -> u32 {
        self.h
    }

    /// Reserved for a future opt-in keyed-mutex mode. v0.0.3 returns 0 for
    /// both keys to signal "no mutex protocol active on this texture".
    #[getter]
    fn keyed_mutex_acquire_key(&self) -> u64 {
        0
    }
    #[getter]
    fn keyed_mutex_release_key(&self) -> u64 {
        0
    }

    fn close(&mut self) {
        if !self.closed.swap(true, Ordering::AcqRel) {
            unsafe {
                let _ = CloseHandle(HANDLE(self.handle as *mut _));
            }
        }
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
        format!(
            "<rustcam.GpuTexture {}x{} BGRA8 handle=0x{:X} luid=({}, {})>",
            self.w, self.h, self.handle, self.luid_low, self.luid_high,
        )
    }
}

// `map_dxgi` import silences the unused warning chain; gpu uses RustcamError::Dxgi directly.
#[allow(dead_code)]
fn _suppress() {
    let _ = map_dxgi;
}
