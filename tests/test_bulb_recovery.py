"""Address recovery uses verified identities and never touches hardware."""

from types import SimpleNamespace

import pytest

from src.core.config import BulbConfig, Config
from src.features.bulb_recovery import RecoveredBulb, recover_saved_bulbs
from src.gui.modern_app import WizLightModernGUI


class FakeController:
    def __init__(self, discovered, states):
        self.discovered = discovered
        self.states = states
        self.lookups = []

    async def discover(self, broadcast_space):
        assert broadcast_space == "255.255.255.255"
        return self.discovered

    async def _resolve_screen_sync_target(self, ip):
        self.lookups.append(ip)
        return self.states.get(ip)


@pytest.mark.asyncio
async def test_recovery_verifies_identity_at_new_address():
    bulb = BulbConfig("old", "Lamp", "AA:BB:CC:DD:EE:FF")
    controller = FakeController([{"ip": "new", "mac": "aa-bb-cc-dd-ee-ff"}],
                                {"new": SimpleNamespace(mac="aabbccddeeff")})
    assert await recover_saved_bulbs(controller, [bulb]) == [
        RecoveredBulb("old", "new", "Lamp", "AA:BB:CC:DD:EE:FF")
    ]


@pytest.mark.asyncio
async def test_ambiguous_occupied_or_mismatched_addresses_are_never_used():
    bulb = BulbConfig("old", "Lamp", "AA:BB:CC:DD:EE:FF")
    occupied = BulbConfig("other", "Other", "11:22:33:44:55:66")
    controller = FakeController([
        {"ip": "other", "mac": bulb.mac},
    ], {"other": SimpleNamespace(mac=bulb.mac)})
    assert await recover_saved_bulbs(controller, [bulb, occupied]) == []
    assert controller.lookups == []
    controller.discovered = [{"ip": "new", "mac": bulb.mac}]
    controller.states = {"new": SimpleNamespace(mac=occupied.mac)}
    assert await recover_saved_bulbs(controller, [bulb]) == []
    controller.discovered.append({"ip": "another", "mac": bulb.mac})
    assert await recover_saved_bulbs(controller, [bulb]) == []


def test_recovery_updates_layout_and_ignores_cancelled_callback(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("old", "Lamp", "aa:bb:cc:dd:ee:ff")
    config.screen_sync.bulb_layout["old"] = "left"
    token = object()
    starts = []
    app = SimpleNamespace(_closing=False, _bulb_recovery_token=token, config=config,
                          screen_sync_switch=SimpleNamespace(get=lambda: True),
                          _update_bulb_status=lambda: None,
                          _start_screen_sync=lambda: starts.append(True))
    found = [RecoveredBulb("old", "new", "Lamp", "aa:bb:cc:dd:ee:ff")]
    WizLightModernGUI._finish_bulb_recovery(app, object(), found, None)
    assert config.bulbs[0].ip == "old"
    WizLightModernGUI._finish_bulb_recovery(app, token, found, None)
    assert config.bulbs[0].ip == "new"
    assert config.screen_sync.bulb_layout == {"new": "left"}
    assert starts == [True]
