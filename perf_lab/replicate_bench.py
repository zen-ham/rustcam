"""Replicate the flip_demo_bench.py mover.py round to see if rustcam
cursor=True specifically slows down when run in that sequence.

Order matches benches/flip_demo_bench.py run_round:
    rustcam nocursor
    rustcam cursor
    rustcam bg
    rustcam gpu
    bettercam grab
    bettercam start
    dxcam grab
    dxcam start
    mss

Times each. Saves results to replicate_bench_results.json.
"""
import ctypes
import json
import os
import signal
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
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
    import xxhash
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
    print(f"  {name:<32} valid={valid_fps:6.1f}fps  unique={unique_fps:6.1f}fps  "
          f"mean={mean/1000:5.2f}ms med={med/1000:5.2f}ms p99={p99/1000:5.2f}ms",
          flush=True)
    return dict(name=name, valid_fps=valid_fps, unique_fps=unique_fps,
                mean_us=mean, median_us=med, p99_us=p99,
                n=n, valid=valid)


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    stim = start_mover(DURATION_S * 11)
    time.sleep(0.5)
    results = []
    try:
        # 1. rustcam nocursor
        import rustcam
        cap = rustcam.Capturer(output=0, cursor=False)
        results.append(bench("rustcam grab(cursor=False)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(0.7)

        # 2. rustcam cursor=True
        cap = rustcam.Capturer(output=0, cursor=True)
        results.append(bench("rustcam grab(cursor=True)",
                             lambda: cap.grab(timeout_ms=30)))
        cap.close()
        time.sleep(0.7)

        # 3. rustcam bg
        cap = rustcam.Capturer(output=0, cursor=False)
        cap.start(target_fps=0, video_mode=False)
        results.append(bench("rustcam start/get_latest_frame",
                             lambda: cap.get_latest_frame(timeout_ms=30)))
        cap.stop()
        cap.close()
        time.sleep(0.7)

        # 4. rustcam gpu
        cap = rustcam.Capturer(output=0, cursor=False)
        seq = [0]
        def _g():
            t = cap.grab_gpu(timeout_ms=30)
            if t is None:
                return None
            t.close()
            seq[0] += 1
            return np.full((256, 1, 3), seq[0] & 0xFF, dtype=np.uint8)
        results.append(bench("rustcam grab_gpu", _g))
        cap.close()
        time.sleep(0.7)

        # 5+6. bettercam
        try:
            import bettercam
            cam = bettercam.create(output_idx=0)
            results.append(bench("bettercam .grab()", lambda: cam.grab()))
            cam.release()
            time.sleep(0.5)
            cam = bettercam.create(output_idx=0)
            cam.start(target_fps=200, video_mode=False)
            time.sleep(0.5)
            results.append(bench("bettercam start", lambda: cam.get_latest_frame()))
            cam.stop()
            cam.release()
            time.sleep(0.5)
        except Exception as e:
            print(f"  bettercam: {e}")

        # 7+8. dxcam
        try:
            import dxcam
            cam = dxcam.create(output_idx=0)
            results.append(bench("dxcam .grab()", lambda: cam.grab()))
            cam.release()
            time.sleep(0.5)
            cam = dxcam.create(output_idx=0)
            cam.start(target_fps=200, video_mode=False)
            time.sleep(0.5)
            results.append(bench("dxcam start", lambda: cam.get_latest_frame()))
            cam.stop()
            cam.release()
            time.sleep(0.5)
        except Exception as e:
            print(f"  dxcam: {e}")

        # 9. mss
        try:
            import mss
            sct = mss.mss()
            monitor = sct.monitors[1]
            results.append(bench("mss", lambda: np.array(sct.grab(monitor))))
            sct.close()
        except Exception as e:
            print(f"  mss: {e}")

    finally:
        kill_proc(stim)

    out = os.path.join(HERE, "replicate_bench_results.json")
    with open(out, "w") as fp:
        json.dump(results, fp)
    print(f"\nresults -> {out}")


if __name__ == "__main__":
    main()
