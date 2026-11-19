"""Bake the desktop 33^3 RGBCW LUT (+ 256-entry gray ramp) to a binary asset
that the Android ColorSyncEngine loads and interpolates identically — so a
given sRGB color produces the SAME bulb payload on TV and desktop.

Binary layout (little-endian):
    magic   : 6 bytes  b"WZLUT1"
    grid    : int32     (33)
    channels: int32     (5 = RGBCW)
    lut     : grid^3 * channels  uint8   (index ((r*grid+g)*grid+b)*channels+ch)
    grayN   : int32     (256)
    gray    : grayN * channels    uint8   (index luma*channels+ch)
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

# Ensure the LOCAL repo src/ wins over any stale pip-installed copy in
# site-packages (running as a script puts scripts/ on the path, not the root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.color_mapping import BulbGamutMapper, BulbColorProfile

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

OUT = Path("wizlight-android/android/app/src/main/assets/wiz_rgbcw_lut_v1.bin")


def main() -> None:
    mapper = BulbGamutMapper(PROFILE)
    lut = np.asarray(mapper._lookup, dtype=np.uint8)          # (33,33,33,5)
    gray = np.asarray(mapper._gray_lookup, dtype=np.uint8)     # (256,5)

    grid = lut.shape[0]
    channels = lut.shape[-1]
    assert channels == 5, f"expected RGBCW (5), got {channels}"
    assert gray.shape == (256, channels), gray.shape

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(b"WZLUT1")
        f.write(struct.pack("<i", grid))
        f.write(struct.pack("<i", channels))
        f.write(np.ascontiguousarray(lut).tobytes())          # C-order: ((r*grid+g)*grid+b)*ch
        f.write(struct.pack("<i", gray.shape[0]))
        f.write(np.ascontiguousarray(gray).tobytes())

    size = OUT.stat().st_size
    print(f"wrote {OUT}  ({size} bytes)  grid={grid} channels={channels}")
    # sanity spot-checks against map_rgb
    for rgb in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (200, 150, 40)]:
        print(f"  {rgb} -> {tuple(mapper.map_rgb(rgb))}")


if __name__ == "__main__":
    main()
