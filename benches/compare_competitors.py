"""Apples-to-apples capturer benchmark.

Runs each library against the SAME moving stimulus for the same wall-clock
duration. Counts unique frames using md5 over every 8th row of the captured
buffer. The "unique fps" number is what actually matters for screen recording
and ML pipelines; container-reported fps numbers can include duplicates.

Stimulus: a daemon thread that pumps `SetCursorPos` as fast as Python can,
which forces DDA to mark every refresh as a unique frame.

The benchmark intentionally avoids the full window-mover stimulus from the
zentape `perf_lab/` so it can run in a headless-ish dev box. The unique-fps
upper bound here is the monitor refresh rate.
"""
import ctypes
import hashlib
import os
import subprocess
import sys
import threading
import time

import numpy as np


def start_stimulus():
    """Spawn the tkinter mover window as a child process. Returns the Popen."""
    here = os.path.dirname(os.path.abspath(__file__))
    mover = os.path.join(os.path.dirname(here), "perf_lab", "mover.py")
    p = subprocess.Popen(
        [sys.executable, mover],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)  # let the window appear + tk's first redraw flush
    return p


def jitter_thread(stop_event):
    """Cursor jitter alongside the tkinter mover (so we cover both
    cursor-compositor and pixel-buffer change paths)."""
    user32 = ctypes.windll.user32
    cx, cy = 600, 400
    while not stop_event.is_set():
        for dx in range(0, 700, 3):
            if stop_event.is_set():
                return
            user32.SetCursorPos(cx + dx, cy + (dx // 3))


def fingerprint(arr):
    """md5 over every 8th row of the buffer. Cheap, content-sensitive."""
    if arr.ndim == 3:
        sub = arr[::8, :, :].tobytes()
    else:
        sub = arr[::8, :].tobytes()
    return hashlib.md5(sub).digest()


def bench(name, capture_fn, duration_s=6.0, warmup_s=0.5):
    """capture_fn: () -> ndarray|None. Returns (unique_fps, total_calls)."""
    stop = threading.Event()
    th = threading.Thread(target=jitter_thread, args=(stop,), daemon=True)
    th.start()

    # warmup
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        capture_fn()

    seen = set()
    calls = 0
    valid = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s:
        f = capture_fn()
        calls += 1
        if f is None:
            continue
        valid += 1
        fp = fingerprint(f)
        seen.add(fp)
    elapsed = time.perf_counter() - t0
    stop.set()
    th.join(timeout=1)
    unique_fps = len(seen) / elapsed
    call_fps = calls / elapsed
    valid_fps = valid / elapsed
    print(f"  {name:<28} unique={unique_fps:6.1f}fps   valid={valid_fps:6.1f}fps   calls={call_fps:6.1f}fps   ({len(seen)} uniques)")
    return {
        "name": name,
        "unique_fps": unique_fps,
        "valid_fps": valid_fps,
        "call_fps": call_fps,
        "unique_total": len(seen),
        "duration_s": elapsed,
    }


def bench_rustcam_nocursor():
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=False)
    try:
        return bench("rustcam (cursor=False)", lambda: cap.grab(timeout_ms=50))
    finally:
        cap.close()


def bench_rustcam_cursor():
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=True)
    try:
        return bench("rustcam (cursor=True)", lambda: cap.grab(timeout_ms=50))
    finally:
        cap.close()


def bench_bettercam():
    import bettercam
    cam = bettercam.create(output_idx=0)
    try:
        return bench("bettercam (grab)", lambda: cam.grab())
    finally:
        cam.release()


def bench_dxcam():
    import dxcam
    cam = dxcam.create(output_idx=0)
    try:
        return bench("dxcam (grab)", lambda: cam.grab())
    finally:
        cam.release()


def bench_mss():
    import mss
    sct = mss.mss()
    monitor = sct.monitors[1]
    def _g():
        img = sct.grab(monitor)
        return np.array(img)
    try:
        return bench("mss (grab + np.array)", _g)
    finally:
        sct.close()


def main():
    if "--quick" in sys.argv:
        global DURATION
        DURATION = 1.0
    print("== capturer comparison ==")
    print(f"  duration: 6 s per library, sequential (DDA only allows one duplication at a time)")
    print(f"  stimulus: tkinter mover window (continuous canvas redraw) + cursor jitter")
    print()
    stim = start_stimulus()
    results = []
    try:
        time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    # Run sequentially with a short cool-down so the previous capturer fully releases its duplication.
    for fn in [
        bench_rustcam_nocursor,
        bench_rustcam_cursor,
        bench_bettercam,
        bench_dxcam,
        bench_mss,
    ]:
        try:
            results.append(fn())
        except Exception as e:
            print(f"  [{fn.__name__} failed: {e}]")
        time.sleep(0.8)

    try:
        stim.terminate()
        stim.wait(timeout=2)
    except Exception:
        pass

    print()
    print("Summary (unique fps):")
    for r in results:
        print(f"  {r['name']:<28} {r['unique_fps']:6.1f}")

    # If matplotlib is available, render a chart to docs/benchmark.png
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        labels = [r["name"].split(" ")[0] for r in results]
        vals = [r["unique_fps"] for r in results]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        bars = ax.bar(labels, vals, color=["#4C9F38", "#1f77b4", "#ff7f0e", "#999"])
        ax.set_ylabel("unique frames per second")
        ax.set_title("rustcam vs bettercam vs mss   (1080p, moving cursor stimulus, 4 s capture)")
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1, f"{v:.0f}",
                    ha="center", va="bottom", fontsize=10)
        plt.tight_layout()
        out = "docs/benchmark.png"
        import os
        os.makedirs("docs", exist_ok=True)
        plt.savefig(out, dpi=130)
        print(f"\nchart saved to {out}")
    except ImportError:
        print("\nmatplotlib not installed; skip chart")


if __name__ == "__main__":
    main()
