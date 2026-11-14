"""Physical (spectral, additive-mixing) model of a WiZ RGBCW bulb.

The heuristic mapper in ``color_mapping.py`` never models what color the
bulb actually emits: saturated colors pass through as an almost-linear
channel-gain scale, and low-saturation colors are routed to the white
channels via hand-tuned saturation/brightness thresholds. Neither path
solves an equation for "what mix of the bulb's 5 real LEDs reproduces this
target color" -- so hue accuracy depends entirely on the WiZ LED primaries
happening to roughly line up with sRGB's, which they don't (a 625nm red LED
is not sRGB's red primary; a phosphor white LED is not neutral gray).

This module treats the bulb as what it physically is: 5 independent,
additive light sources (R/G/B color LEDs + cold-white + warm-white), each
with its own CIE XYZ tristimulus value derived from its documented peak
wavelength / color temperature (see color_science.py's CIE 1931 CMF
integration). Given a target color, it solves the exact linear-mixing
equation for the channel weights that reproduce it, instead of guessing.

Model boundary (stated explicitly, since this cannot be verified without a
spectrometer/colorimeter -- see docs/hardware-color-accuracy.md and the
three prior failed camera-calibration attempts):
  * LED spectra are modeled as Gaussians of an assumed FWHM (real LED SPDs
    are close to but not exactly Gaussian).
  * White LEDs are modeled as blackbody radiators at their documented CCT
    (real phosphor-converted whites have a blue pump peak + broad phosphor
    hump, not a smooth Planckian curve).
  * Relative luminous output between channels is inferred from the
    hardware's reported drive currents and typical luminous output per
    milliamp for each LED type, since no per-channel luminous-flux
    measurement is available.
  * Channel values are sent as linear drive fractions; the bulb's real
    value-to-light response has not been measured.
These are the same class of engineering estimate the existing heuristic
mapper already makes (e.g. its channel-current-based gain correction) --
this model makes the assumptions explicit and solves a real equation with
them, rather than encoding them into hand-tuned thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product
from typing import Optional, Protocol

import numpy as np

from .color_science import (
    SRGB_TO_XYZ,
    XYZ_TO_SRGB,
    blackbody_xyz,
    led_gaussian_xyz,
    linear_srgb_to_oklab,
    oklab_to_linear_srgb,
)

# WiZ 9W E27 documented LED peak wavelengths (see color_mapping.py header).
_LED_WAVELENGTHS_NM = {"r": 625.0, "g": 530.0, "b": 455.0}
# Assumed spectral FWHM per channel (typical values for these LED colors;
# green LEDs are usually broader than red/blue -- see module docstring).
_LED_FWHM_NM = {"r": 20.0, "g": 30.0, "b": 20.0}

# Typical luminous output per milliamp of drive current for each LED type,
# in relative units. An earlier version took drive current alone as the
# luminance of every channel, which made the blue LED two-thirds as bright as
# red. A 455 nm emitter sits where the eye's sensitivity is about 5% of its
# peak, so per unit of current a blue die yields roughly a quarter of red's
# lumens and an eighth of green's, and a phosphor white roughly matches green.
# Derived from typical radiant output (red ~0.9, green ~0.66, blue ~1.65 mW
# per mA) times luminous efficacy of radiation at each peak (219, 589 and
# 33 lm/W), and ~120 lm/W at ~3 V for phosphor white. These are typical-part
# figures, not measurements of any particular bulb.
_LUMENS_PER_MILLIAMP = {"r": 197.0, "g": 389.0, "b": 54.0, "white": 360.0}
# White-LED drive current relative to the strongest colour LED when the bulb
# does not report one (the WiZ ESP25 RGBW module reports 24 mA against 9 mA).
_DEFAULT_WHITE_CURRENT_RATIO = 2.7

_GAMUT_EPSILON = 1e-6  # tolerance for "is this weight within [0,1]"


class _ProfileLike(Protocol):
    """Duck-typed subset of BulbColorProfile this module needs.

    Avoids importing color_mapping.BulbColorProfile directly: color_mapping
    imports THIS module to offer the physical mixing path, so a direct
    import back would be circular.
    """

    white_channels: int
    cct_cool_kelvin: float
    cct_warm_kelvin: float
    rgb_channel_current: tuple[int, int, int]
    white_channel_current: tuple[int, ...]

    @property
    def white_ratio(self) -> float: ...


@dataclass(frozen=True)
class PhysicalPrimaries:
    """A bulb's LED channels' CIE XYZ tristimulus values at full drive.

    ``xyz`` is (N, 3): the first 3 rows are always R, G, B; any remaining
    rows (``white_indices``) are the white channel(s) -- 1 for RGBW bulbs,
    2 (cold + warm) for RGBCW bulbs, matching BulbColorProfile.white_channels.
    """

    xyz: np.ndarray
    white_indices: tuple[int, ...]

    @classmethod
    def from_profile(cls, profile: _ProfileLike) -> "PhysicalPrimaries":
        raw = [
            led_gaussian_xyz(_LED_WAVELENGTHS_NM[name], _LED_FWHM_NM[name])
            for name in ("r", "g", "b")
        ]
        # Relative luminance of each channel: reported drive current times the
        # typical luminous output per milliamp of that LED type (see
        # _LUMENS_PER_MILLIAMP), normalized so the brightest colour LED is 1.
        currents = np.clip(np.array(profile.rgb_channel_current[:3], dtype=np.float64), 1.0, None)
        per_ma = np.array([_LUMENS_PER_MILLIAMP[name] for name in ("r", "g", "b")])
        colour_lumens = currents * per_ma
        scale = float(colour_lumens.max())
        rel_luminance = list(colour_lumens / scale)

        # White channels use their own reported drive current when the bulb
        # provides one; otherwise a typical ratio to the colour LEDs. This is
        # NOT profile.white_ratio (white_to_color_ratio): that field is a
        # blend-threshold knob in the heuristic mapper, not a flux ratio.
        reported_white = tuple(
            float(value) for value in getattr(profile, "white_channel_current", ()) if float(value) > 0
        )
        default_white = float(currents.max()) * _DEFAULT_WHITE_CURRENT_RATIO

        def white_luminance(index: int) -> float:
            current = reported_white[index] if index < len(reported_white) else default_white
            return current * _LUMENS_PER_MILLIAMP["white"] / scale

        dual_white = int(getattr(profile, "white_channels", 1)) >= 2
        if int(getattr(profile, "white_channels", 1)) <= 0:
            white_indices = ()
        elif dual_white:
            raw += [blackbody_xyz(profile.cct_cool_kelvin), blackbody_xyz(profile.cct_warm_kelvin)]
            rel_luminance += [white_luminance(0), white_luminance(1)]
            white_indices = (3, 4)
        else:
            # Single-white bulbs have no distinct CCT field; a WiZ RGBW
            # variant's white channel is typically soft-white, so this uses
            # cct_warm_kelvin as the representative CCT (a stated estimate,
            # like the other spectral assumptions in this module).
            raw += [blackbody_xyz(profile.cct_warm_kelvin)]
            rel_luminance += [white_luminance(0)]
            white_indices = (3,)

        rows = []
        for xyz, scale in zip(raw, rel_luminance):
            y = xyz[1]
            unit = xyz / y if y > _GAMUT_EPSILON else xyz
            rows.append(unit * scale)
        return cls(xyz=np.stack(rows, axis=0), white_indices=white_indices)

    def mix(self, weights: np.ndarray) -> np.ndarray:
        """Additive mix: sum(weight_i * primary_i_XYZ)."""

        return weights @ self.xyz


def _valid_triangles(primaries: PhysicalPrimaries) -> list[tuple[int, int, int]]:
    """Independent triples used as free variables in bounded active-set solves.

    A feasible vertex can also have OTHER channels fixed at their upper bounds.
    Zero-offset triples alone do not cover the full bounded reachable set.
    """

    n_channels = primaries.xyz.shape[0]
    triples = []
    for combo in combinations(range(n_channels), 3):
        sub = primaries.xyz[list(combo)]
        if abs(np.linalg.det(sub)) > 1e-9:
            triples.append(combo)
    return triples


def _solve_triangle(target_xyz: np.ndarray, primaries: PhysicalPrimaries, combo: tuple[int, int, int]) -> Optional[np.ndarray]:
    """Exact 3x3 solve for this triple's weights; None if any weight is out of [0,1]."""

    sub = primaries.xyz[list(combo)].T  # (3,3): columns are the 3 primaries' XYZ
    try:
        weights = np.linalg.solve(sub, target_xyz)
    except np.linalg.LinAlgError:
        return None
    if np.all(weights >= -_GAMUT_EPSILON) and np.all(weights <= 1.0 + _GAMUT_EPSILON):
        return np.clip(weights, 0.0, 1.0)
    return None


def _bounded_mixes(targets, primaries, triangles):
    """Enumerate feasible vertices without adding a numerical solver dependency.

    XYZ has rank three. A vertex has at most three free channel weights;
    every remaining channel is at zero or one. Test all those bounds, then
    prefer the feasible vertex requiring the least total channel drive.
    Process one cell at a time to keep LUT construction memory bounded.
    """
    targets = np.asarray(targets, dtype=np.float64).reshape(-1, 3)
    n_channels = primaries.xyz.shape[0]
    result = np.zeros((len(targets), n_channels), dtype=np.float64)
    best = np.full(len(targets), np.inf)
    for combo in triangles:
        other = [index for index in range(n_channels) if index not in combo]
        inverse = np.linalg.inv(primaries.xyz[list(combo)].T)
        for bounds in product((0.0, 1.0), repeat=len(other)):
            fixed = np.zeros(n_channels)
            fixed[other] = bounds
            offset = fixed @ primaries.xyz
            free = (targets - offset) @ inverse.T
            valid = np.all(free >= -_GAMUT_EPSILON, axis=1) & np.all(free <= 1.0 + _GAMUT_EPSILON, axis=1)
            free = np.clip(free, 0, 1)
            score = free.sum(axis=1) + sum(bounds)
            take = valid & (score < best - 1e-12)
            if np.any(take):
                result[take] = fixed
                result[np.ix_(take, combo)] = free[take]
                best[take] = score[take]
    return result, np.isfinite(best)


def _best_valid_mix(target_xyz, primaries, triangles):
    result, valid = _bounded_mixes(np.asarray(target_xyz).reshape(1, 3), primaries, triangles)
    return result[0] if valid[0] else None


def _compress_toward_gray(target_xyz: np.ndarray, primaries: PhysicalPrimaries, triangles: list[tuple[int, int, int]]) -> np.ndarray:
    """Hue-preserving chroma compression (constant Oklab lightness) via bisection.

    Simplified relative to Ottosson's full adaptive-L0 blend (see
    https://bottosson.github.io/posts/gamutclipping/): this compresses
    straight down to zero chroma at the TARGET's own lightness, rather than
    blending toward a lightness-dependent anchor. Ottosson notes this
    "preserve lightness" strategy can strand a target whose lightness itself
    is unreachable at any chroma for a small/cusped gamut -- not a practical
    concern here, since the bulb's white channel(s) give a wide reachable
    lightness range at chroma=0 (full white lightness), so this simpler
    variant is a reasonable, more tractable choice for this device.
    """

    lab = linear_srgb_to_oklab(XYZ_TO_SRGB @ target_xyz)
    gray_lab = np.array([lab[0], 0.0, 0.0])

    def reachable(t: float) -> Optional[np.ndarray]:
        candidate_lab = lab + (gray_lab - lab) * t
        candidate_xyz = SRGB_TO_XYZ @ oklab_to_linear_srgb(candidate_lab)
        return _best_valid_mix(candidate_xyz, primaries, triangles)

    # If even pure gray at this lightness is unreachable, the LIGHTNESS
    # itself is out of range (e.g. requesting brighter than the bulb's max
    # achievable white) -- pull it toward mid-gray lightness first.
    if reachable(1.0) is None:
        mid_lab = np.array([0.6, 0.0, 0.0])  # a lightness white LEDs can always hit
        lo_l, hi_l = 0.0, 1.0
        for _ in range(24):
            mid = (lo_l + hi_l) / 2.0
            probe_lab = lab * (1 - mid) + mid_lab * mid
            probe_xyz = SRGB_TO_XYZ @ oklab_to_linear_srgb(probe_lab)
            if _best_valid_mix(probe_xyz, primaries, triangles) is not None:
                hi_l = mid
            else:
                lo_l = mid
        lab = lab * (1 - hi_l) + mid_lab * hi_l
        gray_lab = np.array([lab[0], 0.0, 0.0])

    lo, hi = 0.0, 1.0  # t=0 -> original (already known unreachable); t=1 -> gray (reachable)
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if reachable(mid) is not None:
            hi = mid
        else:
            lo = mid
    candidate_lab = lab + (gray_lab - lab) * hi
    return SRGB_TO_XYZ @ oklab_to_linear_srgb(candidate_lab)


def solve_channel_mix_batch(
    target_xyz: np.ndarray,
    primaries: PhysicalPrimaries,
    triangles: Optional[list[tuple[int, int, int]]] = None,
) -> np.ndarray:
    """Vectorized solve_channel_mix over a whole (N, 3) batch of targets.

    Building a 33^3 LUT one point at a time (~185us/call, all in Python) is
    ~7s of blocking numpy calls -- fine as a one-off script, not fine inside
    an async bulb-setup call. This batches the exact 3x3 solves for every
    combo across all N points at once (matching the existing heuristic
    mapper's own fully-vectorized style in color_mapping.py), falling back
    to the slower per-point compression path only for the (typically small)
    subset of points that are genuinely outside the bulb's gamut.
    """

    if triangles is None:
        triangles = _valid_triangles(primaries)

    targets = np.asarray(target_xyz, dtype=np.float64).reshape(-1, 3)
    n_channels = primaries.xyz.shape[0]
    result, any_valid = _bounded_mixes(targets, primaries, triangles)

    # Fallback (per-point, slower) for targets outside every triangle -- the
    # out-of-gamut compression path isn't practically vectorizable (each
    # point needs its own bisection), but this subset is normally small.
    out_of_gamut = np.where(~any_valid)[0]
    for idx in out_of_gamut:
        result[idx] = solve_channel_mix(targets[idx], primaries, triangles)

    return result.reshape(np.asarray(target_xyz).shape[:-1] + (n_channels,))


def solve_channel_mix(
    target_xyz: np.ndarray,
    primaries: PhysicalPrimaries,
    triangles: Optional[list[tuple[int, int, int]]] = None,
) -> np.ndarray:
    """Solve for the 5 channel weights (r,g,b,c,w in [0,1]) that reproduce target_xyz.

    Exact when the target is within the bulb's reachable gamut (a zonotope);
    otherwise the target is compressed toward gray at constant Oklab
    lightness (hue-preserving) until it becomes reachable.
    """

    if triangles is None:
        triangles = _valid_triangles(primaries)

    mix = _best_valid_mix(target_xyz, primaries, triangles)
    if mix is not None:
        return mix

    compressed_xyz = _compress_toward_gray(target_xyz, primaries, triangles)
    mix = _best_valid_mix(compressed_xyz, primaries, triangles)
    return mix if mix is not None else np.zeros(primaries.xyz.shape[0], dtype=np.float64)
