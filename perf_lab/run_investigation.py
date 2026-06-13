"""Orchestrator for the cursor-compositing investigation.

Writes results.json incrementally after each probe so partial results survive.
"""
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PROBE = os.path.join(HERE, "latency_probe.py")
FLIP_DEMO = os.path.join(HERE, "flip_demo.exe")
MOVER = os.path.join(HERE, "mover.py")
MOVER_POS = os.path.join(HERE, "mover_pos.py")
NOTEPAD_IDLE = os.path.join(HERE, "notepad_idle.py")
TK_STIM = os.path.join(HERE, "tk_stim.py")

OUT = os.path.join(HERE, "investigation_results.json")
WARMUP_S = 0.5


def start_flip_demo():
    p = subprocess.Popen(
        [FLIP_DEMO],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        if p.stdout:
            line = p.stdout.readline()
            if line and b"present fps" in line.lower():
                break
        time.sleep(0.05)
    time.sleep(0.6)
    return p


def start_mover(secs, cx=None, cy=None):
    if cx is None:
        p = subprocess.Popen(
            [PY, MOVER, str(secs)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        p = subprocess.Popen(
            [PY, MOVER_POS, str(secs), str(cx), str(cy)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(1.2)
    return p


def start_notepad(secs):
    p = subprocess.Popen(
        [PY, NOTEPAD_IDLE, str(secs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    return p


def start_tk(secs):
    p = subprocess.Popen(
        [PY, TK_STIM, str(secs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    return p


def kill_proc(p):
    if p is None:
        return
    # NOTE: we used to send CTRL_BREAK_EVENT but on Windows that signal
    # ALSO hits the parent (this orchestrator) when the child wasn't
    # spawned in its own process group, killing us. Use terminate/kill.
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
    try:
        p.wait(timeout=1.0)
    except Exception:
        pass


def run_probe(label, cursor, timeout_ms, n_calls, subproc_timeout):
    cmd = [
        PY, PROBE,
        "--cursor", str(cursor),
        "--timeout-ms", str(timeout_ms),
        "--n-calls", str(n_calls),
        "--warmup-s", str(WARMUP_S),
        "--label", label,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=subproc_timeout
        )
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT on probe: {label}")
        return None
    out = proc.stdout
    if "__RESULT_BEGIN__" not in out:
        print(f"  ERROR running probe for {label}:")
        print("  STDOUT:", out[:500])
        print("  STDERR:", proc.stderr[:500])
        return None
    blob = out.split("__RESULT_BEGIN__")[1].split("__RESULT_END__")[0].strip()
    return json.loads(blob)


def stats(lats_us):
    if not lats_us:
        return {}
    s = sorted(lats_us)
    n = len(s)
    def pct(p):
        i = min(n - 1, int(p * n))
        return s[i]
    mean = sum(s) / n
    median = s[n // 2]
    p95 = pct(0.95)
    p99 = pct(0.99)
    mx = s[-1]
    bins = [0, 2000, 4000, 6000, 10000, 20000, 50000, 10**12]
    bin_names = ["<2ms", "2-4ms", "4-6ms", "6-10ms", "10-20ms", "20-50ms", ">50ms"]
    counts = [0] * len(bin_names)
    for v in s:
        for i in range(len(bin_names)):
            if v < bins[i + 1]:
                counts[i] += 1
                break
    return dict(
        n=n, mean_us=mean, median_us=median, p95_us=p95, p99_us=p99,
        max_us=mx, hist=dict(zip(bin_names, counts)),
    )


_all_results = []


def save():
    with open(OUT, "w") as fp:
        json.dump(_all_results, fp)


def append_result(r):
    _all_results.append(r)
    save()


def run_experiment(name, start_fn, configs, stim_secs):
    """For one stimulus, run each (cursor, timeout, n_calls) combo.

    configs = list of (cursor, timeout_ms, n_calls, subproc_timeout_s).
    """
    print(f"\n=== {name} (stim alive for {stim_secs:.0f}s) ===")
    stim = start_fn(stim_secs)
    try:
        for cursor, tmo, n_calls, sub_to in configs:
            lbl = f"{name} cursor={cursor} tmo={tmo} n={n_calls}"
            print(f"  probe: {lbl}", flush=True)
            t0 = time.perf_counter()
            r = run_probe(lbl, cursor, tmo, n_calls, sub_to)
            t1 = time.perf_counter()
            if r is None:
                continue
            r["stim"] = name
            r["stats"] = stats(r["latencies_us"])
            r["wallclock_s"] = t1 - t0
            print(f"     elapsed {t1-t0:5.1f}s  valid={r['valid_count']}  "
                  f"none={r['none_count']}  "
                  f"mean={r['stats']['mean_us']/1000:5.2f}ms  "
                  f"p99={r['stats']['p99_us']/1000:5.2f}ms",
                  flush=True)
            append_result(r)
            time.sleep(0.3)
    finally:
        kill_proc(stim)
        time.sleep(0.5)


def configs_for_main():
    """Experiments 1+2+3: 800 calls at low timeouts; 200 calls at tmo=1000."""
    return [
        (0,   10,  800, 60),
        (0,   30,  800, 60),
        (0,  100,  800, 60),
        (0, 1000,  200, 60),
        (1,   10,  800, 60),
        (1,   30,  800, 60),
        (1,  100,  800, 60),
        (1, 1000,  200, 60),
    ]


def configs_brief():
    return [
        (0, 30, 800, 60),
        (1, 30, 800, 60),
    ]


def stim_total_secs(configs):
    """Upper bound on how long the stim must live."""
    # Each probe: warmup + n_calls * timeout_ms worst case + IPC overhead.
    total = 0
    for cursor, tmo, n, sub_to in configs:
        # rustcam grab is at most ~tmo ms in pathological case; in
        # practice much less. Use sub_to/2 + 3s safety.
        total += sub_to + 3
    return total


def main():
    if os.path.exists(OUT):
        os.remove(OUT)

    # ---- flip_demo
    cfg = configs_for_main()
    run_experiment(
        "flip_demo", lambda s: start_flip_demo(),
        cfg, stim_total_secs(cfg),
    )

    # ---- mover (default orbit) — needs MUCH longer stim
    cfg = configs_for_main()
    run_experiment(
        "mover_default", lambda s: start_mover(s),
        cfg, stim_total_secs(cfg),
    )

    # ---- mover at corner
    cfg = configs_brief()
    run_experiment(
        "mover_corner", lambda s: start_mover(s, cx=300, cy=250),
        cfg, stim_total_secs(cfg),
    )

    # ---- notepad idle
    cfg = configs_brief()
    run_experiment(
        "notepad_idle", lambda s: start_notepad(s),
        cfg, stim_total_secs(cfg),
    )

    # ---- tk stim
    cfg = configs_brief()
    run_experiment(
        "tk_stim", lambda s: start_tk(s),
        cfg, stim_total_secs(cfg),
    )

    # ---- baseline (no stim)
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
              f"none={r['none_count']}  "
              f"mean={r['stats']['mean_us']/1000:5.2f}ms",
              flush=True)
        append_result(r)
        time.sleep(0.3)

    print(f"\nresults -> {OUT}")


if __name__ == "__main__":
    main()
