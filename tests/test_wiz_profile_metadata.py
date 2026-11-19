"""Device metadata can be incomplete or malformed without preventing sync."""

from src.core.color_mapping import BulbColorProfile
from src.core.wiz_profile_metadata import parse_wiz_profile_hints
from src.core.bulb_controller import BulbController
import pytest


def test_complete_metadata_including_rgb_only():
    result = parse_wiz_profile_hints(
        {"nowc": 0, "wcr": 20, "i2cDrv": [{"curr": [9, 8, 6]}], "renderFactor": [1, 2]},
        {"fadeIn": 100, "fadeOut": 200}, BulbColorProfile(),
    )
    assert result.white_channels == 0
    assert result.rgb_channel_current == (9, 8, 6)
    assert result.render_factor == (1, 2)
    assert (result.fade_in_ms, result.fade_out_ms) == (100, 200)


def test_invalid_metadata_uses_defaults_without_discarding_valid_fields():
    defaults = BulbColorProfile()
    result = parse_wiz_profile_hints(
        {"nowc": None, "wcr": 1000, "i2cDrv": [None], "renderFactor": [None]},
        {"fadeIn": -1, "fadeOut": 42}, defaults,
    )
    assert result.white_channels == defaults.white_channels
    assert result.white_to_color_ratio == defaults.white_to_color_ratio
    assert result.rgb_channel_current == defaults.rgb_channel_current
    assert result.render_factor == ()
    assert (result.fade_in_ms, result.fade_out_ms) == (0, 42)


@pytest.mark.asyncio
async def test_malformed_device_reply_does_not_abort_profile_setup(monkeypatch):
    class FakeBulb:
        mac = None

        async def getBulbConfig(self):
            return {"result": {}}

        async def getModelConfig(self):
            return {"result": {"nowc": None, "i2cDrv": [None]}}

        async def getUserConfig(self):
            return {"result": {"fadeIn": None}}

    controller = BulbController()
    monkeypatch.setattr(controller, "_get_bulb", lambda ip: FakeBulb())
    monkeypatch.setattr(controller, "_load_calibration", lambda *args: None)
    monkeypatch.setattr(controller, "_load_tone_lut", lambda *args: None)
    profile = await controller.get_color_profile("test")
    assert profile.white_channels == BulbColorProfile().white_channels
    assert "test" in controller._gamut_mappers
