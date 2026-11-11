"""Short frame waits with a Windows high-resolution timer when available."""

from __future__ import annotations

import math
import sys
import time


class _WindowsHighResolutionTimer:
    """Own one waitable timer; callers must close it on the owning worker."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel.CreateWaitableTimerExW.restype = wintypes.HANDLE
        kernel.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        kernel.SetWaitableTimer.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateWaitableTimerExW(None, None, 0x2, 0x100002)
        if not handle:
            raise OSError(ctypes.get_last_error(), "High-resolution timer unavailable")
        self._kernel = kernel
        self._handle = handle

    def wait(self, seconds: float) -> bool:
        import ctypes

        due = ctypes.c_longlong(-max(1, int(seconds * 10_000_000)))
        if not self._kernel.SetWaitableTimer(self._handle, ctypes.byref(due), 0, None, None, False):
            return False
        timeout_ms = min(0xFFFFFFFE, math.ceil(seconds * 1000) + 1000)
        return self._kernel.WaitForSingleObject(self._handle, timeout_ms) == 0

    def close(self) -> None:
        if self._handle:
            handle, self._handle = self._handle, None
            self._kernel.CloseHandle(handle)


def _make_timer():
    if sys.platform != "win32":
        return None
    try:
        return _WindowsHighResolutionTimer()
    except (OSError, AttributeError):
        return None


class FramePacer:
    """Wait for the remaining frame budget without changing global timer settings."""

    def __init__(self) -> None:
        self._timer = _make_timer()

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._timer is None:
            time.sleep(seconds)
            return
        start = time.perf_counter()
        try:
            if self._timer.wait(seconds):
                return
        except (OSError, ValueError):
            pass
        self.close()
        remaining = seconds - (time.perf_counter() - start)
        if remaining > 0:
            time.sleep(remaining)

    def close(self) -> None:
        if self._timer is not None:
            timer, self._timer = self._timer, None
            timer.close()
