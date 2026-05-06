"""Color science primitives for wide-gamut, HDR screen-sync.

Supports the display pipeline of:
  • Sony BRAVIA 8 MK II (QD-OLED, 99.96 % DCI-P3, true 10-bit, HDR10+ / Dolby Vision, 2000 nit peak)
  • Samsung Galaxy Tab S9 Ultra (AMOLED, ~100 % DCI-P3)
  • ASUS Vivobook Pro 16X OLED (~100 % DCI-P3)

All arithmetic is in linear light (scene-referred).  Output is always
linear-light sRGB in [0, 1], ready to feed into the existing LUT encoder.

Reference standards
-------------------
* BT.2100   — PQ (ST.2084) and HLG transfer functions
* BT.2020   — Rec.2020 primaries and white point
* SMPTE 432 — DCI-P3 primaries
* IEC 61966-2-1 — sRGB primaries and EOTF
* ACES AP0 / S-2014-006 — filmic tone mapping reference
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import numpy as np


# ─── Color spaces ──────────────────────────────────────────────────────────────

class ColorSpace(str, Enum):
    SRGB     = "srgb"
    DISPLAY_P3 = "display-p3"
    DCI_P3   = "dci-p3"
    REC2020  = "rec2020"
    SCRGB    = "scrgb"   # Windows extended-range linear sRGB (FP16 DXCam HDR)


class HDRCurve(str, Enum):
    NONE = "none"   # SDR — already display-referred 0–1
    PQ   = "pq"     # ST.2084 / HDR10 / HDR10+
    HLG  = "hlg"    # Hybrid Log-Gamma / HLG


# ─── Pre-computed 3×3 color-space matrices (D65, linear light) ─────────────────
#
# All values from IEC / ITU standards; verified against ICC colour science tools.

# Rec.2020 → XYZ D65  (ITU-R BT.2020)
_REC2020_TO_XYZ = np.array([
    [0.6369580, 0.1446169, 0.1688810],
    [0.2627002, 0.6779981, 0.0593017],
    [0.0000000, 0.0280727, 1.0609851],
], dtype=np.float64)

# DCI-P3 → XYZ D65  (SMPTE 432-1, D65 white)
_DCI_P3_TO_XYZ = np.array([
    [0.4865709, 0.2656677, 0.1982173],
    [0.2289746, 0.6917385, 0.0792869],
    [0.0000000, 0.0451134, 1.0439444],
], dtype=np.float64)

# sRGB → XYZ D65  (IEC 61966-2-1)
_SRGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)

# XYZ D65 → sRGB  (inverse of the above)
_XYZ_TO_SRGB = np.array([
    [ 3.2404542, -1.5371385, -0.4985314],
    [-0.9692660,  1.8760108,  0.0415560],
    [ 0.0556434, -0.2040259,  1.0572252],
], dtype=np.float64)

# Derived: wide-gamut → linear sRGB  (pre-multiply once at import time)
REC2020_TO_SRGB: np.ndarray  = _XYZ_TO_SRGB @ _REC2020_TO_XYZ
DCI_P3_TO_SRGB:  np.ndarray  = _XYZ_TO_SRGB @ _DCI_P3_TO_XYZ


def gamut_matrix(source: ColorSpace) -> Optional[np.ndarray]:
    """Return the source → linear-light sRGB 3×3 matrix, or None for sRGB."""
    if source in (ColorSpace.REC2020,):
        return REC2020_TO_SRGB
    if source in (ColorSpace.DCI_P3, ColorSpace.DISPLAY_P3):
        return DCI_P3_TO_SRGB
    return None   # sRGB, scRGB — handled separately


# ─── Transfer function / EOTF ───────────────────────────────────────────────────

# PQ constants (ST.2084 / SMPTE 2084)
_PQ_M1 = 0.1593017578125    # 2610 / 16384
_PQ_M2 = 78.84375           # 2523 / 4096 * 128
_PQ_C1 = 0.8359375          # 3424 / 4096
_PQ_C2 = 18.8515625         # 2413 / 4096 * 32
_PQ_C3 = 18.6875            # 2392 / 4096 * 32
_PQ_PEAK_NITS = 10_000.0    # PQ reference peak luminance


def pq_decode(e_prime: np.ndarray) -> np.ndarray:
    """ST.2084 (PQ) EOTF: encoded signal [0,1] → linear light [0,1] at 10 000 nits.

    The returned value is normalised: 1.0 = 10 000 nits.
    Divide by ``_PQ_PEAK_NITS`` to get absolute luminance in nits.

    Parameters
    ----------
    e_prime:
        Float array, values in [0, 1] (the 10-bit signal normalised).
    """
    e = np.clip(e_prime, 0.0, 1.0).astype(np.float64)
    vp = np.power(e, 1.0 / _PQ_M2)
    num = np.maximum(vp - _PQ_C1, 0.0)
    den = np.maximum(_PQ_C2 - _PQ_C3 * vp, 1e-10)
    return np.power(num / den, 1.0 / _PQ_M1)


def pq_encode(linear: np.ndarray) -> np.ndarray:
    """ST.2084 (PQ) OETF: linear light [0,1] at 10 000 nits → encoded [0,1]."""
    y = np.clip(linear, 0.0, 1.0).astype(np.float64)
    yp = np.power(y, _PQ_M1)
    return np.power(
        (_PQ_C1 + _PQ_C2 * yp) / (1.0 + _PQ_C3 * yp),
        _PQ_M2,
    )


# HLG constants (BT.2100)
_HLG_A = 0.17883277
_HLG_B = 0.28466892
_HLG_C = 0.55991073
_HLG_GAMMA = 1.2
_HLG_PEAK  = 1000.0   # HLG system peak: 1000 nits


def hlg_decode(e_prime: np.ndarray, display_peak_nits: float = 1000.0) -> np.ndarray:
    """BT.2100 HLG EOTF: encoded signal [0,1] → normalised display luminance [0,1]."""
    e = np.clip(e_prime, 0.0, 1.0).astype(np.float64)
    scene = np.where(
        e <= 0.5,
        e * e / 3.0,
        (np.exp((e - _HLG_C) / _HLG_A) + _HLG_B) / 12.0,
    )
    # BT.2100 OOTF: scene-referred → display-referred
    # For simplicity, use the normalised form: L = scene^(gamma-1) * scene / peak
    luma = 0.2627 * e[..., 0] + 0.6780 * e[..., 1] + 0.0593 * e[..., 2] \
        if e.ndim == 3 else scene
    # Scalar OOTF weight
    ootf = np.power(np.maximum(luma, 1e-10), _HLG_GAMMA - 1.0)
    normalised = scene * ootf[..., None] if e.ndim == 3 else scene * ootf
    return np.clip(normalised * (_HLG_PEAK / display_peak_nits), 0.0, 1.0)


# ─── Tone mapping ───────────────────────────────────────────────────────────────

def aces_tone_map(
    linear: np.ndarray,
    peak_nits: float = 1000.0,
    target_nits: float = 200.0,
) -> np.ndarray:
    """ACES RRT + ODT approximation — fast, filmic, hue-preserving.

    Maps linear light in [0, peak_nits / 10000] to display-referred [0, 1].
    Based on Narkowicz 2015 parametric approximation of the ACES curve.

    Parameters
    ----------
    linear:
        Linear-light signal in [0, 1] where 1.0 = 10 000 nits (PQ reference).
    peak_nits:
        HDR content peak brightness in nits (e.g. 1000 for HDR10, 2000 for Sony BRAVIA peak).
    target_nits:
        Effective display output range for the ambient bulb (≈200 nits for strong LED).
    """
    # Re-scale from PQ nit-reference to target nit-range
    scale  = peak_nits / _PQ_PEAK_NITS / (target_nits / _PQ_PEAK_NITS)
    x = np.clip(linear.astype(np.float32) * scale, 0.0, None)

    # ACES Narkowicz parametric
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    return np.clip((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0)


def reinhard_tone_map(
    linear: np.ndarray,
    white_nits: float = 1000.0,
) -> np.ndarray:
    """Extended Reinhard tone mapper — simple but hue-neutral."""
    Lw = white_nits / _PQ_PEAK_NITS
    x  = np.clip(linear.astype(np.float32), 0.0, None)
    return x * (1.0 + x / (Lw * Lw)) / (1.0 + x)


# ─── sRGB transfer functions ────────────────────────────────────────────────────

def srgb_encode(linear: np.ndarray) -> np.ndarray:
    """Linear light → sRGB gamma (IEC 61966-2-1)."""
    x = np.clip(linear, 0.0, 1.0).astype(np.float32)
    return np.where(
        x <= 0.0031308,
        x * 12.92,
        1.055 * np.power(np.maximum(x, 0.0), 1.0 / 2.4) - 0.055,
    )


def srgb_decode(encoded: np.ndarray) -> np.ndarray:
    """sRGB gamma → linear light (IEC 61966-2-1)."""
    x = np.clip(encoded, 0.0, 1.0).astype(np.float32)
    return np.where(
        x <= 0.04045,
        x / 12.92,
        np.power((x + 0.055) / 1.055, 2.4),
    )


# ─── Gamut compression ──────────────────────────────────────────────────────────

def compress_to_srgb(
    linear_wide: np.ndarray,
    soft_clip_start: float = 0.82,
) -> np.ndarray:
    """Hue-preserving gamut compression from wide-gamut to sRGB.

    Out-of-gamut values (< 0 or > 1 after the gamut matrix) are compressed
    using a smooth sigmoid so that hue and relative lightness are preserved
    better than hard clipping.

    Parameters
    ----------
    linear_wide:
        Linear-light sRGB after the gamut-matrix multiply.  May contain
        values outside [0, 1] for highly saturated wide-gamut colours.
    soft_clip_start:
        Fraction of the [0,1] range at which soft compression begins.
    """
    # First: hard-clip negative values (below-black, not recoverable)
    x = np.maximum(linear_wide.astype(np.float64), 0.0)

    # Soft-clip the over-ranged positive values (out-of-gamut high saturation)
    above = x > soft_clip_start
    if np.any(above):
        # Sigmoid-style roll-off to 1.0
        excess = (x - soft_clip_start) / (1.0 - soft_clip_start + 1e-10)
        rolled = soft_clip_start + (1.0 - soft_clip_start) * (1.0 - np.exp(-excess * 2.5))
        x      = np.where(above, rolled, x)

    return np.clip(x, 0.0, 1.0).astype(np.float32)


# ─── High-level frame processor ─────────────────────────────────────────────────

class WideGamutFrameProcessor:
    """Convert an arbitrary captured frame to linear-light sRGB [0, 1].

    This is a stateless transformer — call ``process()`` per frame.
    Designed to be extremely fast: the hot path is a single matrix multiply
    + numpy ufunc pass; no Python loops.

    Parameters
    ----------
    source_color_space:
        Color space of the captured frame.
    hdr_curve:
        Transfer function used to encode the frame.  ``HDRCurve.NONE`` for SDR.
    peak_nits:
        HDR content peak in nits.  Only used when ``hdr_curve != NONE``.
    tone_mapper:
        ``"aces"`` (default, filmic) or ``"reinhard"`` (neutral).
    target_nits:
        Effective output range of the bulb for tone mapping.
    """

    def __init__(
        self,
        source_color_space: ColorSpace = ColorSpace.SRGB,
        hdr_curve: HDRCurve = HDRCurve.NONE,
        peak_nits: float = 1000.0,
        tone_mapper: str = "aces",
        target_nits: float = 200.0,
    ) -> None:
        self.source_color_space = source_color_space
        self.hdr_curve          = hdr_curve
        self.peak_nits          = peak_nits
        self.tone_mapper        = tone_mapper
        self.target_nits        = target_nits
        self._matrix            = gamut_matrix(source_color_space)

    # ── Per-frame API ──────────────────────────────────────────────────────────

    def process(self, frame: np.ndarray) -> np.ndarray:
        """Convert frame → uint8 linear-light sRGB [0, 255].

        Parameters
        ----------
        frame:
            uint8 RGB array (H, W, 3)  OR  float32/float64 (H, W, 3) in [0, 1].

        Returns
        -------
        np.ndarray
            uint8 RGB, same spatial dimensions, values in [0, 255].
            Colors are now sRGB-encoded, suitable for the existing LUT.
        """
        if frame.dtype == np.uint8:
            f = frame.astype(np.float32) / 255.0
        else:
            f = frame.astype(np.float32)

        # ── Step 1: decode transfer function → linear light ────────────────────
        linear = self._decode_transfer(f)

        # ── Step 2: gamut convert → linear sRGB ───────────────────────────────
        if self._matrix is not None:
            # Reshape for matrix multiply: (H*W, 3) @ M^T → (H*W, 3)
            h, w = linear.shape[:2]
            flat  = linear.reshape(-1, 3).astype(np.float64)
            flat  = (flat @ self._matrix.T).astype(np.float32)
            linear = compress_to_srgb(flat).reshape(h, w, 3)
        elif self.source_color_space == ColorSpace.SCRGB:
            # scRGB: linear extended sRGB — just clip to [0,1]
            linear = np.clip(linear, 0.0, 1.0)

        # ── Step 3: HDR tone map ───────────────────────────────────────────────
        if self.hdr_curve != HDRCurve.NONE:
            linear = self._tone_map(linear)

        # ── Step 4: sRGB encode ────────────────────────────────────────────────
        srgb = srgb_encode(linear)
        return np.clip(np.round(srgb * 255.0), 0, 255).astype(np.uint8)

    def process_single(
        self,
        color: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        """Convert a single extracted sRGB colour through the pipeline.

        Used when the capture is already in SDR sRGB but colours were pulled
        from a wide-gamut source and need remapping.
        """
        if self._matrix is None and self.hdr_curve == HDRCurve.NONE:
            return color
        arr = np.array(color, dtype=np.float32).reshape(1, 1, 3) / 255.0
        out = self.process((arr * 255).astype(np.uint8))
        return tuple(int(c) for c in out.reshape(3))

    # ── Private helpers ───────────────────────────────────────────────────────

    def _decode_transfer(self, f: np.ndarray) -> np.ndarray:
        if self.hdr_curve == HDRCurve.PQ:
            return pq_decode(f).astype(np.float32)
        if self.hdr_curve == HDRCurve.HLG:
            return hlg_decode(f, display_peak_nits=self.peak_nits).astype(np.float32)
        # SDR: already in sRGB gamma or linear (scRGB)
        if self.source_color_space == ColorSpace.SCRGB:
            return f           # already linear
        return srgb_decode(f)  # sRGB / P3 → linear

    def _tone_map(self, linear: np.ndarray) -> np.ndarray:
        if self.tone_mapper == "reinhard":
            return reinhard_tone_map(linear, white_nits=self.peak_nits)
        return aces_tone_map(linear, peak_nits=self.peak_nits, target_nits=self.target_nits)


# ─── HDR content auto-detection ─────────────────────────────────────────────────

def detect_hdr_content(frame: np.ndarray, sample_frac: float = 0.002) -> dict:
    """Heuristically detect whether a captured frame contains HDR / wide-gamut content.

    Analyses a random sample of pixels for luminance distribution patterns
    that are characteristic of PQ-encoded or wide-gamut SDR content.

    Parameters
    ----------
    frame:
        uint8 RGB capture (H, W, 3).
    sample_frac:
        Fraction of pixels to sample (0.002 = 0.2 %).

    Returns
    -------
    dict with keys:
        ``hdr_likely``         (bool)
        ``wide_gamut_likely``  (bool)
        ``mean_luma``          (float, sRGB-encoded)
        ``peak_luma``          (float, sRGB-encoded)
        ``pq_hint``            (float, 0–1 confidence that PQ is in use)
    """
    h, w = frame.shape[:2]
    n    = max(256, int(h * w * sample_frac))
    rng  = np.random.default_rng(0)
    flat = frame.reshape(-1, 3).astype(np.float32) / 255.0
    idx  = rng.choice(len(flat), min(n, len(flat)), replace=False)
    samp = flat[idx]

    luma = samp[:, 0]*0.2126 + samp[:, 1]*0.7152 + samp[:, 2]*0.0722

    # PQ-encoded content will have a very dark mean luma (midtones ≈ 0.15–0.30)
    # because PQ compresses highlights severely.
    mean_l = float(luma.mean())
    peak_l = float(luma.max())

    # Fraction of pixels above sRGB white that would indicate extended range
    # (shouldn't happen in true 8-bit SDR, but may with blown highlights from
    # a PQ source that was mis-tagged as sRGB)
    bright_frac = float((luma > 0.88).mean())

    # Saturation: DCI-P3 colours will appear hyper-saturated vs. sRGB
    max_ch = samp.max(axis=1)
    min_ch = samp.min(axis=1)
    sat    = max_ch - min_ch
    high_sat_frac = float((sat > 0.80).mean())   # near-100% sat pixels

    # PQ hint: a characteristic of PQ content captured without tone-mapping is
    # a bimodal luma histogram (dark shadows + bright sparks) and low mean luma
    pq_hint = max(0.0, min(1.0, (0.35 - mean_l) * 3.5 + bright_frac * 4.0))

    return {
        "hdr_likely":        pq_hint > 0.55,
        "wide_gamut_likely": high_sat_frac > 0.02 or pq_hint > 0.30,
        "mean_luma":         mean_l,
        "peak_luma":         peak_l,
        "pq_hint":           pq_hint,
        "high_sat_fraction": high_sat_frac,
    }


# ─── Convenience preset builders ────────────────────────────────────────────────

def processor_for_display(
    display_type: str = "srgb",
    hdr_enabled: bool = False,
    peak_nits: float = 1000.0,
) -> WideGamutFrameProcessor:
    """Build a ``WideGamutFrameProcessor`` from a human-readable display type.

    Parameters
    ----------
    display_type:
        One of ``"srgb"``, ``"p3"``, ``"rec2020"``, ``"oled_laptop"``,
        ``"sony_bravia"``, ``"samsung_tab"``, ``"android_p3"``.
    hdr_enabled:
        True if the display/capture is in HDR mode.
    peak_nits:
        HDR content peak (ignored for SDR).
    """
    _DISPLAY_MAP: dict[str, tuple[ColorSpace, float]] = {
        "srgb":         (ColorSpace.SRGB,       100.0),
        "p3":           (ColorSpace.DISPLAY_P3, 500.0),
        "rec2020":      (ColorSpace.REC2020,    1000.0),
        "oled_laptop":  (ColorSpace.DISPLAY_P3, 400.0),   # ASUS Vivobook Pro 16X OLED
        "sony_bravia":  (ColorSpace.DISPLAY_P3, 2000.0),  # Sony BRAVIA 8 MK II
        "samsung_tab":  (ColorSpace.DISPLAY_P3, 900.0),   # Galaxy Tab S9 Ultra
        "android_p3":   (ColorSpace.DISPLAY_P3, 500.0),   # Generic Android P3
    }
    cs, default_peak = _DISPLAY_MAP.get(display_type.lower(), (ColorSpace.SRGB, 100.0))
    p_nits           = peak_nits if hdr_enabled else default_peak

    return WideGamutFrameProcessor(
        source_color_space=cs,
        hdr_curve=HDRCurve.PQ if hdr_enabled else HDRCurve.NONE,
        peak_nits=p_nits,
        tone_mapper="aces",
        target_nits=220.0,
    )
