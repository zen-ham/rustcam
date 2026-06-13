"""Run rustcam nocursor -> rustcam cursor in sequence on mover.py,
to see if there's a degradation when cursor=True follows nocursor.

Then re-run cursor=True after a brief cooldown to see if it
recovers.
"""
import ctypes
import json
import os
import subprocess
import sys
import time

import numpy as np
import xxhash

HERE = os.path.dirname(os.path.abspath(__file__))
MOVER = os.path.join(HERE, "mover.py")

DURATION_S = 4.0
WARMUP_S = 0.5


def start_mover(secs):
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
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=2.0)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def bench(name, capture_fn, duration_s=DURATION_S, warmup_s=WARMUP_S):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        capture_fn()
    latencies = []
    valid = 0
    calls = 0
    seen = set()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s:
        ta = time.perf_counter()
        f = capture_fn()
        tb = time.perf_counter()
        latencies.append(int((tb - ta) * 1_000_000))
        calls += 1
        if f is None:
            continue
        valid += 1
        try:
            h = xxhash.xxh3_64()
            h.update(f)
            seen.add(h.digest())
        except Exception:
            pass
    elapsed = time.perf_counter() - t0
    valid_fps = valid / elapsed
    unique_fps = len(seen) / elapsed
    s = sorted(latencies)
    n = len(s)
    if n:
        mean = sum(s) / n
        med = s[n // 2]
        p99 = s[min(n - 1, int(0.99 * n))]
    else:
        mean = med = p99 = 0
    print(f"  {name:<40} valid={valid_fps:6.1f}fps  unique={unique_fps:6.1f}fps  "
          f"mean={mean/1000:5.2f}ms med={med/1000:5.2f}ms p99={p99/1000:5.2f}ms",
          flush=True)
    return dict(name=name, valid_fps=valid_fps, unique_fps=unique_fps,
                mean_us=mean, median_us=med, p99_us=p99, n=n, valid=valid,
                latencies_us=latencies)


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    stim = start_mover(120.0)
    time.sleep(0.7)
    results = []
    try:
        import rustcam

        # Round 1: fresh, cursor=False
        cap = rustcam.Capturer(output=0, cursor=False)
        results.append(bench("R1 rustcam cursor=False (fresh)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(0.7)

        # Round 2: cursor=True, immediately after nocursor
        cap = rustcam.Capturer(output=0, cursor=True)
        results.append(bench("R2 rustcam cursor=True (after no-c)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(0.7)

        # Round 3: another cursor=True, after the previous
        cap = rustcam.Capturer(output=0, cursor=True)
        results.append(bench("R3 rustcam cursor=True (after cur)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(2.0)

        # Round 4: after a longer pause, cursor=True
        cap = rustcam.Capturer(output=0, cursor=True)
        results.append(bench("R4 rustcam cursor=True (after 2s gap)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(0.7)

        # Round 5: cursor=False again to see if it recovers
        cap = rustcam.Capturer(output=0, cursor=False)
        results.append(bench("R5 rustcam cursor=False (after cur)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
    finally:
        kill_proc(stim)

    out = os.path.join(HERE, "replicate_rustcam_only_results.json")
    with open(out, "w") as fp:
        json.dump(results, fp)
    print(f"\nresults -> {out}")


if __name__ == "__main__":
    main()
