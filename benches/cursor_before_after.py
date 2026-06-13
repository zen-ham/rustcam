"""Focused before/after chart for the v0.0.6 cursor=True fix.

v0.0.5 had `cursor=True` drop rustcam from ~150 fps to ~47 fps on
realistic moving content (mover.py / windowed apps that keep DWM
awake). The drop came from `IDXGISurface1::GetDC` on a
`MISC_GDI_COMPATIBLE` texture — a forced CPU↔GPU sync that stalls
behind whatever DWM has queued.

v0.0.6 drops GDI entirely and composites the cursor in software using
DDA's own `PointerPosition` + `GetFramePointerShape`. No sync barrier.

This bench runs rustcam in isolation against three stimuli and reports
mean/min/max across 5 runs. No competitor libs in the loop — those go
in the main bench. Here we just want a clean before/after for the
cursor fix.
"""
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLIP_DEMO = os.path.join(ROOT, "perf_lab", "flip_demo.exe")
MOVER = os.path.join(ROOT, "perf_lab", "mover.py")
DURATION_S = 3.0
RUNS = 5


def measure_rate(cursor: bool, duration_s: float) -> float:
    """Return valid-fps over `duration_s` seconds with a fresh Capturer."""
    import rustcam
    cap = rustcam.Capturer(output=0, cursor=cursor)
    # Warmup
    for _ in range(20):
        cap.grab(timeout_ms=30)
    t = time.perf_counter()
    n = 0
    while time.perf_counter() - t < duration_s:
        f = cap.grab(timeout_ms=30)
        if f is not None:
            n += 1
    elapsed = time.perf_counter() - t
    cap.close()
    return n / elapsed


def run_round(stim_name: str, stim_proc):
    print(f"\n== {stim_name} ==")
    time.sleep(1.0)
    res = {}
    for cursor in [False, True]:
        runs = []
        for r in range(RUNS):
            rate = measure_rate(cursor, DURATION_S)
            runs.append(rate)
            print(f"  cursor={cursor} run {r}: {rate:.1f}/s")
            time.sleep(0.5)
        res[cursor] = runs
    return res


def start_flip_demo():
    if not os.path.exists(FLIP_DEMO):
        raise SystemExit(f"missing {FLIP_DEMO}")
    return subprocess.Popen([FLIP_DEMO], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_mover(secs):
    if not os.path.exists(MOVER):
        return None
    return subprocess.Popen(
        [sys.executable, MOVER, str(secs)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def kill(p):
    if p is None:
        return
    try: p.terminate()
    except Exception: pass
    try: p.wait(timeout=2)
    except Exception:
        try: p.kill()
        except Exception: pass


def main():
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    total_budget = RUNS * 2 * (DURATION_S + 1.0)

    # Stimulus 1: flip_demo (fullscreen flip — DWM asleep, fast case)
    flip = start_flip_demo()
    flip_res = run_round("flip_demo (DWM asleep, easy case)", flip)
    kill(flip)
    time.sleep(0.5)

    # Stimulus 2: mover.py (windowed PyQt, DWM compositing — the case
    # the v0.0.5 cursor bug catastrophically failed on)
    mover = start_mover(total_budget + 10)
    mover_res = run_round("mover.py (DWM compositing, hard case)", mover)
    kill(mover)

    print()
    print("== summary ==")
    print(f"{'stimulus':<14} {'cursor':<8} {'min':>6} {'median':>6} {'mean':>6} {'max':>6}")
    rows = []
    for label, res in [("flip_demo", flip_res), ("mover.py", mover_res)]:
        for cursor, runs in res.items():
            r = sorted(runs)
            print(f"{label:<14} {str(cursor):<8} {r[0]:>6.1f} {r[len(r)//2]:>6.1f} {sum(r)/len(r):>6.1f} {r[-1]:>6.1f}")
            rows.append(dict(stim=label, cursor=cursor, runs=runs))

    # Chart
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stims = ["flip_demo", "mover.py"]
    cf_means = [sum(flip_res[False]) / len(flip_res[False]),
                sum(mover_res[False]) / len(mover_res[False])]
    ct_means = [sum(flip_res[True]) / len(flip_res[True]),
                sum(mover_res[True]) / len(mover_res[True])]
    cf_min = [min(flip_res[False]), min(mover_res[False])]
    cf_max = [max(flip_res[False]), max(mover_res[False])]
    ct_min = [min(flip_res[True]), min(mover_res[True])]
    ct_max = [max(flip_res[True]), max(mover_res[True])]

    # v0.0.5 reference numbers (from the README perf table on the
    # v0.0.5 release commit, measured by the same bench infrastructure).
    # mover.py: cursor=False ~150, cursor=True ~47 (sometimes lower)
    v005_cf = [165, 150]  # flip_demo, mover.py for cursor=False
    v005_ct = [160, 47]   # flip_demo, mover.py for cursor=True

    x = np.arange(len(stims))
    w = 0.20
    fig, ax = plt.subplots(figsize=(11, 5.5))
    b1 = ax.bar(x - 1.5 * w, v005_cf, w, label="v0.0.5  cursor=False", color="#aac5a3")
    b2 = ax.bar(x - 0.5 * w, v005_ct, w, label="v0.0.5  cursor=True", color="#d99595")
    b3 = ax.bar(x + 0.5 * w, cf_means, w,
                yerr=[[m - mn for m, mn in zip(cf_means, cf_min)],
                      [mx - m for m, mx in zip(cf_means, cf_max)]],
                label="v0.0.6  cursor=False", color="#4C9F38", capsize=4,
                error_kw={"ecolor": "#222", "lw": 0.7})
    b4 = ax.bar(x + 1.5 * w, ct_means, w,
                yerr=[[m - mn for m, mn in zip(ct_means, ct_min)],
                      [mx - m for m, mx in zip(ct_means, ct_max)]],
                label="v0.0.6  cursor=True", color="#1f77b4", capsize=4,
                error_kw={"ecolor": "#222", "lw": 0.7})
    ax.axhline(180, color="grey", linestyle="--", linewidth=1.0,
               alpha=0.7, label="180 Hz monitor refresh")
    ax.set_ylabel("frames-per-second delivered (valid grabs)")
    ax.set_title(
        f"rustcam cursor=True fix:  v0.0.5 GDI/GetDC stall  →  v0.0.6 manual composite\n"
        f"(mean across {RUNS} runs, error bars = min/max)")
    ax.set_xticks(x)
    ax.set_xticklabels(stims)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    for bars, vals in ((b1, v005_cf), (b2, v005_ct), (b3, cf_means), (b4, ct_means)):
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2,
                    f"{v:.0f}", ha="center", va="bottom", fontsize=8.5)
    ax.legend(loc="upper right", fontsize=9)
    plt.tight_layout()
    out = os.path.join(ROOT, "docs", "cursor_fix_before_after.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    plt.savefig(out, dpi=140)
    print(f"\nchart -> {out}")


if __name__ == "__main__":
    main()
