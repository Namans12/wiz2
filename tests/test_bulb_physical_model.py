"""Tests for the physical (spectral, additive-mixing) bulb model.

These verify the MATH is correct and self-consistent (exact reconstruction
in-gamut, hue-preserving compression out-of-gamut, batch/single-point
equivalence) -- not that the model's real-world color accuracy is correct,
which cannot be verified without a spectrometer/colorimeter (see the module
docstring in bulb_physical_model.py for the stated, unverifiable assumptions).
"""

import colorsys

import numpy as np
import pytest

from src.core.bulb_physical_model import (
    PhysicalPrimaries,
    _best_valid_mix,
    _valid_triangles,
    solve_channel_mix,
    solve_channel_mix_batch,
)
from src.core.color_mapping import BulbColorProfile, BulbGamutMapper
from src.core.color_science import SRGB_TO_XYZ, srgb_decode


class _Profile:
    def __init__(self, white_channels=2, currents=(9, 8, 6), cool_k=6500.0, warm_k=2700.0):
        self.white_channels = white_channels
        self.rgb_channel_current = currents
        self.cct_cool_kelvin = cool_k
        self.cct_warm_kelvin = warm_k

    @property
    def white_ratio(self):
        return 0.20


def _srgb8_to_xyz(rgb):
    return SRGB_TO_XYZ @ srgb_decode(np.array(rgb, dtype=np.float64) / 255.0)


def _hue(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360.0


# ---------------------------------------------------------------------------
# CIE CMF / spectral primaries plausibility
# ---------------------------------------------------------------------------


def test_led_primaries_match_known_real_led_chromaticity_ranges():
    """Sanity-check against published typical LED chromaticity ranges.

    Not a hardware measurement of THIS bulb -- a plausibility check that the
    Wyman CMF fit + Gaussian SPD integration lands in the right ballpark for
    real narrowband LEDs at these wavelengths (documented published ranges).
    """
    from src.core.bulb_physical_model import _LED_FWHM_NM, _LED_WAVELENGTHS_NM
    from src.core.color_science import led_gaussian_xyz

    def xy(xyz):
        s = xyz.sum()
        return (xyz[0] / s, xyz[1] / s)

    red_xy = xy(led_gaussian_xyz(_LED_WAVELENGTHS_NM["r"], _LED_FWHM_NM["r"]))
    green_xy = xy(led_gaussian_xyz(_LED_WAVELENGTHS_NM["g"], _LED_FWHM_NM["g"]))
    blue_xy = xy(led_gaussian_xyz(_LED_WAVELENGTHS_NM["b"], _LED_FWHM_NM["b"]))

    assert 0.65 < red_xy[0] < 0.75 and 0.25 < red_xy[1] < 0.35
    assert 0.10 < green_xy[0] < 0.25 and 0.65 < green_xy[1] < 0.85
    assert 0.10 < blue_xy[0] < 0.20 and 0.0 < blue_xy[1] < 0.10


def test_blackbody_6504k_matches_known_d65_chromaticity():
    from src.core.color_science import blackbody_xyz

    xyz = blackbody_xyz(6504.0)
    s = xyz.sum()
    x, y = xyz[0] / s, xyz[1] / s
    # True D65 (a standard daylight illuminant, not exactly Planckian) is
    # x=0.3127, y=0.3290 -- a blackbody at the same CCT should be close.
    assert abs(x - 0.3127) < 0.01
    assert abs(y - 0.3290) < 0.01


# ---------------------------------------------------------------------------
# Primaries construction
# ---------------------------------------------------------------------------


def test_primaries_shape_and_white_indices_rgbcw():
    p = PhysicalPrimaries.from_profile(_Profile(white_channels=2))
    assert p.xyz.shape == (5, 3)
    assert p.white_indices == (3, 4)


def test_primaries_shape_and_white_indices_rgbw():
    p = PhysicalPrimaries.from_profile(_Profile(white_channels=1))
    assert p.xyz.shape == (4, 3)
    assert p.white_indices == (3,)


def test_primaries_all_have_positive_luminance():
    p = PhysicalPrimaries.from_profile(_Profile())
    assert np.all(p.xyz[:, 1] > 0)


# ---------------------------------------------------------------------------
# Solver correctness
# ---------------------------------------------------------------------------


def test_valid_triangles_complete_for_5_primaries():
    p = PhysicalPrimaries.from_profile(_Profile(white_channels=2))
    triangles = _valid_triangles(p)
    assert 1 <= len(triangles) <= 10
    for combo in triangles:
        assert len(combo) == 3
        assert len(set(combo)) == 3


@pytest.mark.parametrize(
    "color",
    [(255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 255, 255), (255, 0, 255),
     (255, 255, 255), (128, 128, 128), (30, 30, 30), (200, 150, 100), (80, 160, 255)],
)
def test_in_gamut_colors_reconstruct_exactly(color):
    p = PhysicalPrimaries.from_profile(_Profile())
    triangles = _valid_triangles(p)
    target = _srgb8_to_xyz(color)
    direct = _best_valid_mix(target, p, triangles)
    if direct is None:
        pytest.skip(f"{color} happens to be outside this profile's gamut")
    weights = solve_channel_mix(target, p, triangles)
    achieved = p.mix(weights)
    rel_err = np.linalg.norm(achieved - target) / max(np.linalg.norm(target), 1e-9)
    assert rel_err < 1e-6


def test_weights_always_in_valid_range():
    p = PhysicalPrimaries.from_profile(_Profile())
    triangles = _valid_triangles(p)
    rng = np.random.default_rng(0)
    for _ in range(50):
        color = rng.integers(0, 256, 3)
        target = _srgb8_to_xyz(color)
        weights = solve_channel_mix(target, p, triangles)
        assert np.all(weights >= -1e-6)
        assert np.all(weights <= 1.0 + 1e-6)


def test_out_of_gamut_compression_is_self_consistent_and_hue_preserving():
    """A target outside the reachable gamut must compress toward gray at
    constant hue, and the returned weights must exactly reconstruct the
    (compressed) color the solver actually used."""

    p = PhysicalPrimaries.from_profile(_Profile())
    triangles = _valid_triangles(p)
    # Pure blue (0,0,255) is a real example that falls outside this specific
    # LED-primary model's gamut (its 455nm primary's chromaticity does not
    # exactly match sRGB blue's less-saturated primary) -- verified directly
    # in this test rather than assumed.
    target = _srgb8_to_xyz((0, 0, 255))
    assert _best_valid_mix(target, p, triangles) is None, "test assumes this target is out-of-gamut"

    weights = solve_channel_mix(target, p, triangles)
    achieved = p.mix(weights)
    # Self-consistency: the returned weights reconstruct SOME color exactly.
    direct_for_achieved = _best_valid_mix(achieved, p, triangles)
    assert direct_for_achieved is not None
    assert np.linalg.norm(p.mix(direct_for_achieved) - achieved) < 1e-9

    from src.core.color_science import XYZ_TO_SRGB, linear_srgb_to_oklab

    target_lab = linear_srgb_to_oklab(XYZ_TO_SRGB @ target)
    achieved_lab = linear_srgb_to_oklab(XYZ_TO_SRGB @ achieved)
    hue_target = np.arctan2(target_lab[2], target_lab[1])
    hue_achieved = np.arctan2(achieved_lab[2], achieved_lab[1])
    assert abs(np.degrees(hue_target - hue_achieved)) < 5.0


def test_gray_colors_favor_white_channels():
    p = PhysicalPrimaries.from_profile(_Profile(white_channels=2))
    triangles = _valid_triangles(p)
    for gray in (20, 80, 136, 200):
        weights = solve_channel_mix(_srgb8_to_xyz((gray, gray, gray)), p, triangles)
        white_share = weights[3] + weights[4]
        assert white_share / max(weights.sum(), 1e-9) > 0.8


# ---------------------------------------------------------------------------
# Batch solver
# ---------------------------------------------------------------------------


def test_batch_solver_matches_single_point_solver():
    for white_channels in (1, 2):
        p = PhysicalPrimaries.from_profile(_Profile(white_channels=white_channels))
        triangles = _valid_triangles(p)
        rng = np.random.default_rng(1)
        colors = rng.integers(0, 256, (60, 3))
        targets = np.stack([_srgb8_to_xyz(c) for c in colors])
        batch = solve_channel_mix_batch(targets, p, triangles)
        for i in range(0, 60, 5):
            single = solve_channel_mix(targets[i], p, triangles)
            assert np.abs(single - batch[i]).max() < 1e-9


def test_batch_solver_handles_grid_shape():
    p = PhysicalPrimaries.from_profile(_Profile())
    grid = np.random.default_rng(2).random((4, 4, 4, 3))
    result = solve_channel_mix_batch(grid, p)
    assert result.shape == (4, 4, 4, 5)


# ---------------------------------------------------------------------------
# Integration with BulbGamutMapper
# ---------------------------------------------------------------------------


def test_mapper_defaults_to_heuristic_model():
    mapper = BulbGamutMapper(BulbColorProfile())
    assert mapper.profile.mixing_model == "heuristic"


def test_physical_mixing_model_builds_and_maps():
    mapper = BulbGamutMapper(
        BulbColorProfile(white_channels=2, rgb_channel_current=(9, 8, 6), mixing_model="physical")
    )
    assert mapper._lookup.shape == (33, 33, 33, 5)
    assert mapper._gray_lookup.shape == (256, 5)
    for color in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (128, 128, 128), (30, 30, 30)]:
        payload = mapper.map_rgb(color)
        assert len(payload) == 5
        assert all(0 <= v <= 255 for v in payload)


def test_physical_mixing_model_rgbw_single_white():
    mapper = BulbGamutMapper(
        BulbColorProfile(white_channels=1, rgb_channel_current=(9, 8, 6), mixing_model="physical")
    )
    assert mapper._lookup.shape == (33, 33, 33, 4)
    payload = mapper.map_rgb((255, 128, 0))
    assert len(payload) == 4


def test_heuristic_mapper_output_unchanged_by_physical_model_existing():
    """The default path must be byte-for-byte unaffected by the new module."""

    mapper = BulbGamutMapper(BulbColorProfile(white_channels=2, rgb_channel_current=(9, 8, 6)))
    assert mapper.map_rgb((255, 0, 0)) == (235, 0, 0, 0, 0)
    assert mapper.map_rgb((0, 255, 255)) == (0, 241, 255, 0, 0)
