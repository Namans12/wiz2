"""Guard the shared generic colour table against desktop/Android drift."""

from pathlib import Path
import struct

import numpy as np

from src.core.color_mapping import BulbColorProfile, BulbGamutMapper


def test_android_colour_asset_matches_generic_desktop_mapper():
    asset = (Path(__file__).resolve().parents[1] /
             "wizlight-android/android/app/src/main/assets/wiz_rgbcw_lut_v1.bin")
    data = asset.read_bytes()
    assert data[:6] == b"WZLUT1"
    grid, channels = struct.unpack_from("<ii", data, 6)
    assert (grid, channels) == (33, 5)
    lut_end = 14 + grid**3 * channels
    assert struct.unpack_from("<i", data, lut_end)[0] == 256
    assert len(data) == lut_end + 4 + 256 * channels
    profile = BulbColorProfile(
        white_channels=2, white_to_color_ratio=20, rgb_channel_current=(9, 8, 6),
        source_color_space="srgb", device_profile="generic", cool_mix_bias=0.0,
    )
    mapper = BulbGamutMapper(profile)
    assert data[14:lut_end] == np.ascontiguousarray(mapper._lookup).tobytes()
    assert data[lut_end + 4:] == np.ascontiguousarray(mapper._gray_lookup).tobytes()


# ── Physical-model tables (per white-LED count) ────────────────────────────────

import random  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "wizlight-android/android/app/src/main/assets"
sys.path.insert(0, str(ROOT / "scripts"))
from export_lut_for_android import TABLES, SAMPLES  # noqa: E402


def _parse(data: bytes):
    assert data[:6] == b"WZLUT1"
    grid, channels = struct.unpack_from("<ii", data, 6)
    lut_end = 14 + grid**3 * channels
    lut = np.frombuffer(data, np.uint8, grid**3 * channels, 14).reshape(grid, grid, grid, channels)
    gray_n = struct.unpack_from("<i", data, lut_end)[0]
    gray = np.frombuffer(data, np.uint8, gray_n * channels, lut_end + 4).reshape(gray_n, channels)
    assert len(data) == lut_end + 4 + gray_n * channels
    return lut, gray


def _kotlin_style_map(lut, gray, rgb):
    """Line-for-line mirror of WizLut.map (Kotlin) on the serialised table."""
    r, g, b = rgb
    gmax = lut.shape[0] - 1
    fr, fg, fb = r / 255.0 * gmax, g / 255.0 * gmax, b / 255.0 * gmax
    r0, g0, b0 = int(fr), int(fg), int(fb)
    r1, g1, b1 = min(r0 + 1, gmax), min(g0 + 1, gmax), min(b0 + 1, gmax)
    tr, tg, tb = fr - r0, fg - g0, fb - b0
    def f(i, j, k):
        return lut[i, j, k].astype(np.float64)

    c00 = f(r0, g0, b0) + (f(r0, g0, b1) - f(r0, g0, b0)) * tb
    c01 = f(r0, g1, b0) + (f(r0, g1, b1) - f(r0, g1, b0)) * tb
    c10 = f(r1, g0, b0) + (f(r1, g0, b1) - f(r1, g0, b0)) * tb
    c11 = f(r1, g1, b0) + (f(r1, g1, b1) - f(r1, g1, b0)) * tb
    c0 = c00 + (c01 - c00) * tg
    c1 = c10 + (c11 - c10) * tg
    out = c0 + (c1 - c0) * tr
    mx = max(r, g, b)
    if mx > 0:
        sat = (mx - min(r, g, b)) / mx
        if sat < 0.12:
            t = min(1.0, (0.12 - sat) / 0.08)
            blend = t * t * (3.0 - 2.0 * t)
            luma = int(min(255, max(0, round(r * 0.2126 + g * 0.7152 + b * 0.0722))))
            out = out + (gray[luma].astype(np.float64) - out) * blend
    return tuple(int(v) for v in np.clip(np.round(out), 0, 255))


@pytest.mark.parametrize("name", sorted(TABLES))
def test_shipped_table_is_the_current_export(name):
    mapper = BulbGamutMapper(TABLES[name])
    data = (ASSETS / name).read_bytes()
    lut, gray = _parse(data)
    assert np.array_equal(lut, np.asarray(mapper._lookup))
    assert np.array_equal(gray, np.asarray(mapper._gray_lookup))


def test_physical_table_shapes_and_zero_channels():
    expected_channels = {
        "wiz_rgbcw_physical_lut_v1.bin": 5,
        "wiz_rgbw_physical_lut_v1.bin": 4,
        "wiz_rgb_physical_lut_v1.bin": 4,
    }
    for name, channels in expected_channels.items():
        lut, gray = _parse((ASSETS / name).read_bytes())
        assert lut.shape == (33, 33, 33, channels)
        assert gray.shape == (256, channels)
    rgb_only, _ = _parse((ASSETS / "wiz_rgb_physical_lut_v1.bin").read_bytes())
    assert not rgb_only[..., 3].any()  # no white LED -> 4th column zero


@pytest.mark.parametrize("name", sorted(TABLES))
def test_kotlin_format_interpolation_matches_python_mapper(name):
    mapper = BulbGamutMapper(TABLES[name])
    lut, gray = _parse((ASSETS / name).read_bytes())
    rng = random.Random(1234)
    points = list(SAMPLES) + [tuple(rng.randrange(256) for _ in range(3)) for _ in range(400)]
    # Near-neutral points exercise the gray-ramp blend.
    for _ in range(100):
        base = rng.randrange(256)
        points.append(tuple(max(0, min(255, base + rng.randrange(-6, 7))) for _ in range(3)))
    errors = [
        np.abs(np.array(_kotlin_style_map(lut, gray, p)) - np.array(mapper.map_rgb(p)))
        for p in points
    ]
    errors = np.array(errors)
    print(f"{name}: max channel error {errors.max()}, mean {errors.mean():.4f}")
    assert errors.max() <= 1  # rint vs numpy round only differ on exact .5 ties
    assert errors.mean() < 0.01


def test_expected_values_file_for_kotlin_matches_python_mapper():
    lines = (ROOT / "wizlight-android/android/app/src/test/resources/lut_expected_v1.txt").read_text().split("\n")
    lines = [line for line in lines if line.strip()]
    assert len(lines) == len(TABLES) * len(SAMPLES)
    for line in lines:
        head, tail = line.split(" -> ")
        name, r, g, b = head.split()
        assert tuple(int(v) for v in tail.split()) == BulbGamutMapper(TABLES[name]).map_rgb((int(r), int(g), int(b)))


def test_single_white_profile_matches_desktop_hint_parsing():
    """The representative RGBW profile is what desktop builds for nowc=1 / 24 mA white."""
    from src.core.wiz_profile_metadata import parse_wiz_profile_hints

    hints = parse_wiz_profile_hints(
        {"nowc": 1, "wcr": 20, "i2cDrv": [{"curr": [9, 8, 6, 24]}]}, {}, BulbColorProfile()
    )
    assert hints.white_channels == 1 and hints.white_channel_current == (24,)
    assert TABLES["wiz_rgbw_physical_lut_v1.bin"].white_channel_current == hints.white_channel_current
