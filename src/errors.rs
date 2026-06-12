use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use windows::Win32::Graphics::Dxgi::{
    DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_DEVICE_REMOVED, DXGI_ERROR_DEVICE_RESET,
};

create_exception!(_rustcam, CaptureError, PyException);
create_exception!(_rustcam, DeviceError, CaptureError);
create_exception!(_rustcam, DuplicationError, CaptureError);
create_exception!(_rustcam, AccessLost, CaptureError);
create_exception!(_rustcam, CaptureTimeout, CaptureError);

pub fn map_dxgi(e: windows::core::Error) -> PyErr {
    let h = e.code().0 as u32;
    let msg = format!("DXGI 0x{:08X}: {}", h, e.message());
    let py_err = match e.code() {
        c if c == DXGI_ERROR_DEVICE_REMOVED || c == DXGI_ERROR_DEVICE_RESET => {
            DeviceError::new_err(msg)
        }
        c if c == DXGI_ERROR_ACCESS_LOST => AccessLost::new_err(msg),
        _ => CaptureError::new_err(msg),
    };
    Python::attach(|py| {
        let bound = py_err.value(py);
        let _ = bound.setattr("hresult", h);
    });
    py_err
}

impl From<windows::core::Error> for RustcamError {
    fn from(e: windows::core::Error) -> Self {
        RustcamError::Dxgi(e)
    }
}

#[allow(dead_code)]
pub enum RustcamError {
    Dxgi(windows::core::Error),
    Closed,
    Busy,
    AlreadyRunning,
    NotRunning,
    Value(String),
    Timeout,
}

impl From<RustcamError> for PyErr {
    fn from(e: RustcamError) -> Self {
        match e {
            RustcamError::Dxgi(err) => map_dxgi(err),
            RustcamError::Closed => {
                pyo3::exceptions::PyRuntimeError::new_err("Capturer is closed")
            }
            RustcamError::Busy => pyo3::exceptions::PyRuntimeError::new_err(
                "Capturer is busy: a frames() iterator is active",
            ),
            RustcamError::AlreadyRunning => {
                pyo3::exceptions::PyRuntimeError::new_err("Capturer is already capturing")
            }
            RustcamError::NotRunning => {
                pyo3::exceptions::PyRuntimeError::new_err("Capturer is not capturing")
            }
            RustcamError::Value(s) => pyo3::exceptions::PyValueError::new_err(s),
            RustcamError::Timeout => CaptureTimeout::new_err("deadline expired"),
        }
    }
}

// (Result alias removed — modules use `Result<T, RustcamError>` directly.)
