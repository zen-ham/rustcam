"""Run the remaining experiments (corner, notepad, tk, baseline) and
append them to investigation_results.json."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_investigation import (
    OUT, run_experiment, run_probe, stats,
    start_mover, start_notepad, start_tk, stim_total_secs, configs_brief,
    _all_results, append_result,
)
import run_investigation as RI


def load_existing():
    if os.path.exists(OUT):
        with open(OUT) as fp:
            data = json.load(fp)
        RI._all_results.extend(data)
        print(f"loaded {len(data)} prior results")


def main():
    load_existing()

    # mover at corner
    cfg = configs_brief()
    run_experiment(
        "mover_corner", lambda s: start_mover(s, cx=300, cy=250),
        cfg, stim_total_secs(cfg),
    )

    # notepad idle
    cfg = configs_brief()
    run_experiment(
        "notepad_idle", lambda s: start_notepad(s),
        cfg, stim_total_secs(cfg),
    )

    # tk stim
    cfg = configs_brief()
    run_experiment(
        "tk_stim", lambda s: start_tk(s),
        cfg, stim_total_secs(cfg),
    )

    # baseline (no stim)
    print("\n=== baseline_desktop (no stim) ===")
    for cursor in [0, 1]:
        lbl = f"baseline_desktop cursor={cursor} tmo=30 n=300"
        print(f"  probe: {lbl}", flush=True)
        t0 = time.perf_counter()
        r = run_probe(lbl, cursor, 30, 300, 60)
        t1 = time.perf_counter()
        if r is None:
            continue
        r["stim"] = "baseline_desktop"
        r["stats"] = stats(r["latencies_us"])
        r["wallclock_s"] = t1 - t0
        print(f"     elapsed {t1-t0:5.1f}s  valid={r['valid_count']}  "
              f"none={r['none_count']}  mean={r['stats']['mean_us']/1000:5.2f}ms",
              flush=True)
        append_result(r)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
