"""Desktop startup must not block the UI or revive a cancelled session."""

from concurrent.futures import Future
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.gui.modern_app import WizLightModernGUI
from src.gui.components.dashboard import BulbDashboard
from src.core.async_runtime import BackgroundAsyncLoop
from src.core.config import BulbConfig, Config


def _app():
    future = Future()
    runner = Mock()

    def submit(coro):
        coro.close()
        return future

    runner.submit.side_effect = submit
    root = Mock()
    root.after.return_value = "poll"
    app = SimpleNamespace(
        _closing=False, screen_sync=None, _async_runner=runner, root=root,
        config=SimpleNamespace(screen_sync_bulbs=lambda: [BulbConfig("lamp", "Lamp")],
                               screen_sync=SimpleNamespace(), save=Mock()),
        screen_sync_switch=SimpleNamespace(get=lambda: True, deselect=Mock()),
        _get_bulb_ips=lambda: [], _save_screen_sync_settings=Mock(),
        _set_status=Mock(), _set_sync_running_ui=Mock(), _finish_sync_start=Mock(),
    )
    app._poll_sync_start = lambda *args: WizLightModernGUI._poll_sync_start(app, *args)
    return app, future


def test_start_schedules_preflight_without_waiting():
    app, future = _app()
    WizLightModernGUI._start_screen_sync(app)
    assert app._sync_start_future is future
    app._async_runner.run.assert_not_called()
    app.root.after.assert_called_once()
    assert not future.done()


def test_no_selected_bulbs_skips_preflight_and_resets_switch():
    app, _ = _app()
    app.config.screen_sync_bulbs = lambda: []
    WizLightModernGUI._start_screen_sync(app)
    app._async_runner.submit.assert_not_called()
    app.screen_sync_switch.deselect.assert_called_once()
    assert app.config.screen_sync.enabled is False


def test_individual_power_off_excludes_bulb_and_replaces_live_targets(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("one", "Desk")
    config.add_bulb("two", "Wall")
    app, _ = _app()
    app.config = config
    app._dashboard = Mock()
    app._stop_screen_sync = Mock()
    app._start_screen_sync = Mock()
    app._set_bulb_screen_sync = lambda *args: WizLightModernGUI._set_bulb_screen_sync(app, *args)

    WizLightModernGUI._exclude_bulb_before_power_off(app, "one")

    assert [bulb.ip for bulb in Config.load(config._config_path).screen_sync_bulbs()] == ["two"]
    app._dashboard.set_screen_sync_enabled.assert_called_once_with("one", False)
    app._stop_screen_sync.assert_called_once_with(update_status=False)
    app._start_screen_sync.assert_called_once()


@pytest.mark.asyncio
async def test_individual_power_off_waits_for_old_sync_transport():
    stopping = Future()
    app, _ = _app()
    app._set_bulb_screen_sync = Mock()
    app._sync_stop_future = stopping
    tasks = []
    dashboard = SimpleNamespace(
        before_power_off=lambda ip: WizLightModernGUI._exclude_bulb_before_power_off(app, ip),
        controller=SimpleNamespace(turn_off=AsyncMock()), _bulbs=[],
        _run_async=lambda coroutine: tasks.append(asyncio.create_task(coroutine)),
    )

    BulbDashboard._toggle_bulb(dashboard, "lamp", False)
    app._set_bulb_screen_sync.assert_called_once_with("lamp", False)
    try:
        await asyncio.sleep(0)
        dashboard.controller.turn_off.assert_not_awaited()
    finally:
        stopping.set_result(None)
        await tasks[0]
    dashboard.controller.turn_off.assert_awaited_once_with("lamp")


def test_completed_preflight_starts_once_and_stale_callback_is_ignored():
    app, future = _app()
    WizLightModernGUI._start_screen_sync(app)
    future.set_result((["lamp"], None))
    settings = app.config.screen_sync
    WizLightModernGUI._poll_sync_start(app, future, ["lamp"], settings)
    app._finish_sync_start.assert_called_once_with(["lamp"], ["lamp"], settings, None)
    WizLightModernGUI._poll_sync_start(app, future, ["lamp"], settings)
    app._finish_sync_start.assert_called_once()


def test_cancelled_preflight_does_not_start():
    app, future = _app()
    WizLightModernGUI._start_screen_sync(app)
    app._sync_start_future = None
    future.cancel()
    WizLightModernGUI._poll_sync_start(app, future, [], app.config.screen_sync)
    app._finish_sync_start.assert_not_called()


def test_preflight_runs_target_and_mapping_steps_off_ui_thread(monkeypatch):
    from src.gui import modern_app

    app, _ = _app()
    runner = BackgroundAsyncLoop()
    app._async_runner = runner
    app.controller = object()
    calls = []

    async def targets(controller, bulbs):
        calls.append("targets")
        return ["lamp"]

    async def mapping(controller, ips, settings):
        calls.append("mapping")

    monkeypatch.setattr(modern_app, "refresh_verified_targets", targets)
    monkeypatch.setattr(modern_app, "prepare_screen_sync_mapping", mapping)
    try:
        WizLightModernGUI._start_screen_sync(app)
        future = app._sync_start_future
        assert future.result(timeout=2) == (["lamp"], None)
        WizLightModernGUI._poll_sync_start(app, future, [], app.config.screen_sync)
        assert calls == ["targets", "mapping"]
        app._finish_sync_start.assert_called_once()
    finally:
        runner.shutdown()
