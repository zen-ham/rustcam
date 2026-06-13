"""Microbench of rustcam.Capturer.grab() under flip_demo vs mover.py.

Goal: pin down WHICH stage in the cursor=True grab() path gets slow under
DWM-active (mover.py) vs DWM-quiet (flip_demo).

We can't see *inside* the Rust call from Python without rebuilding the wheel,
which the task forbids. But we CAN bracket the whole grab() and compare with
cursor=False to isolate the cursor compositing overhead, then compare under
the two stimuli to isolate the DWM-contention effect.

This script outputs a per-call latency histogram (not just mean) so we can
see if the slowdown is "every call is slow" (steady wait) or "some calls hit
a stall while others are fast" (intermittent blocking).
"""
import ctypes, math, os, signal, statistics, subprocess, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLIP_DEMO = os.path.join(HERE, "flip_demo.exe")
MOVER = os.path.join(HERE, "mover.py")

DURATION_S = 4.0
WARMUP_S = 0.5


def start_flip_demo():
    if not os.path.exists(FLIP_DEMO):
        raise SystemExit(f"flip_demo.exe not found at {FLIP_DEMO}")
    p = subprocess.Popen(
        [FLIP_DEMO],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    deadline = time.perf_counter() + 2.5
    while time.perf_counter() < deadline:
        if p.stdout and p.stdout.readable():
            line = p.stdout.readline()
            if line and b"present fps" in line.lower():
                break
        time.sleep(0.05)
    time.sleep(0.5)
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


def measure(label, cap, duration_s=DURATION_S, warmup_s=WARMUP_S):
    """Run grab() over duration_s and return per-call latencies in microseconds."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < warmup_s:
        cap.grab(timeout_ms=30)
    lats = []
    nones = 0
    valids = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s:
        s = time.perf_counter()
        f = cap.grab(timeout_ms=30)
        e = time.perf_counter()
        lats.append((e - s) * 1e6)
        if f is None:
            nones += 1
        else:
            valids += 1
    elapsed = time.perf_counter() - t0
    print(
        f"  {label:<40} n={len(lats):4}  valid={valids:4} ({valids/elapsed:6.1f}/s) "
        f"none={nones:3}  "
        f"min={min(lats):7.1f}us  median={statistics.median(lats):7.1f}us  "
        f"p95={sorted(lats)[int(len(lats)*0.95)]:7.1f}us  "
        f"max={max(lats):7.1f}us  mean={statistics.fmean(lats):7.1f}us"
    )
    return {"label": label, "lats": lats, "valid": valids, "none": nones, "elapsed": elapsed}


def bucketize(lats, edges_us):
    """Counts of latencies falling into [edges[i], edges[i+1]) buckets."""
    counts = [0] * (len(edges_us) + 1)
    for l in lats:
        placed = False
        for i, e in enumerate(edges_us):
            if l < e:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return counts


def print_histogram(label, lats):
    # Bucket edges in microseconds: roughly 0-5ms in nice increments.
    edges = [500, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000, 30000, 50000]
    counts = bucketize(lats, edges)
    print(f"  {label} latency histogram:")
    prev = 0
    for e, c in zip(edges, counts[:-1]):
        bar = "#" * min(60, c)
        print(f"    [{prev:>6}us, {e:>6}us)  {c:5}  {bar}")
        prev = e
    print(f"    [{edges[-1]:>6}us, inf   )  {counts[-1]:5}  {'#' * min(60, counts[-1])}")


def run_round(label, stim_fn, target_secs):
    print(f"\n========== {label} ==========")
    stim = stim_fn()
    try:
        import rustcam
        time.sleep(0.5)

        results = []

        # cursor=False baseline
        cap = rustcam.Capturer(output=0, cursor=False)
        try:
            r = measure("cursor=False", cap, target_secs)
            results.append(r)
        finally:
            cap.close()
        time.sleep(0.5)

        # cursor=True (the suspect)
        cap = rustcam.Capturer(output=0, cursor=True)
        try:
            r = measure("cursor=True ", cap, target_secs)
            results.append(r)
        finally:
            cap.close()

        return results
    finally:
        kill_proc(stim)
        time.sleep(0.4)


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    print("rustcam grab() per-call latency under different stimuli")
    flip = run_round("FLIP_DEMO (DWM-quiet, native flip-model 180fps)",
                     start_flip_demo, DURATION_S)
    mover = run_round("MOVER (DWM busy compositing orbital window)",
                      lambda: start_mover(DURATION_S + 4), DURATION_S)

    print("\n========== HISTOGRAMS ==========")
    for r in flip:
        print_histogram("flip_demo " + r["label"], r["lats"])
    for r in mover:
        print_histogram("mover     " + r["label"], r["lats"])

    print("\n========== DELTAS ==========")
    # For each stimulus, isolate the cursor-compositing penalty.
    fmap = {r["label"]: r for r in flip}
    mmap = {r["label"]: r for r in mover}
    for stim_name, mp in (("flip_demo", fmap), ("mover", mmap)):
        nc = statistics.fmean(mp["cursor=False"]["lats"])
        wc = statistics.fmean(mp["cursor=True "]["lats"])
        delta = wc - nc
        print(f"  {stim_name}: cursor penalty per grab = {delta:7.1f} us  "
              f"(nocursor mean {nc:6.1f}us, cursor mean {wc:6.1f}us)")


if __name__ == "__main__":
    main()
