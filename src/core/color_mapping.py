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
    device_profile: str = "generic"                    # "generic" | "bravia" | "wide_gamut_hdr" | "mac"
    cct_warm_kelvin: float = 2700.0
    cct_cool_kelvin: float = 6500.0
    cool_mix_bias: float = 0.0                         # added to coolMix per profile (Bravia: +0.10)

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
        self._gray_lookup = self._build_gray_lookup()

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
        return self._build_payload_volume(rgb)

    def _build_gray_lookup(self) -> np.ndarray:
        """Dense 256-entry payload ramp for exact neutrals.

        Trilinear interpolation over the 33³ lattice smears the neutral
        diagonal: a gray input blends 8 corners of which 6 are off-diagonal
        and still carry RGB energy, so whites/B&W came out part-RGB instead of
        riding the white LEDs. ``map_rgb`` blends toward this exact-gray ramp
        for near-neutral inputs.
        """

        values = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        diagonal = np.stack((values, values, values), axis=-1).reshape(256, 1, 1, 3)
        return self._build_payload_volume(diagonal).reshape(256, -1)

    def _build_payload_volume(self, rgb: np.ndarray) -> np.ndarray:
        """Run the full color pipeline over any (..., 3) grid of source colors."""

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

        # RGB-only fixtures cannot use energy assigned to a white channel.
        if self.profile.white_channels <= 0:
            rgb_enc = np.clip(np.round(_linear_to_srgb(balanced) * 255.0), 0, 255).astype(np.uint8)
            return np.concatenate((rgb_enc, np.zeros_like(rgb_enc[..., :1])), axis=-1)

        # ── 5. Per-pixel saturation and luminance ──────────────────────────────
        #
        # IMPORTANT: neutrality must be judged on the ORIGINAL linear color,
        # not the channel-gain-balanced one. The R/G/B drive-current gains
        # (0.83/0.88/1.0) turn a perfect gray into sat≈0.17 after balancing,
        # which used to leak most white-content energy into the RGB LEDs —
        # whites/B&W came out tinted and under-white on the bulb.
        min_ch = balanced.min(axis=-1)          # white carrier follows drive levels
        lin_max = linear.max(axis=-1)
        lin_min = linear.min(axis=-1)
        sat = np.where(
            lin_max > _EPSILON,
            (lin_max - lin_min) / np.maximum(lin_max, _EPSILON),
            0.0,
        )
        luma = (
            linear[..., 0] * 0.2126 +
            linear[..., 1] * 0.7152 +
            linear[..., 2] * 0.0722
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
            np.maximum(white_linear, balanced.max(axis=-1) * min(1.0, 0.88 + white_ratio * 0.25)),
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

        # CCT-based cold/warm split (McCamy approximation, vectorised).
        # We use the linearised source (pre-balance) so the colour-temperature
        # cue reflects the original scene, not the channel-current correction.
        rl = np.clip(linear[..., 0], 0.0, 1.0)
        gl = np.clip(linear[..., 1], 0.0, 1.0)
        bl = np.clip(linear[..., 2], 0.0, 1.0)
        X = 0.4124564 * rl + 0.3575761 * gl + 0.1804375 * bl
        Y = 0.2126729 * rl + 0.7151522 * gl + 0.0721750 * bl
        Z = 0.0193339 * rl + 0.1191920 * gl + 0.9503041 * bl
        s = np.maximum(X + Y + Z, _EPSILON)
        x = X / s
        y = Y / s
        denom = 0.1858 - y
        # When denom is ~0 (achromatic), fall back to mid CCT (5500K)
        n = np.where(np.abs(denom) > 1e-4, (x - 0.3320) / np.where(np.abs(denom) > 1e-4, denom, 1.0), 0.0)
        cct = 437.0 * n * n * n + 3601.0 * n * n + 6861.0 * n + 5517.0
        cct = np.clip(cct, 1500.0, 15000.0)
        warm_k = float(self.profile.cct_warm_kelvin)
        cool_k = float(self.profile.cct_cool_kelvin)
        cool_raw = np.clip((cct - warm_k) / max(cool_k - warm_k, 1.0), 0.0, 1.0)
        cool_raw = np.clip(cool_raw + float(self.profile.cool_mix_bias), 0.0, 1.0)

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

        Uses trilinear interpolation over the 33³ lattice: nearest-lattice
        lookup quantized inputs by ±4/channel, which is relatively large in
        dark scenes and caused visible stepping on slow ambient fades. Exact
        at lattice points; ~8 fetches + lerps per call (micro-fast).
        """
        r, g, b = (max(0, min(255, int(ch))) for ch in color)

        fr = r / 255.0 * _GRID_MAX_INDEX
        fg = g / 255.0 * _GRID_MAX_INDEX
        fb = b / 255.0 * _GRID_MAX_INDEX
        ri0, gi0, bi0 = int(fr), int(fg), int(fb)
        ri1 = min(ri0 + 1, _GRID_MAX_INDEX)
        gi1 = min(gi0 + 1, _GRID_MAX_INDEX)
        bi1 = min(bi0 + 1, _GRID_MAX_INDEX)
        tr, tg, tb = fr - ri0, fg - gi0, fb - bi0

        lut = self._lookup
        c000 = lut[ri0, gi0, bi0].astype(np.float64)
        c001 = lut[ri0, gi0, bi1].astype(np.float64)
        c010 = lut[ri0, gi1, bi0].astype(np.float64)
        c011 = lut[ri0, gi1, bi1].astype(np.float64)
        c100 = lut[ri1, gi0, bi0].astype(np.float64)
        c101 = lut[ri1, gi0, bi1].astype(np.float64)
        c110 = lut[ri1, gi1, bi0].astype(np.float64)
        c111 = lut[ri1, gi1, bi1].astype(np.float64)

        c00 = c000 + (c001 - c000) * tb
        c01 = c010 + (c011 - c010) * tb
        c10 = c100 + (c101 - c100) * tb
        c11 = c110 + (c111 - c110) * tb
        c0 = c00 + (c01 - c00) * tg
        c1 = c10 + (c11 - c10) * tg
        payload = c0 + (c1 - c0) * tr

        # Near-neutral inputs blend toward the exact-gray ramp so whites and
        # B&W content ride the white LEDs instead of the interpolation-smeared
        # RGB mix (see _build_gray_lookup). Full ramp below sat 0.04 (grays,
        # photographic whites, B&W grain), fading out by 0.12 (pastels keep a
        # gentle white assist; real colors are untouched).
        max_ch = max(r, g, b)
        if max_ch > 0:
            sat = (max_ch - min(r, g, b)) / max_ch
            if sat < 0.12:
                t = min(1.0, (0.12 - sat) / 0.08)
                blend = t * t * (3.0 - 2.0 * t)
                luma_index = int(round(r * 0.2126 + g * 0.7152 + b * 0.0722))
                gray = self._gray_lookup[max(0, min(255, luma_index))].astype(np.float64)
                payload = payload + (gray - payload) * blend

        return tuple(int(v) for v in np.clip(np.round(payload), 0, 255))
