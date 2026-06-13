//! Background-thread capture + CFR-paced iterator.
//!
//! Two surfaces share the same backing infrastructure:
//!
//! * `start()/stop()/get_latest_frame()` — bettercam-parity background mode.
//!   The Rust capture thread owns the COM state for the run; the main thread
//!   blocks on a Condvar to read the latest frame from a mailbox.
//!
//! * `frames(fps=N)` — paced CFR iterator. Same capture thread; emission is
//!   driven by a slot clock that dup-or-drops frames so the consumer always
//!   sees exactly `fps` frames per wall-clock second.
//!
//! The D3D11 device cannot move between threads under Rust's `Send` rules out
//! of the box, but the underlying COM contract permits ownership transfer
//! (just not concurrent use of the immediate context). We assert `Send` on a
//! small wrapper so the capture-state can move into the worker thread.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use numpy::{IntoPyArray, PyArray3, PyArrayMethods};
use parking_lot::{Condvar, Mutex};
use pyo3::prelude::*;

use crate::capture::CaptureState;
use crate::convert::{bgra_to, Fmt};
use crate::cursor::composite_cursor_into_bgra;
use crate::errors::{CaptureTimeout, RustcamError};
use crate::region::{crop_copy_bgra, Region};

/// Wrap CaptureState so we can move it into another thread. Safe because we
/// guarantee single-threaded use of the immediate context — the bg thread
/// takes ownership for the duration of the run and gives it back on stop.
pub(crate) struct CaptureStateSend(pub CaptureState);
unsafe impl Send for CaptureStateSend {}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Frame {
    pub seq: u64,
    pub captured_at: Instant,
}

/// One-slot mailbox. The buffer is stored as `Arc<Vec<u8>>` so that the
/// capture thread can keep a cheap clone of the latest frame (for video_mode
/// re-publish) and the consumer can take ownership of the Arc without
/// copying 8 MB of BGRA. Whoever has a strong ref last (usually the
/// consumer) gets to consume the buffer via Arc::try_unwrap; otherwise we
/// copy at the API boundary.
pub(crate) struct Mailbox {
    pub buf: Mutex<Option<(Frame, Arc<Vec<u8>>)>>,
    /// Win32 auto-reset event used to wake the consumer thread. Replaces
    /// the parking_lot Condvar that previously lived here — that primitive
    /// is built on top of `SleepConditionVariableCS` on Windows and inherits
    /// the system timer's ~15 ms granularity, capping consumer wake-ups
    /// at ~67 fps. `WaitForSingleObject` on a kernel Event has sub-
    /// millisecond accuracy.
    pub signal: crate::hr_timer::EventSignal,
    pub error: Mutex<Option<PyErr>>,
    pub stop: AtomicBool,
}

impl Mailbox {
    pub fn new() -> Self {
        Self {
            buf: Mutex::new(None),
            signal: crate::hr_timer::EventSignal::new()
                .expect("CreateEventW failed; required for the consumer wait path"),
            error: Mutex::new(None),
            stop: AtomicBool::new(false),
        }
    }
}

pub struct BackgroundHandle {
    join: Option<JoinHandle<CaptureStateSend>>,
    pub(crate) mailbox: Arc<Mailbox>,
    pub region: Region,
    pub width: u32,
    pub height: u32,
}

impl BackgroundHandle {
    /// Signal stop, join, return the recovered CaptureState.
    pub fn stop(mut self) -> CaptureState {
        self.mailbox.stop.store(true, Ordering::Release);
        // Wake any park inside the loop (the capture loop polls stop so this
        // is belt + suspenders).
        self.mailbox.signal.signal();
        let joined = self.join.take().unwrap().join();
        match joined {
            Ok(s) => s.0,
            Err(_) => panic!("capture thread panicked"),
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct StartOpts {
    pub target_fps: u32,
    pub region: Region,
    pub video_mode: bool,
}

/// Spawn the capture thread. CaptureState is moved in; the handle's `stop()`
/// recovers it for reuse.
pub fn spawn(state: CaptureState, opts: StartOpts) -> BackgroundHandle {
    let mailbox = Arc::new(Mailbox::new());
    let mb = mailbox.clone();
    let width = opts.region.width();
    let height = opts.region.height();
    let join = std::thread::spawn(move || {
        let mut state = state;
        capture_loop(&mut state, &mb, opts);
        CaptureStateSend(state)
    });
    BackgroundHandle {
        join: Some(join),
        mailbox,
        region: opts.region,
        width,
        height,
    }
}

fn capture_loop(state: &mut CaptureState, mb: &Arc<Mailbox>, opts: StartOpts) {
    let mut seq: u64 = 0;
    let mut last_arc: Option<Arc<Vec<u8>>> = None;
    let period = if opts.target_fps > 0 {
        Some(Duration::from_nanos(1_000_000_000u64 / opts.target_fps as u64))
    } else {
        None
    };
    let mut next_deadline = Instant::now();
    let region = opts.region;
    // High-res waitable timer for paced sleeps. std::thread::sleep on
    // Windows has ~15ms granularity which caps `target_fps=60` at ~58 fps
    // in practice. CreateWaitableTimerExW(HIGH_RESOLUTION) gets us
    // sub-millisecond accuracy without touching the global timer res.
    // Falls back to std::thread::sleep if the OS doesn't support it.
    let hr = crate::hr_timer::HrTimer::new().ok();

    loop {
        if mb.stop.load(Ordering::Acquire) {
            break;
        }

        let got = unsafe { state.try_acquire_into_capture(2) };
        let got = match got {
            Ok(b) => b,
            Err(e) => {
                let mut err = mb.error.lock();
                *err = Some(e.into());
                break;
            }
        };

        let mut new_arc: Option<Arc<Vec<u8>>> = None;
        if got {
            let map_result = unsafe { state.map_staging() };
            match map_result {
                Ok(m) => {
                    let w = region.width() as usize;
                    let h = region.height() as usize;
                    let mut buf = vec![0u8; w * h * 4];
                    unsafe {
                        crop_copy_bgra(
                            &mut buf,
                            m.pData as *const u8,
                            m.RowPitch as usize,
                            region,
                        );
                        state.unmap_staging();
                    }
                    // Cursor composite (same software path used by grab()).
                    if state.cursor {
                        composite_cursor_into_bgra(
                            &mut buf,
                            region.width(),
                            region.height(),
                            region.left as i32,
                            region.top as i32,
                            &state.cursor_state,
                        );
                    }
                    new_arc = Some(Arc::new(buf));
                }
                Err(e) => {
                    let mut err = mb.error.lock();
                    *err = Some(e.into());
                    break;
                }
            }
        }

        // Publish: new frame, OR if video_mode + we already have a previous
        // frame, re-publish that one (an Arc clone, just a refcount bump).
        let to_publish: Option<Arc<Vec<u8>>> = if let Some(arc) = new_arc.as_ref() {
            Some(arc.clone())
        } else if opts.video_mode {
            last_arc.clone()
        } else {
            None
        };

        if let Some(arc) = to_publish {
            seq += 1;
            let frame = Frame {
                seq,
                captured_at: Instant::now(),
            };
            {
                let mut slot = mb.buf.lock();
                *slot = Some((frame, arc));
            }
            mb.signal.signal();
            if let Some(nb) = new_arc {
                last_arc = Some(nb);
            }
        }

        if let Some(period) = period {
            next_deadline += period;
            let now = Instant::now();
            if next_deadline > now {
                let delta = next_deadline - now;
                if let Some(t) = hr.as_ref() {
                    t.sleep(delta);
                } else {
                    std::thread::sleep(delta);
                }
            } else {
                // We're behind schedule — reset to now so we don't burn a
                // backlog of zero-duration sleeps.
                next_deadline = now;
            }
        }
    }
}

/// Blocking read: returns when a fresh frame is in the mailbox OR the deadline expires.
/// Takes the frame out of the mailbox (consumes it). Returns Ok(None) if stopped.
pub fn get_latest(
    mb: &Arc<Mailbox>,
    timeout: Option<Duration>,
) -> Result<Option<(Frame, Arc<Vec<u8>>)>, RustcamError> {
    let deadline = timeout.map(|d| Instant::now() + d);

    loop {
        // Fast path: peek and take without waiting.
        {
            let mut slot = mb.buf.lock();
            if let Some(pair) = slot.take() {
                return Ok(Some(pair));
            }
        }
        if mb.stop.load(Ordering::Acquire) {
            return Ok(None);
        }
        if let Some(err) = mb.error.lock().take() {
            return Err(RustcamError::Value(err.to_string()));
        }
        // Slow path: block on the kernel event until the capture thread
        // publishes (sub-millisecond wake-up via WaitForSingleObject) or
        // the deadline expires.
        let wait_timeout = match deadline {
            Some(end) => {
                let now = Instant::now();
                if now >= end {
                    return Err(RustcamError::Timeout);
                }
                Some(end - now)
            }
            None => None,
        };
        mb.signal.wait(wait_timeout);
    }
}

/// Convert an Arc<Vec<u8>> back into an owned Vec. If we hold the only strong
/// ref, unwrap is zero-copy; otherwise we clone the Vec.
pub fn arc_into_vec(arc: Arc<Vec<u8>>) -> Vec<u8> {
    Arc::try_unwrap(arc).unwrap_or_else(|a| (*a).clone())
}

/// Peek without consuming. Used by the CFR pacer.
pub fn peek_seq(mb: &Arc<Mailbox>) -> Option<u64> {
    mb.buf.lock().as_ref().map(|(f, _)| f.seq)
}

/// Take the buffer if its seq is greater than `since_seq`.
pub fn take_if_newer(
    mb: &Arc<Mailbox>,
    since_seq: u64,
) -> Option<(Frame, Arc<Vec<u8>>)> {
    let mut slot = mb.buf.lock();
    let take = slot.as_ref().is_some_and(|(f, _)| f.seq > since_seq);
    if take {
        slot.take()
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// CFR-paced iterator returned by Capturer.frames(fps=N)
// ---------------------------------------------------------------------------

#[pyclass(unsendable, module = "rustcam._rustcam")]
pub struct FramesIter {
    mailbox: Arc<Mailbox>,
    /// Held shutdown handle. On Drop the loop joins.
    handle_slot: Option<BackgroundHandle>,
    capturer_busy_flag: Arc<AtomicBool>,
    /// Slot pacing
    iter_start: Instant,
    slot: u64,
    fps: u32,
    period: Duration,
    /// fmt + region for emit-side conversion
    fmt: Fmt,
    emit_region: Region,
    /// What seq we last emitted (for detecting fresh frames)
    last_seq: u64,
    /// Cache of the last emitted BGRA buffer (for duplicate slots)
    last_buf: Option<Arc<Vec<u8>>>,
    /// Per-slot wait deadline (timeout_ms in the public API)
    slot_timeout: Duration,
    /// High-res timer for sub-ms slot pacing.
    hr_timer: Option<crate::hr_timer::HrTimer>,
    /// Whether we've handed back the parent's CaptureState yet.
    closed: bool,
}

impl FramesIter {
    pub fn new(
        handle: BackgroundHandle,
        busy: Arc<AtomicBool>,
        fps: u32,
        fmt: Fmt,
        emit_region: Region,
        timeout: Duration,
    ) -> Self {
        let period = Duration::from_nanos(1_000_000_000u64 / fps.max(1) as u64);
        let mailbox = handle.mailbox.clone();
        Self {
            mailbox,
            handle_slot: Some(handle),
            capturer_busy_flag: busy,
            iter_start: Instant::now(),
            slot: 0,
            fps,
            period,
            fmt,
            emit_region,
            last_seq: 0,
            last_buf: None,
            slot_timeout: timeout,
            hr_timer: crate::hr_timer::HrTimer::new().ok(),
            closed: false,
        }
    }

    /// Recover the CaptureState. Caller is responsible for putting it back on
    /// the Capturer. Idempotent (returns None if already closed).
    pub fn take_state(&mut self) -> Option<CaptureState> {
        if self.closed {
            return None;
        }
        self.closed = true;
        let h = self.handle_slot.take()?;
        Some(h.stop())
    }
}

impl Drop for FramesIter {
    fn drop(&mut self) {
        // Make sure the bg thread is wound down even if Python never explicitly
        // closes the iterator.
        let _ = self.take_state();
        self.capturer_busy_flag.store(false, Ordering::Release);
    }
}

#[pymethods]
impl FramesIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__<'py>(
        &mut self,
        py: Python<'py>,
    ) -> PyResult<Option<(Bound<'py, PyArray3<u8>>, f64)>> {
        if self.closed {
            return Ok(None);
        }

        // Compute deadline for this slot.
        let slot_wall = self.iter_start + self.period * (self.slot as u32);
        let now = Instant::now();
        if slot_wall > now {
            let to_sleep = slot_wall - now;
            if let Some(t) = self.hr_timer.as_ref() {
                py.detach(|| t.sleep(to_sleep));
            } else {
                py.detach(|| std::thread::sleep(to_sleep));
            }
        }

        // Drain mailbox: prefer a fresh frame, else dup the last we emitted.
        // Wait up to slot_timeout for the first frame.
        let fresh = py.detach(|| -> Result<Option<(Frame, Arc<Vec<u8>>)>, RustcamError> {
            // If first slot and no frame yet, wait up to slot_timeout.
            if self.last_buf.is_none() {
                return get_latest(&self.mailbox, Some(self.slot_timeout));
            }
            Ok(take_if_newer(&self.mailbox, self.last_seq))
        })?;

        let buf: Vec<u8> = match (fresh, &self.last_buf) {
            (Some((f, b)), _) => {
                self.last_seq = f.seq;
                self.last_buf = Some(b.clone());
                arc_into_vec(b)
            }
            (None, Some(prev)) => (**prev).clone(),
            (None, None) => {
                return Err(CaptureTimeout::new_err(
                    "frames() iteration produced no frames",
                ));
            }
        };

        // Slot timestamp = slot wallclock (monotonic, exact 1/fps spacing).
        let ts_secs = self.slot as f64 / self.fps as f64;
        self.slot += 1;

        let w = self.emit_region.width() as usize;
        let h = self.emit_region.height() as usize;

        // Format conversion (still without holding GIL where convert is cheap
        // — but we already have the buffer in hand so we may as well finalise
        // here).
        let arr = match self.fmt {
            Fmt::Bgra => {
                let v = buf;
                let arr = v.into_pyarray(py);
                arr.reshape([h, w, 4])?
            }
            _ => {
                let mut out = vec![0u8; w * h * self.fmt.channels()];
                py.detach(|| bgra_to(self.fmt, &buf, &mut out, w, h));
                let arr = out.into_pyarray(py);
                arr.reshape([h, w, self.fmt.channels()])?
            }
        };
        Ok(Some((arr, ts_secs)))
    }

    /// Explicit close — wind down bg thread, release Capturer busy flag.
    fn close(&mut self) {
        let _ = self.take_state();
        self.capturer_busy_flag.store(false, Ordering::Release);
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
}
