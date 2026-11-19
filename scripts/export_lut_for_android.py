"""Bake bulb colour tables for the Android ColorSyncEngine.

Binary layout (little-endian), shared by every table:
    magic   : 6 bytes  b"WZLUT1"
    grid    : int32     (33)
    channels: int32     (4 = RGBW / RGB-only, 5 = RGBCW)
    lut     : grid^3 * channels  uint8   (index ((r*grid+g)*grid+b)*channels+ch)
    grayN   : int32     (256)
    gray    : grayN * channels    uint8   (index luma*channels+ch)

Tables written to android/app/src/main/assets:

  wiz_rgbcw_lut_v1.bin           heuristic mapper, generic dual-white profile
                                 (legacy table; fallback when a bulb's
                                 getModelConfig probe fails)
  wiz_rgbcw_physical_lut_v1.bin  physical mixing model, dual white (nowc=2), 5 ch
  wiz_rgbw_physical_lut_v1.bin   physical mixing model, single white (nowc=1),
                                 4 ch = r,g,b,w   (Android sends c:0, as desktop)
  wiz_rgb_physical_lut_v1.bin    physical mixing model, no white LED (nowc=0),
                                 4 ch = r,g,b,0

The physical tables use the same BulbColorProfile settings the desktop builds
for mixing_model "physical" (sRGB source, generic device profile, no cool
bias, no per-bulb calibration/tone table).  Per white count the profile is a
REPRESENTATIVE one, since the bulb's channel currents are not used by Android:

  * rgb_channel_current = (9, 8, 6)   desktop BulbColorProfile default
  * white_to_color_ratio = 20         desktop default / WiZ firmware value
  * nowc=2: white_channel_current unknown -> physical model default
            (2.7 x strongest colour current), cool 6500 K + warm 2700 K
  * nowc=1: white_channel_current = (24,) -- the measured WiZ ESP25 RGBW
            module (docs: "reports 24 mA against 9 mA"); white LED modelled as
            a 2700 K blackbody (desktop's single-white assumption)
  * nowc=0: RGB only, 4th column zero

Also writes android/app/src/test/resources/lut_expected_v1.txt: sample inputs
with the Python mapper's map_rgb output, used by the Kotlin tests. One line per
sample: "<table file> r g b -> o0 o1 o2 o3 [o4]".
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

# Ensure the LOCAL repo src/ wins over any stale pip-installed copy in
# site-packages (running as a script puts scripts/ on the path, not the root).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.color_mapping import BulbGamutMapper, BulbColorProfile  # noqa: E402

# Generic WiZ 9W E27 RGBCW, sRGB input, no per-device cool bias (applied on the
# Android side post-hoc so a single LUT serves every device profile).
PROFILE = BulbColorProfile(
    white_channels=2,
    white_to_color_ratio=20,
    rgb_channel_current=(9, 8, 6),
    source_color_space="srgb",
    device_profile="generic",
    cool_mix_bias=0.0,
)

ASSETS = ROOT / "wizlight-android/android/app/src/main/assets"
RESOURCES = ROOT / "wizlight-android/android/app/src/test/resources"
OUT = ASSETS / "wiz_rgbcw_lut_v1.bin"


def physical_profile(white_channels: int) -> BulbColorProfile:
    """Representative physical-model profile for a white-LED count (see docstring)."""

    return BulbColorProfile(
        white_channels=white_channels,
        white_to_color_ratio=20,
        rgb_channel_current=(9, 8, 6),
        white_channel_current=(24,) if white_channels == 1 else (),
        source_color_space="srgb",
        device_profile="generic",
        cool_mix_bias=0.0,
        mixing_model="physical",
    )


TABLES = {
    "wiz_rgbcw_lut_v1.bin": PROFILE,
    "wiz_rgbcw_physical_lut_v1.bin": physical_profile(2),
    "wiz_rgbw_physical_lut_v1.bin": physical_profile(1),
    "wiz_rgb_physical_lut_v1.bin": physical_profile(0),
}

SAMPLES = [
    (0, 0, 0), (255, 255, 255), (128, 128, 128), (40, 40, 40), (200, 190, 180),
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255),
    (255, 0, 255), (255, 128, 0), (255, 140, 5), (200, 150, 40), (90, 60, 30),
    (30, 80, 200), (10, 200, 120), (180, 20, 90), (255, 200, 160), (13, 77, 251),
]


def serialise(mapper: BulbGamutMapper) -> bytes:
    lut = np.asarray(mapper._lookup, dtype=np.uint8)
    gray = np.asarray(mapper._gray_lookup, dtype=np.uint8)
    grid, channels = lut.shape[0], lut.shape[-1]
    assert channels in (4, 5), channels
    assert gray.shape == (256, channels), gray.shape
    return b"".join((
        b"WZLUT1",
        struct.pack("<i", grid),
        struct.pack("<i", channels),
        np.ascontiguousarray(lut).tobytes(),
        struct.pack("<i", gray.shape[0]),
        np.ascontiguousarray(gray).tobytes(),
    ))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, profile in TABLES.items():
        mapper = BulbGamutMapper(profile)
        data = serialise(mapper)
        (ASSETS / name).write_bytes(data)
        for rgb in SAMPLES:
            out = " ".join(str(int(v)) for v in mapper.map_rgb(rgb))
            lines.append(f"{name} {rgb[0]} {rgb[1]} {rgb[2]} -> {out}")
        print(f"wrote {name}  ({len(data)} bytes)  channels={mapper._lookup.shape[-1]}")
        for rgb in [(255, 0, 0), (255, 255, 255), (200, 150, 40)]:
            print(f"  {rgb} -> {tuple(mapper.map_rgb(rgb))}")
    (RESOURCES / "lut_expected_v1.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
