"""RGB-only devices must not lose neutral colours to absent white LEDs."""

from src.core.color_mapping import BulbColorProfile, BulbGamutMapper


def test_rgb_only_neutral_ramp_uses_no_white_channel():
    mapper = BulbGamutMapper(BulbColorProfile(white_channels=0))
    black = mapper.map_rgb((0, 0, 0))
    gray = mapper.map_rgb((128, 128, 128))
    white = mapper.map_rgb((255, 255, 255))
    assert black == (0, 0, 0, 0)
    assert gray[3] == white[3] == 0
    assert all(0 < gray[i] < white[i] for i in range(3))
