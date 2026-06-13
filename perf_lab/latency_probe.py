"""Per-call latency probe for rustcam.Capturer.grab().

Usage:
    python latency_probe.py --cursor 0|1 --timeout-ms N --n-calls N \
        --warmup-s S [--out OUT.json]

Runs N grab() calls back-to-back and records per-call latency.
Emits a JSON blob on stdout (and to --out if given) with:
    config (cursor, timeout_ms, n_calls, warmup_s)
    latencies_us : list[int]
    valid_count, none_count
    elapsed_s
"""
import argparse
import ctypes
import json
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", type=int, required=True, choices=[0, 1])
    ap.add_argument("--timeout-ms", type=int, required=True)
    ap.add_argument("--n-calls", type=int, default=1000)
    ap.add_argument("--warmup-s", type=float, default=0.5)
    ap.add_argument("--out", default=None)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    ctypes.windll.shcore.SetProcessDpiAwareness(2)

    import rustcam
    cap = rustcam.Capturer(output=0, cursor=bool(args.cursor))

    try:
        # Warmup
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < args.warmup_s:
            cap.grab(timeout_ms=args.timeout_ms)

        latencies_us = []
        valid = 0
        none_c = 0
        clock = time.perf_counter
        t_start = clock()
        # Pre-bind locals for speed.
        grab = cap.grab
        tmo = args.timeout_ms
        n = args.n_calls
        for _ in range(n):
            t_a = clock()
            f = grab(timeout_ms=tmo)
            t_b = clock()
            latencies_us.append(int((t_b - t_a) * 1_000_000))
            if f is None:
                none_c += 1
            else:
                valid += 1
        elapsed = clock() - t_start

        result = {
            "label": args.label,
            "cursor": args.cursor,
            "timeout_ms": args.timeout_ms,
            "n_calls": args.n_calls,
            "warmup_s": args.warmup_s,
            "elapsed_s": elapsed,
            "valid_count": valid,
            "none_count": none_c,
            "latencies_us": latencies_us,
        }
        blob = json.dumps(result)
        if args.out:
            with open(args.out, "w") as fp:
                fp.write(blob)
        # Print marker + blob (so subprocess parsing can find it)
        print("__RESULT_BEGIN__")
        print(blob)
        print("__RESULT_END__")
    finally:
        cap.close()


if __name__ == "__main__":
    main()
