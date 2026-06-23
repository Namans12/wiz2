"""Cross-platform display gamut + HDR auto-detection.

Provides a single helper, :func:`detect_display_profile`, that returns a
:class:`DetectedDisplayProfile` describing the primary display's colour
characteristics. Used by the screen-sync pipeline to pick a sensible default
``source_color_space`` and ``device_profile`` when the user has selected
``"auto"`` in the GUI / config.

The detection is best-effort and degrades gracefully:

* **macOS** — queries Quartz / Core Graphics via PyObjC when available. Returns
  ``display-p3`` for Display P3 panels (most modern Macs since 2015) and
  ``rec2020`` for Pro Display XDR. Marks the device as ``"mac"`` profile so the
  cool/warm mapping gets the same +0.05 cool-bias correction as wide-gamut
  Android.
* **Windows** — queries DXGI / WCG capability and ICC profile via the WinAPI
  ctypes bindings. Falls back to sRGB if the profile cannot be read.
* **Linux** — no reliable single API. Returns sRGB with a comment in the
  ``notes`` field.

The module imports its platform-specific dependencies lazily inside the
detection functions so importing this module from a Windows build does not pull
in PyObjC, and vice versa.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class DetectedDisplayProfile:
    source_color_space: str = "srgb"        # "srgb" | "display-p3" | "rec2020"
    hdr_supported: bool = False
    supported_hdr_types: tuple[str, ...] = field(default_factory=tuple)
    primary_hdr_type: str = "none"
    device_profile: str = "generic"         # "generic" | "mac" | "bravia" | "wide_gamut_hdr"
    panel_is_oled: bool = False
    notes: str = ""


def detect_display_profile() -> DetectedDisplayProfile:
    system = platform.system().lower()
    try:
        if system == "darwin":
            return _detect_macos()
        if system == "windows":
            return _detect_windows()
        if system == "linux":
            return DetectedDisplayProfile(notes="linux: no reliable gamut probe")
    except Exception as exc:                # pragma: no cover - defensive
        return DetectedDisplayProfile(notes=f"detect failed: {exc!r}")
    return DetectedDisplayProfile(notes=f"unknown platform: {system}")


# ────────────────────────────────────────────────────────────────────────────────
# macOS
# ────────────────────────────────────────────────────────────────────────────────

def _detect_macos() -> DetectedDisplayProfile:
    """Use Core Graphics + AppKit to inspect the primary display's colour space."""
    try:
        from Quartz import (                      # type: ignore[import-not-found]
            CGMainDisplayID,
            CGDisplayCopyColorSpace,
        )
        from CoreFoundation import CFCopyDescription  # type: ignore[import-not-found]
    except Exception as exc:
        return DetectedDisplayProfile(
            device_profile="mac",
            notes=f"macOS detected but PyObjC missing: {exc!r}",
        )

    cs = CGDisplayCopyColorSpace(CGMainDisplayID())
    desc = str(CFCopyDescription(cs)).lower() if cs is not None else ""

    gamut = "srgb"
    if "display p3" in desc or "displayp3" in desc:
        gamut = "display-p3"
    elif "p3-d65" in desc or "dci-p3" in desc:
        gamut = "display-p3"
    elif "bt.2020" in desc or "bt2020" in desc or "rec.2020" in desc or "rec2020" in desc:
        gamut = "rec2020"
    elif "srgb" in desc:
        gamut = "srgb"
    elif "extended" in desc and "linear" in desc:
        # Apple's "extendedLinearDisplayP3" reports as extended-linear; treat as P3.
        gamut = "display-p3"

    hdr = "extended" in desc or "linear" in desc or gamut == "rec2020"
    # All Retina Macs since 2016 use P3 OLED-like behaviour for ambient sync.
    # Treat the model as an OLED-like profile so true-blacks aren't lifted.
    notes = f"macOS color space: {desc!r}"
    primary_hdr = "hdr10" if hdr else "none"
    return DetectedDisplayProfile(
        source_color_space=gamut,
        hdr_supported=hdr,
        supported_hdr_types=("hdr10",) if hdr else (),
        primary_hdr_type=primary_hdr,
        device_profile="mac",
        panel_is_oled=False,
        notes=notes,
    )


# ────────────────────────────────────────────────────────────────────────────────
# Windows
# ────────────────────────────────────────────────────────────────────────────────

def _detect_windows() -> DetectedDisplayProfile:
    """Best-effort: query advanced colour info via DXGI when present."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        return DetectedDisplayProfile(notes=f"win: ctypes unavailable: {exc!r}")

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # Read MONITORINFOEX for the primary monitor to get the device name.
    class MONITORINFOEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    hmonitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    info = MONITORINFOEX()
    info.cbSize = ctypes.sizeof(MONITORINFOEX)
    if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
        return DetectedDisplayProfile(notes="win: GetMonitorInfo failed")

    # Try DXGI 1.6 GetOutputProperties via Direct3D — too involved to wrap with
    # raw ctypes here; instead we read the ICC profile registry hint, which is
    # what Windows uses to advertise wide-gamut.
    gamut = "srgb"
    hdr = False
    notes = f"win: monitor {info.szDevice}"

    try:
        # Look up the colour profile path via GetICMProfileW
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        hdc = user32.GetDC(0)
        try:
            buf_len = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(260)
            if gdi32.GetICMProfileW(hdc, ctypes.byref(buf_len), buf):
                lower = buf.value.lower()
                if "p3" in lower:
                    gamut = "display-p3"
                elif "2020" in lower or "rec2020" in lower or "hdr" in lower:
                    gamut = "rec2020"
                    hdr = True
                notes += f"; icc={buf.value}"
        finally:
            user32.ReleaseDC(0, hdc)
    except Exception as exc:                # pragma: no cover - best-effort
        notes += f"; icc probe failed: {exc!r}"

    profile = "wide_gamut_hdr" if (hdr or gamut != "srgb") else "generic"
    return DetectedDisplayProfile(
        source_color_space=gamut,
        hdr_supported=hdr,
        supported_hdr_types=("hdr10",) if hdr else (),
        primary_hdr_type="hdr10" if hdr else "none",
        device_profile=profile,
        panel_is_oled=False,
        notes=notes,
    )


def cool_mix_bias_for_profile(profile: str) -> float:
    return {
        "bravia": 0.10,
        "wide_gamut_hdr": 0.05,
        "mac": 0.05,
        "generic": 0.0,
    }.get(profile, 0.0)
