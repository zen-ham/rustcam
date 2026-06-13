"""Run a single (stim, cursor, tmo, n) combination and append to results.

Usage:
    python run_one.py <stim> <cursor> <tmo_ms> <n_calls> [stim_arg1 stim_arg2 ...]
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_investigation import (
    OUT, run_probe, stats,
    start_flip_demo, start_mover, start_notepad, start_tk,
    kill_proc,
)


def stim_for(name, secs, extras):
    if name == "flip_demo":
        return start_flip_demo()
    if name == "mover_default":
        return start_mover(secs)
    if name == "mover_corner":
        return start_mover(secs, cx=300, cy=250)
    if name == "notepad_idle":
        return start_notepad(secs)
    if name == "tk_stim":
        return start_tk(secs)
    if name == "baseline_desktop":
        return None
    if name == "mover_custom":
        cx = int(extras[0]); cy = int(extras[1])
        return start_mover(secs, cx=cx, cy=cy)
    raise ValueError(f"unknown stim {name}")


def append_to_results(r):
    if os.path.exists(OUT):
        with open(OUT) as fp:
            data = json.load(fp)
    else:
        data = []
    data.append(r)
    with open(OUT, "w") as fp:
        json.dump(data, fp)


def main():
    stim_name = sys.argv[1]
    cursor = int(sys.argv[2])
    tmo = int(sys.argv[3])
    n_calls = int(sys.argv[4])
    extras = sys.argv[5:]

    stim_secs = 30
    stim = stim_for(stim_name, stim_secs, extras)
    try:
        time.sleep(0.5)
        lbl = f"{stim_name} cursor={cursor} tmo={tmo} n={n_calls}"
        print(f"probe: {lbl}", flush=True)
        t0 = time.perf_counter()
        r = run_probe(lbl, cursor, tmo, n_calls, 60)
        t1 = time.perf_counter()
        if r is None:
            print("FAILED")
            return 1
        r["stim"] = stim_name
        r["stats"] = stats(r["latencies_us"])
        r["wallclock_s"] = t1 - t0
        print(f"  elapsed {t1-t0:5.1f}s  valid={r['valid_count']}  "
              f"none={r['none_count']}  mean={r['stats']['mean_us']/1000:5.2f}ms "
              f"median={r['stats']['median_us']/1000:5.2f}ms "
              f"p95={r['stats']['p95_us']/1000:5.2f}ms "
              f"p99={r['stats']['p99_us']/1000:5.2f}ms", flush=True)
        print(f"  hist: {r['stats']['hist']}", flush=True)
        append_to_results(r)
    finally:
        kill_proc(stim)


if __name__ == "__main__":
    sys.exit(main() or 0)
