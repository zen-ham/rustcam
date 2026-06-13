"""Spawn Notepad to a known position and just let it sit there idle.
Stays alive for SECS seconds then exits, killing notepad.
"""
import sys, time, subprocess
import ctypes
from ctypes import wintypes

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

user32 = ctypes.windll.user32
EnumWindows = user32.EnumWindows
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
MoveWindow = user32.MoveWindow
IsWindowVisible = user32.IsWindowVisible

EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
)


def find_window_for_pid(pid):
    found = [None]

    def cb(hwnd, lparam):
        if not IsWindowVisible(hwnd):
            return True
        pid_buf = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
        if pid_buf.value == pid:
            ln = GetWindowTextLengthW(hwnd)
            if ln > 0:
                found[0] = hwnd
                return False
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return found[0]


p = subprocess.Popen(["notepad.exe"])
hwnd = None
deadline = time.perf_counter() + 5.0
while hwnd is None and time.perf_counter() < deadline:
    time.sleep(0.1)
    hwnd = find_window_for_pid(p.pid)

if hwnd:
    # Put at fixed position with fixed size.
    MoveWindow(hwnd, 600, 300, 800, 500, True)

try:
    time.sleep(SECS)
finally:
    try:
        p.terminate()
        p.wait(timeout=2.0)
    except Exception:
        p.kill()
