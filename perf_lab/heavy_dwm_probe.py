"""Run rustcam latency probe under HEAVY DWM load (multiple windowed stimuli).

Spawns 3 movers at different orbit centers + 1 tk_stim, sleeps 2.5s,
then runs the latency probe with cursor=0 and cursor=1.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PY = sys.executable
PROBE = os.path.join(HERE, "latency_probe.py")


def spawn_stims():
    procs = []
    for cx, cy in [(300, 300), (1500, 300), (900, 800)]:
        p = subprocess.Popen(
            [PY, os.path.join(HERE, "mover_pos.py"), "60", str(cx), str(cy)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    procs.append(subprocess.Popen(
        [PY, os.path.join(HERE, "tk_stim.py"), "60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
    return procs


def kill_all(procs):
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=2.0)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def run_probe(cursor, n_calls=800):
    cmd = [
        PY, PROBE,
        "--cursor", str(cursor),
        "--timeout-ms", "30",
        "--n-calls", str(n_calls),
        "--warmup-s", "0.5",
        "--label", f"heavy_dwm cursor={cursor}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    blob = proc.stdout.split("__RESULT_BEGIN__")[1].split("__RESULT_END__")[0].strip()
    return json.loads(blob)


def stats(lats):
    s = sorted(lats)
    n = len(s)
    bins = [0, 2000, 4000, 6000, 10000, 20000, 50000, 10**12]
    bin_names = ["<2ms", "2-4ms", "4-6ms", "6-10ms", "10-20ms", "20-50ms", ">50ms"]
    counts = [0] * len(bin_names)
    for v in s:
        for i in range(len(bin_names)):
            if v < bins[i + 1]:
                counts[i] += 1
                break
    return dict(
        n=n, mean_us=sum(s) / n, median_us=s[n // 2],
        p95_us=s[int(0.95 * n)], p99_us=s[int(0.99 * n)],
        max_us=s[-1], hist=dict(zip(bin_names, counts)),
    )


def main():
    print("Spawning 3 movers + 1 tk_stim for heavy DWM load...")
    procs = spawn_stims()
    time.sleep(2.5)

    try:
        results = []
        for cursor in [0, 1]:
            print(f"\nProbe cursor={cursor}", flush=True)
            r = run_probe(cursor)
            r["stim"] = "heavy_dwm"
            r["stats"] = stats(r["latencies_us"])
            results.append(r)
            print(f"  valid={r['valid_count']} none={r['none_count']} "
                  f"mean={r['stats']['mean_us']/1000:5.2f}ms "
                  f"median={r['stats']['median_us']/1000:5.2f}ms "
                  f"p95={r['stats']['p95_us']/1000:5.2f}ms "
                  f"p99={r['stats']['p99_us']/1000:5.2f}ms",
                  flush=True)
            print(f"  hist: {r['stats']['hist']}", flush=True)
            time.sleep(0.5)

        # Append to investigation_results.json
        OUT = os.path.join(HERE, "investigation_results.json")
        if os.path.exists(OUT):
            with open(OUT) as fp:
                existing = json.load(fp)
        else:
            existing = []
        existing.extend(results)
        with open(OUT, "w") as fp:
            json.dump(existing, fp)
        print(f"\nappended to {OUT}")
    finally:
        kill_all(procs)


if __name__ == "__main__":
    main()
