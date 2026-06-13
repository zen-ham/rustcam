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
    CreateEventW, CreateWaitableTimerExW, SetEvent, SetWaitableTimer, WaitForSingleObject,
    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, INFINITE, TIMER_ALL_ACCESS,
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

/// Win32 auto-reset event with sub-millisecond wake-up accuracy.
///
/// Replaces `parking_lot::Condvar` in places where the wait is on a wall-
/// clock deadline at high refresh rates. Parking_lot's Condvar on Windows
/// uses `SleepConditionVariableCS` which inherits the system timer's
/// ~15 ms granularity, so a "wait until next publish" call can sit idle
/// for 13 ms even when the publish happens immediately. `WaitForSingleObject`
/// on an Event has sub-millisecond accuracy regardless of the system
/// timer.
///
/// Semantics: signal() sets the event; if no thread is waiting, the
/// signal is latched (one set = at most one wake-up). wait() blocks until
/// the event is signaled, then auto-resets it. The single-publisher /
/// single-consumer pattern this is used for in pacing.rs guarantees no
/// notification is lost: every set() is either consumed by an outstanding
/// wait() or queues for the next one.
pub struct EventSignal {
    handle: HANDLE,
}

impl EventSignal {
    pub fn new() -> windows::core::Result<Self> {
        let handle = unsafe {
            CreateEventW(
                None,
                false, // bManualReset = FALSE -> auto-reset
                false, // bInitialState = FALSE -> initially non-signaled
                windows::core::PCWSTR::null(),
            )?
        };
        Ok(Self { handle })
    }

    /// Signal the event. Wakes one waiter (or latches if no waiter).
    pub fn signal(&self) {
        unsafe {
            let _ = SetEvent(self.handle);
        }
    }

    /// Block until signaled or the timeout expires. Returns true if
    /// signaled, false on timeout. `None` timeout blocks indefinitely.
    pub fn wait(&self, timeout: Option<Duration>) -> bool {
        let ms = match timeout {
            None => INFINITE,
            Some(d) => {
                let total_ms = d.as_millis();
                if total_ms > u32::MAX as u128 - 1 {
                    INFINITE
                } else {
                    total_ms as u32
                }
            }
        };
        let r = unsafe { WaitForSingleObject(self.handle, ms) };
        r == WAIT_OBJECT_0
    }
}

impl Drop for EventSignal {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.handle);
        }
    }
}

unsafe impl Send for EventSignal {}
unsafe impl Sync for EventSignal {}
