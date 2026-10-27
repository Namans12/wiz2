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
