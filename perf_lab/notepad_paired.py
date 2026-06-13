"""Run notepad_idle cursor=0 and cursor=1 BACK-TO-BACK so user mouse activity
is comparable across them. Probe runs in-process to avoid subprocess delay.
"""
import ctypes
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def find_window_for_pid(pid):
    user32 = ctypes.windll.user32
    from ctypes import wintypes
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
    )
    found = [None]
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid_buf = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
        if pid_buf.value == pid:
            ln = user32.GetWindowTextLengthW(hwnd)
            if ln > 0:
                found[0] = hwnd
                return False
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0]


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    np = subprocess.Popen(["notepad.exe"])
    hwnd = None
    deadline = time.perf_counter() + 5.0
    while hwnd is None and time.perf_counter() < deadline:
        time.sleep(0.1)
        hwnd = find_window_for_pid(np.pid)
    if hwnd:
        ctypes.windll.user32.MoveWindow(hwnd, 600, 300, 800, 500, True)

    time.sleep(0.5)

    try:
        import rustcam
        results = []
        for cursor in [0, 1, 0, 1]:  # alternate to cancel out timing effects
            cap = rustcam.Capturer(output=0, cursor=bool(cursor))
            # warmup
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 0.3:
                cap.grab(timeout_ms=30)
            lats = []
            valid = 0
            none_c = 0
            grab = cap.grab
            for _ in range(400):
                ta = time.perf_counter()
                f = grab(timeout_ms=30)
                tb = time.perf_counter()
                lats.append(int((tb - ta) * 1_000_000))
                if f is None:
                    none_c += 1
                else:
                    valid += 1
            s = sorted(lats)
            med = s[len(s)//2]
            mean = sum(s) / len(s)
            p99 = s[int(0.99 * len(s))]
            print(f"  cursor={cursor}  valid={valid:3d}  none={none_c:3d}  "
                  f"mean={mean/1000:5.2f}ms  median={med/1000:5.2f}ms  "
                  f"p99={p99/1000:5.2f}ms")
            results.append({
                "cursor": cursor, "valid": valid, "none": none_c,
                "mean_us": mean, "median_us": med, "p99_us": p99,
                "latencies_us": lats,
            })
            cap.close()
            time.sleep(0.3)

        with open(os.path.join(HERE, "notepad_paired_results.json"), "w") as fp:
            json.dump(results, fp)
    finally:
        try: np.terminate()
        except: pass


if __name__ == "__main__":
    main()
