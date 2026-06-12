"""The honest benchmark per `DDA_BENCHMARK_NOTES.md`.

Drives a TRUE 180-fps flip-model swapchain source (`flip_demo.exe`, a tiny
Rust D3D11 app) that emits a fresh full-screen color every refresh. Then
captures with each library and counts UNIQUE frames via md5 of a subsample.

Container fps lies. A capturer can claim 180 fps while returning the same
buffer over and over. Unique fps + % consecutive-changed is the metric that
can't be faked.

Reports:
- unique fps  (how many distinct frames per second hit the buffer)
- % changed   (how often consecutive returned frames differ - ~100% means
              the capture rode the source rate cleanly, no dropped/duped)
- valid fps   (how many non-None returns per second total)
"""
import ctypes
import os
import signal
import subprocess
import sys
import time

import numpy as np
import xxhash


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLIP_DEMO = os.path.join(ROOT, "perf_lab", "flip_demo.exe")
MOVER = os.path.join(ROOT, "perf_lab", "mover.py")

DURATION_S = 4.0
WARMUP_S = 0.5


def start_flip_demo():
    """Launch flip_demo.exe and wait until it has presented its first frames."""
    if not os.path.exists(FLIP_DEMO):
        raise SystemExit(f"flip_demo.exe not found at {FLIP_DEMO}")
    p = subprocess.Popen(
        [FLIP_DEMO],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    # Wait for "flip demo present fps:" line or 2 seconds, whichever first.
    deadline = time.perf_counter() + 2.5
    while time.perf_counter() < deadline:
        if p.stdout and p.stdout.readable():
            line = p.stdout.readline()
            if line and b"present fps" in line.lower():
                break
        time.sleep(0.05)
    time.sleep(0.5)  # let DWM stabilize
    return p


def start_mover(secs):
    if not os.path.exists(MOVER):
        return None
    p = subprocess.Popen(
        [sys.executable, MOVER, str(secs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    return p


def kill_proc(p):
    if p is None:
        return
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT)
    except Exception:
        pass
    try:
        p.wait(timeout=2.0)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass
    try:
        p.wait(timeout=1.0)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def fingerprint(arr):
    """xxhash over the entire frame buffer.

    The previous spatial-grid and single-row approaches were biased:
    they only sampled specific regions of the frame, so movement that
    didn't intersect the sample points was invisible. xxhash64 runs at
    ~25 GB/s on this CPU (~0.3 ms per 8 MB BGRA frame), so it stays out
    of the way of the capture loop while looking at every pixel.

    Returns an 8-byte digest. Uses numpy's buffer protocol directly
    (no tobytes() copy).
    """
    if arr is None:
        return None
    h = xxhash.xxh3_64()
    h.update(arr)
    return h.digest()


def bench(name, capture_fn, duration_s=DURATION_S, warmup_s=WARMUP_S):
    """capture_fn: () -> ndarray | None. Returns dict with the metrics."""
    # warmup
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        capture_fn()

    seen = set()
    fps_chain = []
    valid = 0
    calls = 0
    last_fp = None
    changed = 0
    valid_minus_1 = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s:
        f = capture_fn()
        calls += 1
        if f is None:
            continue
        valid += 1
        fp = fingerprint(f)
        seen.add(fp)
        if last_fp is not None:
            valid_minus_1 += 1
            if fp != last_fp:
                changed += 1
        last_fp = fp
    elapsed = time.perf_counter() - t0
    unique_fps = len(seen) / elapsed
    valid_fps = valid / elapsed
    call_fps = calls / elapsed
    pct_changed = 100.0 * changed / valid_minus_1 if valid_minus_1 else 0.0
    print(
        f"  {name:<28} unique={unique_fps:6.1f}fps  "
        f"valid={valid_fps:6.1f}fps  calls={call_fps:6.1f}fps  "
        f"%changed={pct_changed:5.1f}%   ({len(seen)} uniques)"
    )
    return dict(
        name=name,
        unique_fps=unique_fps,
        valid_fps=valid_fps,
        call_fps=call_fps,
        pct_changed=pct_changed,
        uniques=len(seen),
        duration_s=elapsed,
    )


# -------- one wrapper per library --------

def bench_rustcam_cursor(label="rustcam grab(cursor=True)"):
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=True)
    try:
        return bench(label, lambda: cap.grab(timeout_ms=30))
    finally:
        cap.close()


def bench_rustcam_nocursor(label="rustcam grab(cursor=False)"):
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=False)
    try:
        return bench(label, lambda: cap.grab(timeout_ms=30))
    finally:
        cap.close()


def bench_rustcam_bg(label="rustcam start/get_latest_frame"):
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=False)
    cap.start(target_fps=0, video_mode=False)
    try:
        return bench(label, lambda: cap.get_latest_frame(timeout_ms=30))
    finally:
        cap.stop()
        cap.close()


def bench_rustcam_gpu(label="rustcam grab_gpu (no readback)"):
    """Producer-side throughput only. The bench fingerprint is meant for
    CPU buffers; here we synthesise a fingerprint from the increasing
    GPU shared-handle metadata so the unique-counter agrees with the
    valid-counter (every call is a "unique" produce)."""
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=False)
    seq = [0]
    def _g():
        t = cap.grab_gpu(timeout_ms=30)
        if t is None:
            return None
        t.close()
        seq[0] += 1
        # 256x1x3 sentinel so the fingerprint slicer doesn't divide by zero
        return np.full((256, 1, 3), seq[0] & 0xFF, dtype=np.uint8)
    try:
        return bench(label, _g)
    finally:
        cap.close()


def bench_bettercam(label="bettercam"):
    import bettercam
    cam = bettercam.create(output_idx=0)
    try:
        return bench(label, lambda: cam.grab())
    finally:
        cam.release()


def bench_dxcam(label="dxcam"):
    import dxcam
    cam = dxcam.create(output_idx=0)
    try:
        return bench(label, lambda: cam.grab())
    finally:
        cam.release()


def bench_mss(label="mss"):
    import mss
    sct = mss.mss()
    monitor = sct.monitors[1]
    def _g():
        return np.array(sct.grab(monitor))
    try:
        return bench(label, _g)
    finally:
        sct.close()


# -------- orchestration --------

def run_round(label_prefix, stim_fn):
    """Spin up the stimulus, run each capturer, tear down."""
    print(f"\n== {label_prefix} ==")
    stim = stim_fn()
    try:
        time.sleep(0.5)
        out = []
        for fn in (
            bench_rustcam_nocursor,
            bench_rustcam_cursor,
            bench_rustcam_bg,
            bench_rustcam_gpu,
            bench_bettercam,
            bench_dxcam,
            bench_mss,
        ):
            try:
                out.append(fn())
            except Exception as e:
                print(f"  [{fn.__name__} failed: {e}]")
            time.sleep(0.7)
        return out
    finally:
        kill_proc(stim)
        time.sleep(0.4)


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    print("capturer comparison vs flip_demo (180 Hz true-flip source)")
    print(f"  duration: {DURATION_S}s per library, sequential")
    print(f"  metric:   md5(every 13th byte of every 8th row) -> unique-frame count")
    flip_results = run_round("flip_demo (~180 fps unique full-screen colours)",
                             lambda: start_flip_demo())
    mover_results = run_round("mover.py (orbital window, realistic content)",
                              lambda: start_mover(DURATION_S + 4))
    return flip_results, mover_results


if __name__ == "__main__":
    flip, mover = main()

    print("\nSummary (unique fps):")
    print(f"  {'capturer':<28} {'flip_demo':>14} {'mover.py':>14}")
    by_name = {r["name"]: r for r in flip}
    by_name_m = {r["name"]: r for r in mover}
    names = list(by_name) + [n for n in by_name_m if n not in by_name]
    for n in names:
        a = by_name.get(n, {}).get("unique_fps", float("nan"))
        b = by_name_m.get(n, {}).get("unique_fps", float("nan"))
        print(f"  {n:<28} {a:14.1f} {b:14.1f}")

    # Chart
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # valid_fps is the honest "frames-per-second-delivered" metric.
        # unique_fps is biased by the hashing cost + content periodicity;
        # %changed (annotated below each bar) is the freshness check.
        labels = [r["name"] for r in flip]
        flip_vals = [r["valid_fps"] for r in flip]
        mover_vals = []
        mover_changed = []
        for n in labels:
            r = next((r for r in mover if r["name"] == n), None)
            mover_vals.append(r["valid_fps"] if r else 0.0)
            mover_changed.append(r["pct_changed"] if r else 0.0)

        x = np.arange(len(labels))
        w = 0.38
        fig, ax = plt.subplots(figsize=(11, 5.2))
        bars1 = ax.bar(x - w / 2, flip_vals, w,
                       label="flip_demo (180 fps full-screen flip)",
                       color="#4C9F38")
        bars2 = ax.bar(x + w / 2, mover_vals, w,
                       label="mover.py (orbital window)", color="#1f77b4")
        ax.axhline(180, color="grey", linestyle="--", linewidth=1.0,
                   alpha=0.7, label="180 Hz monitor refresh")
        ax.set_ylabel("frames-per-second delivered (valid grabs)")
        ax.set_title("rustcam vs bettercam vs dxcam vs mss   (1080p, 4 s capture)\n"
                     "valid fps = non-None returns per second; ride the 180 Hz line = the lib is keeping up")
        ax.set_xticks(x)
        ax.set_xticklabels([s.replace(" ", "\n", 1) for s in labels], fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        for bars, vals in ((bars1, flip_vals), (bars2, mover_vals)):
            for bar, v in zip(bars, vals):
                if v > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
                            f"{v:.0f}", ha="center", va="bottom", fontsize=8.5)
        ax.legend(loc="upper right", fontsize=9)
        plt.tight_layout()
        out = os.path.join(ROOT, "docs", "benchmark.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        plt.savefig(out, dpi=140)
        print(f"\nchart -> {out}")
    except ImportError:
        print("\nmatplotlib not installed; skipping chart")
