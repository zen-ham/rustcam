//! rustcam — Rust-backed Windows DXGI Desktop Duplication API screen capture.

use pyo3::prelude::*;

mod capture;
mod convert;
mod cursor;
mod errors;
mod gpu;
mod hr_timer;
mod pacing;
mod region;

use crate::capture::{device_info, list_outputs, output_info, Capturer};
use crate::errors::{AccessLost, CaptureError, CaptureTimeout, DeviceError, DuplicationError};
use crate::gpu::GpuTexture;
use crate::pacing::FramesIter;

#[pymodule]
fn _rustcam(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Capturer>()?;
    m.add_class::<FramesIter>()?;
    m.add_class::<GpuTexture>()?;

    m.add("CaptureError", py.get_type::<CaptureError>())?;
    m.add("DeviceError", py.get_type::<DeviceError>())?;
    m.add("DuplicationError", py.get_type::<DuplicationError>())?;
    m.add("AccessLost", py.get_type::<AccessLost>())?;
    m.add("CaptureTimeout", py.get_type::<CaptureTimeout>())?;

    m.add_function(wrap_pyfunction!(list_outputs, m)?)?;
    m.add_function(wrap_pyfunction!(device_info, m)?)?;
    m.add_function(wrap_pyfunction!(output_info, m)?)?;

    Ok(())
}
