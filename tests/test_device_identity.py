"""Discovery may report one device's MAC in several equivalent formats."""

import pytest

from src.core.bulb_controller import BulbController, BulbState
from src.core.config import Config
from src.core.device_identity import mac_identity


def test_equivalent_mac_formats_have_same_identity():
    assert mac_identity("AA:BB:CC:DD:EE:FF") == mac_identity("aa-bb-cc-dd-ee-ff")
    assert mac_identity("aabb.ccdd.eeff") == mac_identity("aabbccddeeff")
    assert mac_identity(None) is None


@pytest.mark.asyncio
async def test_target_resolution_deduplicates_mac_formats(monkeypatch):
    controller = BulbController()
    macs = {"first": "AA:BB:CC:DD:EE:FF", "second": "aa-bb-cc-dd-ee-ff"}

    async def resolve(ip):
        return BulbState(ip, macs[ip], True, 100, (1, 2, 3), None)

    monkeypatch.setattr(controller, "_resolve_screen_sync_target", resolve)
    assert await controller.resolve_screen_sync_targets(["first", "second"]) == ["first"]


def test_rediscovery_moves_existing_light_layout_to_new_ip(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("old", "Lamp", "AA:BB:CC:DD:EE:FF")
    config.screen_sync.bulb_layout["old"] = "left"
    config.add_bulb("new", "Lamp", "aa-bb-cc-dd-ee-ff")
    assert len(config.bulbs) == 1
    assert config.bulbs[0].name == "Lamp"
    assert config.bulbs[0].ip == "new"
    assert config.screen_sync.bulb_layout == {"new": "left"}
