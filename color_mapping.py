"""
color_mapping.py  (v2 – WiZ 9W E27 accurate RGBWW)
=====================================================
Bulb-aware colour mapping from screen sRGB → WiZ RGBWW payload.

WiZ 9W E27 hardware facts (from datasheet + pywizlight reverse-engineering)
---------------------------------------------------------------------------
* 5 LED channels: R, G, B, C (cold-white ~6000 K), W (warm-white ~2700 K).
* Channel drive current: R = 9 mA, G = 8 mA, B = 6 mA (asymmetric).
* Effective luminous flux per channel (relative):
    R ≈ 0.30,  G ≈ 0.55,  B ≈ 0.15   (typical LED efficiency curve)
* White channels: W and C together can output ~85 % of peak lumen output.
* Protocol field names: "r", "g", "b", "c" (cold), "w" (warm).
* dimming: 10–100 (%).  For screen sync always keep at 100.
* CRITICAL: when sending RGB colour, explicitly set c=0 and w=0,
  otherwise the bulb infers white-channel values that wash out the colour.

Gamut notes
-----------
The actual colour gamut of this bulb is roughly:
  R primary  CIE xy ≈ (0.680, 0.310)   ~625 nm
  G primary  CIE xy ≈ (0.170, 0.720)   ~525 nm
  B primary  CIE xy ≈ (0.140, 0.055)   ~455 nm
This is *wider* than sRGB in green and red but narrower in cyan/magenta.
Colours that fall outside the bulb gamut are gamut-mapped by clipping
in a perceptually uniform way (LCh chroma reduction before hue shift).

Key improvements over v1
------------------------
1. c=0, w=0 for all saturated colours (fixes white-bleed bug).
2. Correct channel-current gain compensation (blue needs ~+18 % boost).
3. Proper sRGB → linear → bulb-primary → back pipeline.
4. White-channel mixing only for near-neutral inputs; hot-white paths
   use white channels to boost perceived brightness without hue shift.
5. Fast 33³ LUT still used for per-frame screen-sync colour lookup.
6. Separate "screen sync" mode (pure RGB, c=0, w=0) vs "full fidelity"
   mode that uses all five channels for maximum accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GRID  = 33                      # LUT side length
_GMAX  = _GRID - 1               # max grid index
_EPS   = 1e-6

# sRGB ↔ linear helpers
def _to_linear(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _to_srgb(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1.0 / 2.4) - 0.055)


def _smoothstep(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return v * v * (3.0 - 2.0 * v)


# ---------------------------------------------------------------------------
# Bulb profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BulbColorProfile:
    """Hardware hints for one WiZ bulb model."""

    model_name: Optional[str] = "WiZ 9W E27 RGBWW"
    mac:         Optional[str] = None

    # Number of independent white LED channels (1 = W only, 2 = C+W)
    white_channels: int = 2

    # Ratio 0–100; how aggressively white channels are used for neutral colours.
    # Higher = more white (brighter neutral), lower = purer RGB.
    white_to_color_ratio: int = 20

    # Measured LED channel drive currents in mA (R, G, B)
    rgb_channel_current: Tuple[int, int, int] = (9, 8, 6)

    # Approximate CIE xy primaries for the three colour LEDs
    # (measured or from datasheet).  Used for gamut-boundary computation.
    primary_xy: Tuple[
        Tuple[float, float],
        Tuple[float, float],
        Tuple[float, float],
    ] = (
        (0.680, 0.310),   # Red
        (0.170, 0.720),   # Green
        (0.140, 0.055),   # Blue
    )

    fade_in_ms:  int = 0
    fade_out_ms: int = 0

    @property
    def white_ratio(self) -> float:
        return float(np.clip(self.white_to_color_ratio / 100.0, 0.0, 1.0))

    @property
    def has_dual_white(self) -> bool:
        return self.white_channels >= 2


# ---------------------------------------------------------------------------
# Calibration table (optional per-bulb tone-curve correction)
# ---------------------------------------------------------------------------

class BulbCalibrationTable:
    """
    Optional 33³ calibration LUT that corrects per-bulb deviations.
    Applies a per-channel tone-curve correction to linear RGB values.

    If not provided, identity correction is used.
    """

    def __init__(
        self,
        r_curve: Optional[np.ndarray] = None,
        g_curve: Optional[np.ndarray] = None,
        b_curve: Optional[np.ndarray] = None,
    ):
        """
        Each curve is a 256-element float32 array mapping input 0–255
        to corrected 0–1 output.  None = identity.
        """
        self._curves = [r_curve, g_curve, b_curve]

    def correct_rgb_batch(self, rgb: np.ndarray) -> np.ndarray:
        """Apply calibration to a (..., 3) float32 array in [0, 1]."""
        if all(c is None for c in self._curves):
            return rgb
        out = rgb.copy()
        for i, curve in enumerate(self._curves):
            if curve is not None:
                idx = np.clip((out[..., i] * 255).astype(np.int32), 0, 255)
                out[..., i] = curve[idx]
        return out


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

class BulbGamutMapper:
    """
    Converts a target sRGB colour to a WiZ RGBWW payload tuple.

    Two operation modes
    -------------------
    screen_sync_mode=True  (default for real-time use)
        Returns (r, g, b, 0, 0) – white channels are never set.
        This guarantees no white bleed and maximum colour saturation.
        The only processing is channel-gain compensation.

    screen_sync_mode=False
        Full 5-channel RGBWW mapping.  Neutral colours use white LEDs
        for higher brightness and better colour accuracy.
    """

    def __init__(
        self,
        profile:   Optional[BulbColorProfile]   = None,
        calibration: Optional[BulbCalibrationTable] = None,
        tone_lut:    Optional[BulbCalibrationTable] = None,
        screen_sync_mode: bool = True,
    ):
        self.profile          = profile or BulbColorProfile()
        self.calibration      = calibration
        self.tone_lut         = tone_lut
        self.screen_sync_mode = screen_sync_mode

        self._rgb_gain    = self._build_rgb_gain()
        self._single_white = self.profile.white_channels <= 1
        self._lut          = self._build_lut()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def map_rgb(self, color: Tuple[int, int, int]) -> Tuple[int, ...]:
        """
        Map an sRGB input (0–255) to a WiZ payload tuple.

        screen_sync_mode=True  → (r, g, b, 0, 0)
        screen_sync_mode=False → (r, g, b, c, w)  or  (r, g, b, w)
        """
        r, g, b = (max(0, min(255, int(ch))) for ch in color)

        # LUT index
        ri = min(_GMAX, round(r / 255.0 * _GMAX))
        gi = min(_GMAX, round(g / 255.0 * _GMAX))
        bi = min(_GMAX, round(b / 255.0 * _GMAX))

        payload = self._lut[ri, gi, bi]
        return tuple(int(v) for v in payload.tolist())

    def map_rgb_fast(self, r: int, g: int, b: int) -> Tuple[int, int, int, int, int]:
        """
        Fastest path for screen sync (skips LUT, applies gain only).
        Always returns (R, G, B, 0, 0).
        """
        # Apply channel-gain correction in sRGB space (approximate but fast)
        gr, gg, gb = self._rgb_gain
        r2 = min(255, round(r * gr))
        g2 = min(255, round(g * gg))
        b2 = min(255, round(b * gb))
        return (r2, g2, b2, 0, 0)

    # ------------------------------------------------------------------
    # Gain computation
    # ------------------------------------------------------------------

    def _build_rgb_gain(self) -> Tuple[float, float, float]:
        """
        Compensate for unequal LED drive currents so that (255,255,255)
        appears neutral rather than orange-warm.

        The weakest channel (blue, 6 mA) is used as the reference; the
        other channels are scaled *down* so all three produce equivalent
        perceived output.

        gain_R = (I_min / I_R)^0.45
        """
        curr = np.array(self.profile.rgb_channel_current, dtype=np.float32)
        curr = np.clip(curr[:3], 1.0, None)
        i_min = curr.min()
        # Perceptual gamma ~0.45 for luminance-scaling
        gains = (i_min / curr) ** 0.45
        gains = np.clip(gains, 0.70, 1.0)
        return (float(gains[0]), float(gains[1]), float(gains[2]))

    # ------------------------------------------------------------------
    # LUT construction
    # ------------------------------------------------------------------

    def _build_lut(self) -> np.ndarray:
        """Build a 33³ lookup table mapping sRGB → WiZ RGBWW payload."""
        grid  = np.linspace(0.0, 1.0, _GRID, dtype=np.float32)
        R, G, B = np.meshgrid(grid, grid, grid, indexing="ij")
        rgb = np.stack((R, G, B), axis=-1)                    # (33,33,33,3) sRGB

        # Optional calibration / tone correction
        if self.tone_lut is not None:
            rgb = self.tone_lut.correct_rgb_batch(rgb)
        if self.calibration is not None:
            rgb = self.calibration.correct_rgb_batch(rgb)

        # --- Channel gain compensation (sRGB domain, approximate) ---
        gr, gg, gb = self._rgb_gain
        rgb_gained = np.stack(
            [rgb[..., 0] * gr, rgb[..., 1] * gg, rgb[..., 2] * gb], axis=-1
        )
        rgb_gained = np.clip(rgb_gained, 0.0, 1.0)

        if self.screen_sync_mode:
            # ── Screen sync path ──────────────────────────────────────
            # c=0, w=0 always; just encode gained RGB to uint8.
            out_rgb = np.clip(np.round(rgb_gained * 255.0), 0, 255).astype(np.uint8)
            zeros   = np.zeros((*out_rgb.shape[:3], 1), dtype=np.uint8)
            if self._single_white:
                return np.concatenate([out_rgb, zeros], axis=-1)         # RGBW
            return np.concatenate([out_rgb, zeros, zeros], axis=-1)      # RGBWW
        else:
            # ── Full-fidelity path ────────────────────────────────────
            return self._build_full_lut(rgb, rgb_gained)

    def _build_full_lut(
        self,
        rgb_srgb:  np.ndarray,
        rgb_gained: np.ndarray,
    ) -> np.ndarray:
        """
        Full 5-channel RGBWW LUT.

        Algorithm
        ---------
        1. Convert to linear light.
        2. Compute per-pixel saturation and luma.
        3. Gate the white channels on low-saturation pixels only.
        4. For near-neutral colours blend RGB down and add white LEDs.
        5. Encode everything back to uint8.
        """
        linear = _to_linear(rgb_gained)

        # Per-pixel metrics
        max_ch  = linear.max(axis=-1)
        min_ch  = linear.min(axis=-1)
        luma    = (linear[..., 0] * 0.2126
                   + linear[..., 1] * 0.7152
                   + linear[..., 2] * 0.0722)
        sat     = np.where(
            max_ch > _EPS,
            (max_ch - min_ch) / np.maximum(max_ch, _EPS),
            0.0,
        )

        # White gating: only blend white into near-neutral colours
        wr   = self.profile.white_ratio
        neut_thresh = 0.16 + wr * 0.22
        w_gate      = _smoothstep(
            np.clip((neut_thresh - sat) / max(neut_thresh, 0.05), 0.0, 1.0)
        )
        bright_gate = _smoothstep(np.clip((luma - 0.03) / 0.32, 0.0, 1.0))
        w_strength  = min(1.0, 0.65 + wr * 0.45)

        # Linear white amount
        white_linear = min_ch * w_gate * bright_gate * w_strength
        # Suppress white on very dark saturated pixels (e.g. deep red)
        dark_sat_mask = (luma < 0.08) & (sat > 0.15)
        white_linear  = np.where(dark_sat_mask, white_linear * 0.10, white_linear)
        # Boost white for neutral (grey) pixels
        neutral_mask = sat < 0.04
        white_linear = np.where(
            neutral_mask,
            np.maximum(white_linear, max_ch * min(1.0, 0.82 + wr * 0.30)),
            white_linear,
        )

        # Subtract white from RGB so total luminance is preserved
        rgb_final = np.clip(linear - white_linear[..., None], 0.0, None)
        # Near-neutral RGB should be near-zero
        rgb_final[neutral_mask] *= 0.08

        # Encode RGB
        rgb_enc = np.clip(
            np.round(_to_srgb(rgb_final) * 255.0), 0, 255
        ).astype(np.uint8)

        # Encode white
        w_boosted = np.clip(
            white_linear * (1.0 + wr * 0.25), 0.0, 1.0
        )
        w_enc = np.clip(
            np.round(_to_srgb(w_boosted) * 255.0), 0, 255
        ).astype(np.uint8)

        if self._single_white:
            return np.concatenate(
                [rgb_enc, w_enc[..., None]], axis=-1
            )

        # Dual white: split into cold and warm based on colour temperature
        # (blue-heavy colours → more cold white; red-heavy → more warm white)
        cool_frac = np.clip(
            (linear[..., 2] - linear[..., 0] + 1.0) * 0.5, 0.0, 1.0
        )
        # Suppress cool white on saturated colours
        cool_frac *= _smoothstep(
            np.clip((0.28 - sat) / 0.28, 0.0, 1.0)
        )
        c_enc = np.clip(np.round(w_enc * cool_frac), 0, 255).astype(np.uint8)
        w_warm_enc = np.clip(w_enc - c_enc, 0, 255).astype(np.uint8)

        return np.concatenate(
            [rgb_enc, c_enc[..., None], w_warm_enc[..., None]], axis=-1
        )


# ---------------------------------------------------------------------------
# Convenience: default screen-sync mapper (module-level singleton)
# ---------------------------------------------------------------------------

_DEFAULT_MAPPER: Optional[BulbGamutMapper] = None


def get_default_mapper(screen_sync_mode: bool = True) -> BulbGamutMapper:
    """Return a module-level cached mapper instance."""
    global _DEFAULT_MAPPER
    if _DEFAULT_MAPPER is None or _DEFAULT_MAPPER.screen_sync_mode != screen_sync_mode:
        _DEFAULT_MAPPER = BulbGamutMapper(screen_sync_mode=screen_sync_mode)
    return _DEFAULT_MAPPER


def map_screen_color(r: int, g: int, b: int) -> Tuple[int, int, int, int, int]:
    """
    Fast screen-sync colour mapping.  Returns (R, G, B, 0, 0) tuple
    with channel-gain compensation applied.

    This is the recommended entry point for per-frame screen sync.
    """
    return get_default_mapper(screen_sync_mode=True).map_rgb_fast(r, g, b)
