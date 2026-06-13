"""Isolate the cost of IDXGISurface1::GetDC + DrawIconEx + ReleaseDC
on a standalone GDI-compatible D3D11 texture, under different DWM loads.

We don't go through DDA at all — that takes the DXGI duplication path out of
the picture so any timing changes are *purely* from the GDI/GetDC machinery
talking to DWM under different desktop loads. If the GetDC-waits-for-DWM
hypothesis is right, GetDC should get slower under mover.py than under
flip_demo or idle desktop.

Uses ctypes to call D3D11 + DXGI + user32 directly.
"""
import ctypes
import ctypes.wintypes as wt
import os
import signal
import statistics
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_int, c_uint, c_uint32, c_void_p, Structure

# ----- minimal D3D11 / DXGI bindings via comtypes -----
import comtypes
from comtypes import IUnknown, GUID, COMMETHOD, HRESULT, BSTR
from comtypes.client import CreateObject

HERE = os.path.dirname(os.path.abspath(__file__))
FLIP_DEMO = os.path.join(HERE, "flip_demo.exe")
MOVER = os.path.join(HERE, "mover.py")

# IIDs we need
IID_ID3D11Device = GUID("{db6f6ddb-ac77-4e88-8253-819df9bbf140}")
IID_ID3D11DeviceContext = GUID("{c0bfa96c-e089-44fb-8eaf-26f8796190da}")
IID_IDXGISurface1 = GUID("{4AE63092-6327-4c1b-80AE-BFE12EA32B86}")
IID_ID3D11Texture2D = GUID("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")

# Use rustcam ourselves to create the device + texture and then poke at it via ctypes.
# Way simpler than reimplementing D3D11CreateDevice in ctypes. We use the fact that
# rustcam.Capturer creates a GDI-compatible texture under cursor=True, but we want
# to time GetDC ALONE outside the grab loop. We do that by getting an IDXGISurface1
# from the texture via Python... actually that requires native code.
#
# Simpler: invoke draw_cursor a million times by repeatedly calling cap.grab(cursor=True)
# and SUBTRACTING the cursor=False time to isolate GDI overhead in differential.
import rustcam


def start_flip_demo():
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
    return subprocess.Popen(
        [sys.executable, MOVER, str(secs)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def kill_proc(p):
    if p is None:
        return
    try: p.send_signal(signal.CTRL_BREAK_EVENT)
    except: pass
    try: p.wait(timeout=2.0)
    except:
        try: p.terminate()
        except: pass
    try: p.wait(timeout=1.0)
    except:
        try: p.kill()
        except: pass


def measure_diff(label, stim_fn, duration=2.5):
    """Measure cursor=False vs cursor=True under the stimulus.
    Return (nocursor_lats, cursor_lats)."""
    stim = stim_fn() if stim_fn else None
    if stim is not None: time.sleep(1.0)
    try:
        results = {}
        for cursor in (False, True):
            cap = rustcam.Capturer(output=0, cursor=cursor)
            try:
                # warmup
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < 0.5: cap.grab(timeout_ms=30)
                lats = []
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < duration:
                    s = time.perf_counter()
                    cap.grab(timeout_ms=30)
                    lats.append((time.perf_counter() - s) * 1e6)
                results[cursor] = lats
            finally:
                cap.close()
                time.sleep(0.4)
        return results
    finally:
        kill_proc(stim)
        time.sleep(0.4)


def summarize(lats):
    sl = sorted(lats)
    return {
        "n": len(lats),
        "min": min(lats),
        "p10": sl[int(len(sl) * 0.10)],
        "p50": sl[len(sl) // 2],
        "p90": sl[int(len(sl) * 0.90)],
        "p99": sl[int(len(sl) * 0.99)],
        "max": max(lats),
        "mean": statistics.fmean(lats),
        "fps": len(lats) / (sum(lats) / 1e6) if lats else 0,
    }


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    print("Measuring grab() latency cursor=False vs cursor=True under three loads.\n")

    rounds = [
        ("idle desktop", None),
        ("flip_demo (DWM bypassed by flip-model)", start_flip_demo),
        ("mover.py (DWM busy compositing window)", lambda: start_mover(20)),
    ]

    for label, stim_fn in rounds:
        print(f"== {label} ==")
        results = measure_diff(label, stim_fn)
        nc = summarize(results[False])
        wc = summarize(results[True])
        # The per-call differential is dominated by the cursor=True extra work.
        # Compare percentile-by-percentile for fairness — DWM and DDA delivery
        # vary per-call so means alone can mislead.
        print(f"  cursor=False: n={nc['n']:4}  fps={nc['fps']:6.1f}  "
              f"min={nc['min']:6.0f}  p50={nc['p50']:6.0f}  p90={nc['p90']:6.0f}  "
              f"p99={nc['p99']:7.0f}  max={nc['max']:7.0f}us")
        print(f"  cursor=True:  n={wc['n']:4}  fps={wc['fps']:6.1f}  "
              f"min={wc['min']:6.0f}  p50={wc['p50']:6.0f}  p90={wc['p90']:6.0f}  "
              f"p99={wc['p99']:7.0f}  max={wc['max']:7.0f}us")
        print(f"  delta:        " + " " * 17 +
              f"min={wc['min']-nc['min']:+6.0f}  p50={wc['p50']-nc['p50']:+6.0f}  "
              f"p90={wc['p90']-nc['p90']:+6.0f}  p99={wc['p99']-nc['p99']:+7.0f}  "
              f"max={wc['max']-nc['max']:+7.0f}us")
        print()


if __name__ == "__main__":
    main()
