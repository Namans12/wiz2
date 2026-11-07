"""Address recovery uses verified identities and never touches hardware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from src.core.config import BulbConfig, Config
from src.features.bulb_recovery import RecoveredBulb, recover_saved_bulbs, refresh_verified_targets
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

    def _get_bulb(self, ip):
        async def get_config():
            self.lookups.append(ip)
            state = self.states.get(ip)
            return {"result": {"mac": state.mac}} if state is not None else {}
        return SimpleNamespace(getBulbConfig=get_config)

    async def refresh_screen_sync_targets(self, ips):
        return list(ips)


@pytest.mark.asyncio
async def test_startup_rejects_reused_address_but_keeps_unknown_identity():
    saved = [BulbConfig("old", "Lamp", "aa:bb:cc:dd:ee:ff"),
             BulbConfig("manual", "Manual", None)]
    controller = FakeController([], {"old": SimpleNamespace(mac="11:22:33:44:55:66")})
    assert await refresh_verified_targets(controller, saved) == ["manual"]
    controller.states["old"] = SimpleNamespace(mac="aa-bb-cc-dd-ee-ff")
    assert await refresh_verified_targets(controller, saved) == ["old", "manual"]


@pytest.mark.asyncio
async def test_startup_ignores_stale_cached_mac():
    saved = [BulbConfig("old", "Lamp", "aa:bb:cc:dd:ee:ff")]

    class CachedBulb:
        mac = "aa:bb:cc:dd:ee:ff"

        async def getBulbConfig(self):
            return {"result": {"mac": "11:22:33:44:55:66"}}

    controller = FakeController([], {})
    controller._get_bulb = lambda ip: CachedBulb()
    assert await refresh_verified_targets(controller, saved) == []


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
    controller.discovered = [{"ip": "new", "mac": bulb.mac}]
    controller.states = {"new": SimpleNamespace(mac=occupied.mac)}
    assert await recover_saved_bulbs(controller, [bulb]) == []
    controller.discovered.append({"ip": "another", "mac": bulb.mac})
    assert await recover_saved_bulbs(controller, [bulb]) == []


@pytest.mark.asyncio
async def test_recovery_targets_only_missing_lights():
    missing = BulbConfig("old", "Missing", "aa:bb:cc:dd:ee:ff")
    active = BulbConfig("active", "Active", "11:22:33:44:55:66")
    controller = FakeController([
        {"ip": "new", "mac": missing.mac},
        {"ip": "other-new", "mac": active.mac},
    ], {"new": SimpleNamespace(mac=missing.mac),
        "other-new": SimpleNamespace(mac=active.mac)})
    result = await recover_saved_bulbs(controller, [missing, active], target_ips={"old"})
    assert [item.new_ip for item in result] == ["new"]
    assert controller.lookups == ["new"]


@pytest.mark.asyncio
async def test_recovery_accepts_only_fully_verified_address_swap():
    left = BulbConfig("one", "Left", "aa:bb:cc:dd:ee:ff")
    right = BulbConfig("two", "Right", "11:22:33:44:55:66")
    controller = FakeController([
        {"ip": "two", "mac": left.mac}, {"ip": "one", "mac": right.mac},
    ], {"two": SimpleNamespace(mac=left.mac), "one": SimpleNamespace(mac=right.mac)})
    result = await recover_saved_bulbs(controller, [left, right])
    assert {(item.old_ip, item.new_ip) for item in result} == {("one", "two"), ("two", "one")}
    controller.states["one"] = SimpleNamespace(mac="00:00:00:00:00:01")
    assert await recover_saved_bulbs(controller, [left, right]) == []


@pytest.mark.asyncio
async def test_recovery_retries_incomplete_discovery_and_identity_timeout():
    bulb = BulbConfig("old", "Lamp", "aa:bb:cc:dd:ee:ff")
    controller = FakeController([], {})
    calls = []

    async def discover(_broadcast):
        calls.append("discover")
        return [] if calls.count("discover") == 1 else [{"ip": "new", "mac": bulb.mac}]

    async def read_config():
        calls.append("identity")
        if calls.count("identity") == 1:
            raise asyncio.TimeoutError()
        return {"result": {"mac": bulb.mac}}

    controller.discover = discover
    controller._get_bulb = lambda ip: SimpleNamespace(getBulbConfig=read_config)
    result = await recover_saved_bulbs(controller, [bulb])
    assert [(item.old_ip, item.new_ip) for item in result] == [("old", "new")]
    assert calls.count("discover") == calls.count("identity") == 2


def test_verified_swap_preserves_names_and_zone_assignments(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("one", "Left", "aa:bb:cc:dd:ee:ff")
    config.add_bulb("two", "Right", "11:22:33:44:55:66")
    config.screen_sync.bulb_layout = {"one": "left", "two": "right"}
    moves = [("one", "two", "aa:bb:cc:dd:ee:ff"), ("two", "one", "11:22:33:44:55:66")]
    assert config.reassign_bulb_addresses(moves[:1]) == 0
    assert config.reassign_bulb_addresses(moves) == 2
    reloaded = Config.load(tmp_path / "config.json")
    assert [(bulb.ip, bulb.name) for bulb in reloaded.bulbs] == [("two", "Left"), ("one", "Right")]
    assert reloaded.screen_sync.bulb_layout == {"two": "left", "one": "right"}


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


def test_returning_active_light_gets_fresh_screen_update():
    engine = SimpleNamespace(is_running=True, _stats_lock=threading.Lock(),
                             _last_sent_colors={"all": (1, 2, 3)})
    app = SimpleNamespace(_closing=False, screen_sync=engine,
                          _sync_missing_ips={"lamp"}, _sync_active_ips={"lamp"},
                          _schedule_bulb_recovery_monitor=Mock())
    WizLightModernGUI._finish_sync_bulb_poll(app, engine, set())
    assert engine._last_sent_colors == {}
    app._schedule_bulb_recovery_monitor.assert_called_once()


def test_returning_previously_skipped_light_restarts_sync():
    engine = SimpleNamespace(is_running=True)
    app = SimpleNamespace(_closing=False, screen_sync=engine,
                          _sync_missing_ips={"lamp"}, _sync_active_ips=set(),
                          _stop_screen_sync=Mock(), _start_screen_sync=Mock())
    WizLightModernGUI._finish_sync_bulb_poll(app, engine, set())
    app._stop_screen_sync.assert_called_once_with(update_status=False)
    app._start_screen_sync.assert_called_once()


def test_background_recovery_rejoins_light_and_stale_session_cannot_restart(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("old", "Lamp", "aa:bb:cc:dd:ee:ff")
    engine = SimpleNamespace(is_running=True)
    token = object()
    app = SimpleNamespace(_closing=False, screen_sync=engine, _bulb_recovery_token=token,
                          config=config, screen_sync_switch=SimpleNamespace(get=lambda: True),
                          _update_bulb_status=Mock(), _stop_screen_sync=Mock(),
                          _start_screen_sync=Mock())
    found = [RecoveredBulb("old", "new", "Lamp", "aa:bb:cc:dd:ee:ff")]
    WizLightModernGUI._finish_bulb_recovery(app, token, found, None, object())
    assert config.bulbs[0].ip == "old"
    app._start_screen_sync.assert_not_called()
    app._bulb_recovery_token = token
    WizLightModernGUI._finish_bulb_recovery(app, token, found, None, engine)
    assert config.bulbs[0].ip == "new"
    app._stop_screen_sync.assert_called_once_with(update_status=False)
    app._start_screen_sync.assert_called_once()
