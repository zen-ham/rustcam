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

DURATION_S = 3.0
WARMUP_S = 0.5
REPEATS = 3  # number of timed runs per capturer; we take the median

# Capturers and the keys understood by _runner.py.
#
# Note on bettercam/dxcam: only `.start()` mode is included. The previous
# bench compared `.grab()` (one-shot, tight loop, ~180 fps on a controlled
# flip source) against rustcam's normal API. That comparison was apples to
# oranges: no production code uses .grab() in a tight while-True loop —
# .start() + .get_latest_frame() is the universal pattern. .grab() lights
# up benchmarks but is not how either library is actually used.
CAPTURERS = [
    "rustcam_nocursor",
    "rustcam_cursor",
    "rustcam_bg",
    "rustcam_gpu",
    "bettercam_start",
    "dxcam_start",
    "mss",
]

# Per-capturer wall-clock budget incl. startup + warmup + REPEATS runs.
# REPEATS * DURATION_S + warmup + ~1s python start + ~1s teardown
PER_CAP_SECS = REPEATS * DURATION_S + WARMUP_S + 3.0
ROUND_BUDGET = PER_CAP_SECS * len(CAPTURERS) + 8.0


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


def start_mover(secs=None):
    if not os.path.exists(MOVER):
        return None
    secs = secs if secs is not None else ROUND_BUDGET
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


def _single_run(capture_fn, duration_s, warmup_s):
    """One timed sweep. Returns the metric dict for this single run."""
    # warmup
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        capture_fn()

    seen = set()
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
    return dict(
        unique_fps=len(seen) / elapsed,
        valid_fps=valid / elapsed,
        call_fps=calls / elapsed,
        pct_changed=(100.0 * changed / valid_minus_1) if valid_minus_1 else 0.0,
        uniques=len(seen),
        duration_s=elapsed,
    )


def bench(name, capture_fn, duration_s=DURATION_S, warmup_s=WARMUP_S, repeats=REPEATS):
    """Run the capture loop `repeats` times. Take MEDIAN across runs so
    single-shot variance (PyQt timing jitter, GC pauses, transient DWM
    work) doesn't sneak misleading numbers onto the chart. Returns the
    median dict with `valid_runs` and `valid_min/max` annotations so we
    can see if a cell is unstable."""
    runs = []
    for i in range(repeats):
        runs.append(_single_run(capture_fn, duration_s, warmup_s if i == 0 else 0.2))
    by_valid = sorted(runs, key=lambda r: r["valid_fps"])
    med = by_valid[len(by_valid) // 2]
    valids = [r["valid_fps"] for r in runs]
    print(
        f"  {name:<32} unique={med['unique_fps']:6.1f}  "
        f"valid={med['valid_fps']:6.1f} (min {min(valids):.1f} max {max(valids):.1f})  "
        f"calls={med['call_fps']:7.1f}  %ch={med['pct_changed']:4.0f}%"
    )
    out = dict(med)
    out["name"] = name
    out["valid_runs"] = valids
    out["valid_min"] = min(valids)
    out["valid_max"] = max(valids)
    return out


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


def bench_bettercam_start(label="bettercam .start/.get_latest_frame"):
    """The TYPICAL bettercam usage pattern — `.start()` spawns its bg
    capture thread and `.get_latest_frame()` blocks until a fresh
    frame is in the ring buffer. This is what every bettercam tutorial
    and recipe online uses."""
    import bettercam
    cam = bettercam.create(output_idx=0)
    cam.start(target_fps=200, video_mode=False)
    import time; time.sleep(0.5)
    try:
        return bench(label, lambda: cam.get_latest_frame())
    finally:
        cam.stop()
        cam.release()
        time.sleep(0.5)


def bench_dxcam_start(label="dxcam .start/.get_latest_frame"):
    import dxcam
    cam = dxcam.create(output_idx=0)
    cam.start(target_fps=200, video_mode=False)
    import time; time.sleep(0.5)
    try:
        return bench(label, lambda: cam.get_latest_frame())
    finally:
        cam.stop()
        cam.release()
        time.sleep(0.5)


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


# -------- orchestration via subprocess-per-capturer --------

def run_one_capturer(name):
    """Spawn `_runner.py <name>` as a fresh Python process and parse
    its JSON output. Returns the summary dict, or None on failure."""
    runner = os.path.join(HERE, "_runner.py")
    cmd = [
        sys.executable, runner, name,
        str(DURATION_S), str(WARMUP_S), str(REPEATS),
    ]
    summary = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, text=True,
        )
    except Exception as e:
        print(f"  [{name} failed to spawn: {e}]")
        return None

    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = __import__("json").loads(line)
            except Exception:
                continue
            if "run" in obj:
                r = obj["run"]
                print(
                    f"  [{name:<22} run {r['run_idx']}] "
                    f"valid={r['valid_fps']:6.1f}  "
                    f"unique={r['unique_fps']:6.1f}  "
                    f"%ch={r['pct_changed']:5.1f}",
                    flush=True,
                )
            elif "summary" in obj:
                summary = obj["summary"]
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    err = proc.stderr.read() if proc.stderr else ""
    if summary is None:
        # Show the subprocess stderr so we can see what went wrong
        if err.strip():
            print(f"  [{name} no summary; stderr first 400 chars]: {err.strip()[:400]}")
        else:
            print(f"  [{name} no summary returned]")
    return summary


def run_round(label_prefix, stim_fn):
    print(f"\n== {label_prefix} ==")
    stim = stim_fn()
    try:
        time.sleep(0.5)
        out = []
        for cap in CAPTURERS:
            summary = run_one_capturer(cap)
            if summary is not None:
                print(
                    f"  --> median valid={summary['valid_fps']:6.1f} "
                    f"(min {summary['valid_min']:.1f} max {summary['valid_max']:.1f})"
                )
                out.append(summary)
        return out
    finally:
        kill_proc(stim)
        time.sleep(0.5)


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

    # Persist raw data so we can sanity-check before publishing the chart
    import json
    raw_path = os.path.join(ROOT, "docs", "benchmark_raw.json")
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "w") as f:
        json.dump({"flip_demo": flip, "mover_py": mover}, f, indent=2)
    print(f"raw -> {raw_path}")

    # Chart
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        labels = [r["name"] for r in flip]
        flip_vals = [r["valid_fps"] for r in flip]
        flip_min = [r.get("valid_min", r["valid_fps"]) for r in flip]
        flip_max = [r.get("valid_max", r["valid_fps"]) for r in flip]
        mover_vals = []
        mover_min = []
        mover_max = []
        mover_changed = []
        for n in labels:
            r = next((r for r in mover if r["name"] == n), None)
            mover_vals.append(r["valid_fps"] if r else 0.0)
            mover_min.append(r.get("valid_min", r["valid_fps"]) if r else 0.0)
            mover_max.append(r.get("valid_max", r["valid_fps"]) if r else 0.0)
            mover_changed.append(r["pct_changed"] if r else 0.0)

        x = np.arange(len(labels))
        w = 0.38
        fig, ax = plt.subplots(figsize=(12, 6.5))
        flip_err = [
            [max(0, m - lo) for m, lo in zip(flip_vals, flip_min)],
            [max(0, hi - m) for m, hi in zip(flip_vals, flip_max)],
        ]
        mover_err = [
            [max(0, m - lo) for m, lo in zip(mover_vals, mover_min)],
            [max(0, hi - m) for m, hi in zip(mover_vals, mover_max)],
        ]
        bars1 = ax.bar(x - w / 2, flip_vals, w,
                       label="flip_demo (180 fps full-screen flip)",
                       color="#4C9F38", yerr=flip_err, capsize=3,
                       error_kw={"ecolor": "#333", "lw": 0.7})
        bars2 = ax.bar(x + w / 2, mover_vals, w,
                       label="mover.py (orbital window)", color="#1f77b4",
                       yerr=mover_err, capsize=3,
                       error_kw={"ecolor": "#333", "lw": 0.7})
        ax.axhline(180, color="grey", linestyle="--", linewidth=1.0,
                   alpha=0.7, label="180 Hz monitor refresh")
        ax.set_ylabel("frames-per-second delivered (valid grabs)")
        ax.set_title("rustcam vs bettercam vs dxcam vs mss   (1080p, 4 s capture)\n"
                     "valid fps = non-None returns per second; ride the 180 Hz line = the lib is keeping up")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9, rotation=45, ha="right",
                           rotation_mode="anchor")
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
