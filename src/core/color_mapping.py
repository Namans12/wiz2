"""Bulb-aware color mapping — wide-gamut edition.

The 33³ LUT is now built in the *input* color space (sRGB, DCI-P3, or Rec.2020).
This means that when the screen-sync extractor feeds a DCI-P3 colour directly,
the LUT returns the correct RGBCW payload without any additional conversion step
in the hot path.

WiZ 9W E27 hardware facts
--------------------------
* RGBCW: R ≈ 625 nm, G ≈ 530 nm, B ≈ 455 nm color LEDs +
          C ≈ 6200 K cold-white + W ≈ 2700 K warm-white.
* Channel currents: R ≈ 9 mA, G ≈ 8 mA, B ≈ 6 mA  → B needs gain boost.
* wcr (white_to_color_ratio) ≈ 20 from firmware.
* Always send explicit C and W (even zeros) to prevent firmware white inference
  that washes out saturated colors.

Wide-gamut strategy
-------------------
* The LUT grid is seeded from the *source* gamut's primary coordinates.
  For DCI-P3 input, the R primary maps to (1, 0, 0) in P3 space which is
  genuinely more saturated than sRGB red — the bulb tries to match that.
* Out-of-gamut P3/Rec.2020 saturation beyond what the LED can produce is
  compressed toward the white point while preserving hue angle.
* The white-channel gating is tightened: saturated wide-gamut inputs suppress
  both C and W more aggressively for maximum gamut utilization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .calibration import BulbCalibrationTable
from .color_science import (
    ColorSpace,
    REC2020_TO_SRGB,
    DCI_P3_TO_SRGB,
    compress_to_srgb,
)


_GRID_SIZE      = 33
_GRID_MAX_INDEX = _GRID_SIZE - 1
_EPSILON        = 1e-6


# ─── Transfer helpers (kept local for LUT build speed) ─────────────────────────

def _srgb_to_linear(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1.0 / 2.4) - 0.055)


def _smoothstep01(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# ─── Bulb hardware profile ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class BulbColorProfile:
    """Hardware description of one WiZ bulb.

    Parameters
    ----------
    source_color_space:
        The color space of the *input* colors that will be fed to the LUT.
        Defaults to sRGB; set to ``"display-p3"`` or ``"rec2020"`` when
        capturing from a wide-gamut display (Sony BRAVIA, Samsung Tab, ASUS OLED).
    """

    model_name: Optional[str] = None
    mac: Optional[str] = None
    white_channels: int = 1
    white_to_color_ratio: int = 20
    rgb_channel_current: tuple[int, int, int] = (9, 8, 6)
    render_factor: tuple[int, ...] = field(default_factory=tuple)
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    source_color_space: str = "srgb"                   # NEW: gamut of input colours

    @property
    def white_ratio(self) -> float:
        return max(0.0, min(1.0, float(self.white_to_color_ratio) / 100.0))

    @property
    def color_space(self) -> ColorSpace:
        normalized = self.source_color_space.lower().replace("_", "-")
        aliases = {
            "p3": ColorSpace.DISPLAY_P3,
            "display_p3": ColorSpace.DISPLAY_P3,
            "bt2020": ColorSpace.REC2020,
            "bt.2020": ColorSpace.REC2020,
            "rec-2020": ColorSpace.REC2020,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return ColorSpace(normalized)
        except ValueError:
            return ColorSpace.SRGB


# ─── Gamut mapper ───────────────────────────────────────────────────────────────

class BulbGamutMapper:
    """Pre-built 33³ LUT: wide-gamut sRGB/P3/Rec.2020 → RGBW or RGBCW payload.

    The LUT is constructed once at init; ``map_rgb()`` is an O(1) array lookup.

    Wide-gamut support
    ------------------
    When ``profile.source_color_space`` is ``"display-p3"`` or ``"rec2020"``,
    the grid is seeded in that gamut and converted to linear sRGB internally
    before building the RGBCW payload.  The caller can pass P3-valued colours
    directly to ``map_rgb()`` and get the correct bulb payload.
    """

    def __init__(
        self,
        profile: Optional[BulbColorProfile] = None,
        calibration: Optional[BulbCalibrationTable] = None,
        tone_lut: Optional[BulbCalibrationTable] = None,
    ) -> None:
        self.profile     = profile or BulbColorProfile()
        self.calibration = calibration
        self.tone_lut    = tone_lut
        self._rgb_gain   = self._derive_rgb_gain()
        self._dual_white = self.profile.white_channels >= 2
        self._gamut_matrix = self._choose_gamut_matrix()
        self._lookup     = self._build_lookup()

    # ── Private ────────────────────────────────────────────────────────────────

    def _choose_gamut_matrix(self) -> Optional[np.ndarray]:
        cs = self.profile.color_space
        if cs == ColorSpace.REC2020:
            return REC2020_TO_SRGB
        if cs in (ColorSpace.DCI_P3, ColorSpace.DISPLAY_P3):
            return DCI_P3_TO_SRGB
        return None

    def _derive_rgb_gain(self) -> np.ndarray:
        """Equal-brightness gain correction from measured channel currents."""
        cur = np.array(self.profile.rgb_channel_current, dtype=np.float32)
        if cur.size < 3:
            return np.ones(3, dtype=np.float32)
        cur  = np.clip(cur[:3], 1.0, None)
        gain = (cur.min() / cur) ** 0.45
        return np.clip(gain, 0.70, 1.0)

    def _build_lookup(self) -> np.ndarray:
        """Build the full 33³ → RGBW(W) LUT in the configured source gamut."""

        # ── 1. Seed a 33³ grid in the source color space (0…1 per channel) ────
        grid = np.linspace(0.0, 1.0, _GRID_SIZE, dtype=np.float32)
        r_g, g_g, b_g = np.meshgrid(grid, grid, grid, indexing="ij")
        rgb = np.stack((r_g, g_g, b_g), axis=-1)       # (33,33,33,3)

        # ── 2. Optional tone/calibration correction ────────────────────────────
        if self.tone_lut is not None:
            rgb = self.tone_lut.correct_rgb_batch(rgb)
        if self.calibration is not None:
            rgb = self.calibration.correct_rgb_batch(rgb)

        # ── 3. Convert to linear sRGB ──────────────────────────────────────────
        if self._gamut_matrix is not None:
            # Wide-gamut path: linear-P3 / linear-Rec2020 → linear-sRGB
            # First linearise using sRGB gamma (P3 D65 uses same gamma as sRGB)
            linear_wide = _srgb_to_linear(rgb)
            h, w_sz = linear_wide.shape[0], linear_wide.shape[1]
            d = linear_wide.shape[2]
            flat = linear_wide.reshape(-1, 3).astype(np.float64)
            flat = (flat @ self._gamut_matrix.T).astype(np.float32)
            # Soft-compress out-of-gamut values (hyper-saturated P3 / Rec.2020)
            flat = compress_to_srgb(flat)
            linear = flat.reshape(h, w_sz, d, 3)
        else:
            # SDR sRGB path
            linear = _srgb_to_linear(rgb)

        # ── 4. Channel-current gain equalisation ──────────────────────────────
        balanced = np.clip(linear * self._rgb_gain.reshape(1, 1, 1, 3), 0.0, None)
        peak = np.maximum(balanced.max(axis=-1, keepdims=True), 1.0)
        balanced = balanced / peak

        # ── 5. Per-pixel saturation and luminance ──────────────────────────────
        max_ch = balanced.max(axis=-1)
        min_ch = balanced.min(axis=-1)
        sat    = np.where(
            max_ch > _EPSILON,
            (max_ch - min_ch) / np.maximum(max_ch, _EPSILON),
            0.0,
        )
        luma = (
            balanced[..., 0] * 0.2126 +
            balanced[..., 1] * 0.7152 +
            balanced[..., 2] * 0.0722
        )

        # ── 6. White-channel gating ────────────────────────────────────────────
        #
        # Wide-gamut inputs have genuine highly-saturated hues that the bulb
        # should attempt to reproduce with pure RGB (no white bleed).
        #
        # Threshold tuning for WiZ 9W E27 (wcr = 20):
        #   sat ≥ 0.28 → no white at all   (maximise gamut)
        #   sat < 0.06 → white carries all  (neutral whites look clean)
        #   between    → smooth cross-fade

        white_ratio = self.profile.white_ratio   # 0.20

        # Saturation gate
        sat_gate = _smoothstep01(np.clip((0.26 - sat) / max(0.20, _EPSILON), 0.0, 1.0))
        # Brightness gate (don't add white to very dark pixels)
        bright_gate = _smoothstep01(np.clip((luma - 0.022) / 0.26, 0.0, 1.0))
        # Neutral boost: near-grey pixels should appear via white channels
        neutral_boost = np.where(sat < 0.05, 1.0, 1.0 - _smoothstep01(np.clip(sat / 0.26, 0.0, 1.0)))

        white_linear = (
            min_ch
            * sat_gate
            * bright_gate
            * (0.58 + white_ratio * 0.30 + neutral_boost * 0.22)
        )

        # Pastel mid-sat dimming
        pastel = (sat >= 0.06) & (sat < 0.26)
        white_linear = np.where(
            pastel,
            white_linear * np.clip(1.0 - (sat - 0.06) / 0.20, 0.25, 1.0),
            white_linear,
        )

        # Pure neutrals: white channels carry nearly everything
        neutral_mask = sat < 0.04
        white_linear = np.where(
            neutral_mask,
            np.maximum(white_linear, max_ch * min(1.0, 0.88 + white_ratio * 0.25)),
            white_linear,
        )

        # ── 7. RGB residual ────────────────────────────────────────────────────
        rgb_lin = np.clip(balanced - white_linear[..., None], 0.0, None)
        rgb_lin[sat < 0.04] *= 0.06    # hard-zero residual for near-grey

        # ── 8. Encode to uint8 ─────────────────────────────────────────────────
        rgb_enc = np.clip(np.round(_linear_to_srgb(rgb_lin) * 255.0), 0, 255).astype(np.uint8)
        w_enc   = np.clip(
            np.round(_linear_to_srgb(np.clip(white_linear * (1.0 + white_ratio * 0.20), 0.0, 1.0)) * 255.0),
            0, 255,
        ).astype(np.uint8)

        # ── 9. Cold / warm split for RGBCW ────────────────────────────────────
        if not self._dual_white:
            return np.concatenate((rgb_enc, w_enc[..., None]), axis=-1)

        # Derive cold/warm fraction from the input's blue/red energy.
        # Use the original linearised source (not balanced) for colour-temperature cue.
        blue_e  = np.clip(linear[..., 2], 0.0, 1.0)
        red_e   = np.clip(linear[..., 0], 0.0, 1.0)
        cool_raw = blue_e / np.maximum(blue_e + red_e, _EPSILON)

        # Suppress cold split for saturated colours (avoid blue tint)
        sat_sup   = _smoothstep01(np.clip((0.16 - sat) / 0.16, 0.0, 1.0))
        cool_mix  = cool_raw * sat_sup

        cold_w = np.clip(np.round(w_enc * cool_mix), 0, 255).astype(np.uint8)
        warm_w = np.clip(w_enc - cold_w, 0, 255).astype(np.uint8)

        return np.concatenate(
            (rgb_enc, cold_w[..., None], warm_w[..., None]),
            axis=-1,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def map_rgb(self, color: tuple[int, int, int]) -> tuple[int, ...]:
        """Map one (R,G,B) tuple → RGBW or RGBCW WiZ payload.

        The input is assumed to be in the color space declared in
        ``profile.source_color_space`` (default: sRGB).
        """
        r, g, b = (max(0, min(255, int(ch))) for ch in color)
        ri = min(_GRID_MAX_INDEX, int(round(r / 255.0 * _GRID_MAX_INDEX)))
        gi = min(_GRID_MAX_INDEX, int(round(g / 255.0 * _GRID_MAX_INDEX)))
        bi = min(_GRID_MAX_INDEX, int(round(b / 255.0 * _GRID_MAX_INDEX)))
        return tuple(int(v) for v in self._lookup[ri, gi, bi].tolist())
