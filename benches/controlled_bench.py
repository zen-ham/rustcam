"""Controlled benchmark with a status window + Win+D minimize all.

The previous bench results were unreliable because the user keeps using
their PC while the bench runs. This harness:

1. Pops a status window on the TOP monitor (the one with the lowest
   screen-Y top edge), shows "Starting benchmark in N..." for 3 seconds
   so the user can finish whatever they're doing
2. Triggers Win+D (via `pydirectinput`) to minimize all windows for a
   quiet desktop
3. Runs the bench, updating the status window with current step + a
   progress bar
4. Shows "Benchmark complete" for 3 seconds at the end so the user
   knows when they can use the PC again
5. Closes the status window

The bench itself runs in a worker thread; tkinter stays on the main
thread. Results are written to JSON and a fresh chart.
"""
import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

import numpy as np
import xxhash

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLIP_DEMO = os.path.join(ROOT, "perf_lab", "flip_demo.exe")
MOVER = os.path.join(ROOT, "perf_lab", "mover.py")

DURATION_S = 4.0
WARMUP_S = 0.5
REPEATS = 3


# ----- monitor enumeration (find the top monitor) ------------------------

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", _RECT),
                ("rcWork", _RECT),
                ("dwFlags", wintypes.DWORD)]


def enumerate_monitors():
    user32 = ctypes.windll.user32
    mons = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(_RECT), wintypes.LPARAM,
    )

    def cb(hmon, hdc, rect, data):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            mons.append((r.left, r.top, r.right, r.bottom,
                         bool(mi.dwFlags & 1)))
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return mons


def top_monitor_rect():
    """Return the screen rect of the topmost monitor (lowest y_top)."""
    mons = enumerate_monitors()
    if not mons:
        return (100, 100, 700, 350)
    return min(mons, key=lambda m: m[1])[:4]


# ----- the status window -------------------------------------------------

class StatusWin:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("rustcam benchmark")
        # Position on top monitor
        l, t, r, b = top_monitor_rect()
        w, h = 640, 220
        x = l + ((r - l) - w) // 2
        y = t + 60
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#0a0a0f")
        # Resist accidental close while bench is running
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        title = tk.Label(
            self.root, text="rustcam benchmark",
            font=("Segoe UI", 18, "bold"),
            fg="#7CD992", bg="#0a0a0f",
        )
        title.pack(pady=(20, 4))

        self.status = tk.Label(
            self.root, text="getting ready...",
            font=("Segoe UI", 12),
            fg="#dddddd", bg="#0a0a0f",
            wraplength=600,
        )
        self.status.pack(pady=2)

        self.sub = tk.Label(
            self.root, text="",
            font=("Consolas", 10),
            fg="#888899", bg="#0a0a0f",
            wraplength=600,
        )
        self.sub.pack(pady=2)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("g.Horizontal.TProgressbar",
                        background="#4C9F38", troughcolor="#1f1f28",
                        bordercolor="#1f1f28", lightcolor="#4C9F38",
                        darkcolor="#4C9F38")
        self.bar = ttk.Progressbar(
            self.root, orient="horizontal", length=520, mode="determinate",
            style="g.Horizontal.TProgressbar", maximum=100,
        )
        self.bar.pack(pady=14)

        self.foot = tk.Label(
            self.root, text="",
            font=("Consolas", 9),
            fg="#666677", bg="#0a0a0f",
        )
        self.foot.pack(pady=2)

        self.root.update()

    def set(self, msg, sub="", pct=None, foot=""):
        self.status.configure(text=msg)
        self.sub.configure(text=sub)
        self.foot.configure(text=foot)
        if pct is not None:
            self.bar.configure(value=max(0, min(100, pct)))
        self.root.update()

    def close(self):
        try: self.root.destroy()
        except Exception: pass


# ----- bench primitives --------------------------------------------------

def fingerprint(arr):
    if arr is None:
        return None
    h = xxhash.xxh3_64(); h.update(arr); return h.digest()


def _single_run(capture_fn, duration_s, warmup_s):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        capture_fn()
    seen = set(); valid = 0; calls = 0
    last_fp = None; changed = 0; valid_minus_1 = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s:
        f = capture_fn()
        calls += 1
        if f is None: continue
        valid += 1
        fp = fingerprint(f)
        seen.add(fp)
        if last_fp is not None:
            valid_minus_1 += 1
            if fp != last_fp: changed += 1
        last_fp = fp
    el = time.perf_counter() - t0
    return dict(
        unique_fps=len(seen)/el, valid_fps=valid/el, call_fps=calls/el,
        pct_changed=(100.0*changed/valid_minus_1) if valid_minus_1 else 0.0,
        duration_s=el,
    )


def open_capturer(name):
    if name == "rustcam_nocursor":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        return ("rustcam grab(cursor=False)", lambda: cap.grab(timeout_ms=30), cap.close)
    if name == "rustcam_cursor":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=True)
        return ("rustcam grab(cursor=True)", lambda: cap.grab(timeout_ms=30), cap.close)
    if name == "rustcam_bg":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        cap.start(target_fps=0, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try: cap.stop()
            finally: cap.close()
        return ("rustcam start/get_latest_frame",
                lambda: cap.get_latest_frame(timeout_ms=30), teardown)
    if name == "bettercam_start":
        import bettercam
        cam = bettercam.create(output_idx=0)
        cam.start(target_fps=200, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try: cam.stop()
            finally: cam.release()
        return ("bettercam .start/.get_latest_frame", cam.get_latest_frame, teardown)
    if name == "dxcam_start":
        import dxcam
        cam = dxcam.create(output_idx=0)
        cam.start(target_fps=200, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try: cam.stop()
            finally: cam.release()
        return ("dxcam .start/.get_latest_frame", cam.get_latest_frame, teardown)
    if name == "mss":
        import mss
        sct = mss.mss()
        monitor = sct.monitors[1]
        return ("mss", lambda: np.array(sct.grab(monitor)), sct.close)
    raise SystemExit(f"unknown capturer {name!r}")


CAPTURERS = [
    "rustcam_nocursor",
    "rustcam_cursor",
    "rustcam_bg",
    "bettercam_start",
    "dxcam_start",
    "mss",
]


def bench_one(name, q_status, idx, total_caps, stim_label):
    """Run REPEATS isolated trials of `name`. Updates UI via `q_status`."""
    try:
        label, fn, teardown = open_capturer(name)
    except Exception as e:
        return dict(name=name, error=str(e))
    runs = []
    try:
        for r in range(REPEATS):
            q_status.put(("step", f"{stim_label}: {label}",
                          f"run {r+1}/{REPEATS}",
                          ((idx + (r+0.0)/REPEATS) / total_caps) * 100))
            res = _single_run(fn, DURATION_S, WARMUP_S if r == 0 else 0.2)
            runs.append(res)
    finally:
        try: teardown()
        except Exception: pass
    by_v = sorted(runs, key=lambda r: r["valid_fps"])
    med = by_v[len(by_v) // 2]
    valids = [r["valid_fps"] for r in runs]
    return dict(name=label, key=name,
                unique_fps=med["unique_fps"], valid_fps=med["valid_fps"],
                pct_changed=med["pct_changed"],
                valid_runs=valids, valid_min=min(valids), valid_max=max(valids))


# ----- stimulus subprocess helpers ---------------------------------------

def start_flip_demo():
    return subprocess.Popen([FLIP_DEMO],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_mover(secs):
    return subprocess.Popen([sys.executable, MOVER, str(secs)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_proc(p):
    if p is None: return
    try: p.terminate()
    except Exception: pass
    try: p.wait(timeout=2)
    except Exception:
        try: p.kill()
        except Exception: pass


# ----- save/restore visible-window state ---------------------------------
#
# Win+D would close our status window too, and there's no way to know
# which windows were already minimized before. So: enumerate every visible
# top-level window that isn't already minimized (skipping our own status
# window by HWND), minimize each one, save the HWND list. On exit, restore
# every saved HWND. The user's desktop ends up exactly as they left it.

import ctypes as _ctypes

_SW_MINIMIZE = 6
_SW_RESTORE = 9
_GW_OWNER = 4


def _window_title(hwnd):
    u = _ctypes.windll.user32
    n = u.GetWindowTextLengthW(hwnd)
    if n == 0:
        return ""
    buf = _ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _is_real_app_window(hwnd, exclude_hwnds, exclude_titles):
    """Filter out tooltips, ghost windows, the taskbar, etc.

    Visible, not iconic, no owner (popup), has a title, and not in the
    exclude lists (which catch our status window even if winfo_id gave us
    a child HWND)."""
    u = _ctypes.windll.user32
    if int(hwnd) in exclude_hwnds:
        return False
    if not u.IsWindowVisible(hwnd):
        return False
    if u.IsIconic(hwnd):
        return False
    if u.GetWindow(hwnd, _GW_OWNER):
        return False
    title = _window_title(hwnd)
    if not title:
        return False
    for t in exclude_titles:
        if t and t in title:
            return False
    return True


def _enumerate_app_windows(exclude_hwnds, exclude_titles):
    user32 = _ctypes.windll.user32
    EnumWindowsProc = _ctypes.WINFUNCTYPE(
        _ctypes.c_int, wintypes.HWND, wintypes.LPARAM,
    )
    out = []

    def cb(hwnd, lparam):
        if _is_real_app_window(hwnd, exclude_hwnds, exclude_titles):
            out.append(int(hwnd))
        return 1

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def minimize_all_except(exclude_hwnds, exclude_titles=("rustcam benchmark",)):
    """Returns the list of HWNDs we minimized (for later restore)."""
    user32 = _ctypes.windll.user32
    exc_set = {int(h) for h in (exclude_hwnds if isinstance(exclude_hwnds, (list, tuple, set)) else [exclude_hwnds])}
    hwnds = _enumerate_app_windows(exc_set, exclude_titles)
    minimized = []
    for h in hwnds:
        if user32.ShowWindow(h, _SW_MINIMIZE):
            minimized.append(h)
    return minimized


def restore_windows(hwnds):
    """Restore in REVERSE Z-order so the originally-topmost window ends up
    topmost again.

    `EnumWindows` enumerates top-to-bottom. We minimized them in that order
    (top first). When you `ShowWindow(SW_RESTORE)` a window, it comes back
    on top of the stack. So if we restore in the same top-to-bottom order,
    the originally-bottom window ends up on top of everything else.
    Iterating in REVERSED order puts each restore on top of the previous
    restore, recreating the original Z-order. The originally-topmost
    window is restored last and lands on top.
    """
    user32 = _ctypes.windll.user32
    for h in reversed(hwnds):
        try:
            user32.ShowWindow(h, _SW_RESTORE)
        except Exception:
            pass


# ----- the worker --------------------------------------------------------

def bench_worker(q_status, out_results, status_hwnd):
    minimized_hwnds = []
    try:
        # Countdown so user can finish what they're doing
        for s in (3, 2, 1):
            q_status.put(("step", f"Starting benchmark in {s}...",
                          "your open windows will be minimized + restored after",
                          0.0))
            time.sleep(1.0)

        q_status.put(("step", "minimizing your visible windows...",
                      "they'll all be restored when the bench finishes", 1.0))
        minimized_hwnds = minimize_all_except(status_hwnd)
        q_status.put(("step", "minimizing your visible windows...",
                      f"saved {len(minimized_hwnds)} windows to restore later", 1.5))
        time.sleep(0.7)

        results = {"flip_demo": [], "mover_py": []}

        # ---- flip_demo round ----
        q_status.put(("step", "starting flip_demo (controlled 180 fps source)...",
                      "", 2.0))
        flip = start_flip_demo()
        time.sleep(1.5)
        try:
            for i, name in enumerate(CAPTURERS):
                r = bench_one(name, q_status, i, len(CAPTURERS) * 2,
                              "flip_demo")
                results["flip_demo"].append(r)
        finally:
            kill_proc(flip)
            time.sleep(0.5)

        # ---- mover.py round ----
        q_status.put(("step", "starting mover.py (orbital window)...",
                      "this is the realistic-content test", 50.0))
        # generous budget so it stays alive through all measurements
        per_cap = REPEATS * DURATION_S + WARMUP_S + 4.0
        mover_budget = per_cap * len(CAPTURERS) + 8.0
        mover = start_mover(mover_budget)
        time.sleep(1.5)
        try:
            for i, name in enumerate(CAPTURERS):
                r = bench_one(name, q_status, len(CAPTURERS) + i,
                              len(CAPTURERS) * 2, "mover.py")
                results["mover_py"].append(r)
        finally:
            kill_proc(mover)
            time.sleep(0.5)

        out_results.update(results)
        q_status.put(("step", "writing results...", "", 99.0))

        raw_path = os.path.join(ROOT, "docs", "benchmark_raw.json")
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "w") as f:
            json.dump(results, f, indent=2)

        _render_chart(results)

        # End screen + restore windows
        q_status.put(("step", "restoring your windows...",
                      f"bringing back {len(minimized_hwnds)} windows", 100.0))
        restore_windows(minimized_hwnds)
        minimized_hwnds = []  # so the `finally` doesn't restore twice
        for s in (3, 2, 1):
            q_status.put(("step", f"benchmark complete - closing in {s}",
                          f"results in docs/benchmark_raw.json", 100.0))
            time.sleep(1.0)
    except Exception as e:
        import traceback
        q_status.put(("step", f"bench errored: {e}",
                      traceback.format_exc()[:300], 0.0))
        time.sleep(4)
    finally:
        # Belt-and-braces: if the happy path didn't fully restore, do it now.
        if minimized_hwnds:
            restore_windows(minimized_hwnds)
        q_status.put(("done", None, None, None))


def _render_chart(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    flip = results.get("flip_demo", [])
    mover = results.get("mover_py", [])
    labels = [r["name"] for r in flip]

    def pull(rs, attr):
        return [next((r[attr] for r in rs if r["name"] == n), 0.0) for n in labels]

    flip_v = pull(flip, "valid_fps")
    mover_v = pull(mover, "valid_fps")
    flip_min = pull(flip, "valid_min")
    flip_max = pull(flip, "valid_max")
    mover_min = pull(mover, "valid_min")
    mover_max = pull(mover, "valid_max")

    x = np.arange(len(labels))
    w = 0.38
    fig, ax = plt.subplots(figsize=(12, 6.5))
    err_f = [[max(0, v - mn) for v, mn in zip(flip_v, flip_min)],
             [max(0, mx - v) for v, mx in zip(flip_v, flip_max)]]
    err_m = [[max(0, v - mn) for v, mn in zip(mover_v, mover_min)],
             [max(0, mx - v) for v, mx in zip(mover_v, mover_max)]]
    ax.bar(x - w/2, flip_v, w, label="flip_demo (controlled 180 fps source)",
           color="#4C9F38", yerr=err_f, capsize=3,
           error_kw={"ecolor": "#222", "lw": 0.7})
    ax.bar(x + w/2, mover_v, w, label="mover.py (orbital window)",
           color="#1f77b4", yerr=err_m, capsize=3,
           error_kw={"ecolor": "#222", "lw": 0.7})
    ax.axhline(180, color="grey", linestyle="--", linewidth=1.0,
               alpha=0.7, label="180 Hz monitor refresh")
    ax.set_ylabel("frames-per-second delivered (valid grabs, median of 3 runs)")
    ax.set_title(
        "rustcam vs bettercam vs dxcam vs mss (controlled-bench: Win+D minimize, top-monitor status)\n"
        f"each bar = median of {REPEATS} runs ({DURATION_S}s each); error bars = min/max",
        fontsize=11,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9, rotation=35, ha="right",
                       rotation_mode="anchor")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    for vals, off in ((flip_v, -w/2), (mover_v, w/2)):
        for xi, v in zip(x, vals):
            ax.text(xi + off, v + 2, f"{v:.0f}",
                    ha="center", va="bottom", fontsize=8.5)
    ax.legend(loc="upper right", fontsize=9)
    plt.tight_layout()
    out = os.path.join(ROOT, "docs", "benchmark.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    plt.savefig(out, dpi=140)
    print(f"chart -> {out}")


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)

    win = StatusWin()
    # Get the OS-level HWND of the status window so the worker can exclude
    # it from the minimize-everything sweep.
    status_hwnd = win.root.winfo_id()
    q_status = queue.Queue()
    out_results = {}

    th = threading.Thread(
        target=bench_worker, args=(q_status, out_results, status_hwnd),
        daemon=True,
    )
    th.start()

    def pump():
        try:
            while True:
                tag, msg, sub, pct = q_status.get_nowait()
                if tag == "done":
                    win.close()
                    return
                win.set(msg, sub=sub or "", pct=pct)
        except queue.Empty:
            pass
        win.root.after(50, pump)

    win.root.after(50, pump)
    win.root.mainloop()


if __name__ == "__main__":
    main()
