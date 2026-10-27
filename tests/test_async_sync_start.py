"""Desktop startup must not block the UI or revive a cancelled session."""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from src.gui.modern_app import WizLightModernGUI
from src.core.async_runtime import BackgroundAsyncLoop


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
        config=SimpleNamespace(bulbs=[], screen_sync=SimpleNamespace()),
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
