//! High-resolution waitable timer wrapper.
//!
//! Replaces `std::thread::sleep` for paced waits. `std::thread::sleep` on
//! Windows defaults to ~15.6 ms granularity unless the system timer
//! resolution is bumped via `timeBeginPeriod(1)`. That granularity makes a
//! 16.67 ms sleep (1/60 fps) regularly come back at 30 ms, which is why
//! `start(target_fps=60)` previously delivered ~58 fps.
//!
//! `CreateWaitableTimerExW(CREATE_WAITABLE_TIMER_HIGH_RESOLUTION)` was
//! added in Windows 10 1803 and gives sub-millisecond accuracy without
//! touching the global timer resolution.

use std::time::Duration;

use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::{
    CreateWaitableTimerExW, SetWaitableTimer, WaitForSingleObject,
    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS, INFINITE,
};

pub struct HrTimer {
    handle: HANDLE,
}

impl HrTimer {
    pub fn new() -> windows::core::Result<Self> {
        let handle = unsafe {
            CreateWaitableTimerExW(
                None,
                windows::core::PCWSTR::null(),
                CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                TIMER_ALL_ACCESS.0,
            )?
        };
        Ok(Self { handle })
    }

    /// Block until at least `duration` has elapsed. Sub-millisecond accuracy
    /// on Windows 10 1803+. Zero or negative duration returns immediately.
    pub fn sleep(&self, duration: Duration) {
        if duration.is_zero() {
            return;
        }
        // SetWaitableTimer's lpDueTime is in 100-nanosecond intervals.
        // Negative = relative time from now.
        let ns_100 = duration.as_nanos() / 100;
        if ns_100 == 0 {
            return;
        }
        let due_time: i64 = -(ns_100 as i64);
        unsafe {
            if SetWaitableTimer(
                self.handle,
                &due_time as *const i64,
                0,    // period — one-shot
                None, // no APC
                None, // no arg
                false,
            )
            .is_ok()
            {
                let _ = WaitForSingleObject(self.handle, INFINITE);
            }
        }
    }
}

impl Drop for HrTimer {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.handle);
        }
    }
}

// HANDLE is just an opaque pointer; the timer kernel object itself is
// safe to use across threads. windows-rs marks HANDLE as !Send/!Sync by
// default though, so we assert this manually.
unsafe impl Send for HrTimer {}
unsafe impl Sync for HrTimer {}
