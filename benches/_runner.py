"""Single-capturer bench runner. Spawned as a subprocess by
flip_demo_bench.py so each capturer gets a fresh Python interpreter,
fresh DDA state, and no cross-contamination from previous tests.

Usage:
  python _runner.py <capturer_name> <duration_s> <warmup_s> <repeats>

Prints one line of JSON per run, then one final JSON line with the
median result (caller picks that up).
"""
import ctypes
import hashlib
import json
import sys
import time

import numpy as np
import xxhash


def fingerprint(arr):
    if arr is None:
        return None
    h = xxhash.xxh3_64()
    h.update(arr)
    return h.digest()


def _single_run(capture_fn, duration_s, warmup_s):
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


def open_capturer(name):
    """Return (label, capture_fn, teardown_fn). Each is isolated."""
    if name == "rustcam_nocursor":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        return (
            "rustcam grab(cursor=False)",
            lambda: cap.grab(timeout_ms=30),
            cap.close,
        )
    if name == "rustcam_cursor":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=True)
        return (
            "rustcam grab(cursor=True)",
            lambda: cap.grab(timeout_ms=30),
            cap.close,
        )
    if name == "rustcam_bg":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        cap.start(target_fps=0, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try:
                cap.stop()
            finally:
                cap.close()
        return (
            "rustcam start/get_latest_frame",
            lambda: cap.get_latest_frame(timeout_ms=30),
            teardown,
        )
    if name == "rustcam_gpu":
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        seq = [0]
        def _g():
            t = cap.grab_gpu(timeout_ms=30)
            if t is None:
                return None
            t.close()
            seq[0] += 1
            return np.full((256, 1, 3), seq[0] & 0xFF, dtype=np.uint8)
        return ("rustcam grab_gpu (no readback)", _g, cap.close)
    if name == "bettercam_start":
        import bettercam
        cam = bettercam.create(output_idx=0)
        cam.start(target_fps=200, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try:
                cam.stop()
            finally:
                cam.release()
        return ("bettercam .start/.get_latest_frame", cam.get_latest_frame, teardown)
    if name == "dxcam_start":
        import dxcam
        cam = dxcam.create(output_idx=0)
        cam.start(target_fps=200, video_mode=False)
        time.sleep(0.5)
        def teardown():
            try:
                cam.stop()
            finally:
                cam.release()
        return ("dxcam .start/.get_latest_frame", cam.get_latest_frame, teardown)
    if name == "mss":
        import mss
        sct = mss.mss()
        monitor = sct.monitors[1]
        def _g():
            return np.array(sct.grab(monitor))
        return ("mss", _g, sct.close)
    raise SystemExit(f"unknown capturer {name!r}")


def main():
    name = sys.argv[1]
    duration_s = float(sys.argv[2])
    warmup_s = float(sys.argv[3])
    repeats = int(sys.argv[4])

    ctypes.windll.shcore.SetProcessDpiAwareness(2)

    label, fn, teardown = open_capturer(name)
    runs = []
    try:
        for i in range(repeats):
            r = _single_run(fn, duration_s, warmup_s if i == 0 else 0.2)
            r["run_idx"] = i
            runs.append(r)
            print(json.dumps({"run": r, "label": label}), flush=True)
    finally:
        try:
            teardown()
        except Exception:
            pass

    by_valid = sorted(runs, key=lambda r: r["valid_fps"])
    med = by_valid[len(by_valid) // 2]
    valids = [r["valid_fps"] for r in runs]
    summary = dict(
        name=label,
        unique_fps=med["unique_fps"],
        valid_fps=med["valid_fps"],
        call_fps=med["call_fps"],
        pct_changed=med["pct_changed"],
        uniques=med["uniques"],
        duration_s=med["duration_s"],
        valid_runs=valids,
        valid_min=min(valids),
        valid_max=max(valids),
    )
    print(json.dumps({"summary": summary}), flush=True)


if __name__ == "__main__":
    main()
