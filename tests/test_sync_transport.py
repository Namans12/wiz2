"""Brightness and lifecycle contracts at the desktop transport boundary."""

from concurrent.futures import Future
import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.async_runtime import BackgroundAsyncLoop
from src.features.sync_transport import ScreenSyncTransport, prepare_screen_sync_mapping


def test_fast_and_fallback_preserve_color_and_brightness():
    runner = BackgroundAsyncLoop()
    calls = []

    class Controller:
        mapped = True

        def map_screen_sync_payloads_cached(self, colors):
            return {ip: (*color, 0, 0) for ip, color in colors.items()} if self.mapped else {}

        async def set_screen_sync_map(self, colors, brightness=None):
            calls.append((colors, brightness))

    controller = Controller()
    sender = Mock()
    transport = ScreenSyncTransport(controller, runner.submit, sender)
    colors = {"lamp": (255, 16, 8)}
    try:
        transport.send(colors, 83)
        sender.send_payload_map.assert_called_once_with({"lamp": (255, 16, 8, 0, 0)}, 83)
        controller.mapped = False
        transport.send(colors, 42)
        colors["lamp"] = (0, 0, 0)
        runner.run(asyncio.sleep(0.01))
        assert calls == [({"lamp": (255, 16, 8)}, 42)]
        transport.close()
        transport.close()
        sender.close.assert_called_once()
        transport.send({"lamp": (1, 2, 3)}, 10)
        assert len(calls) == 1
    finally:
        transport.close()
        runner.shutdown()


def test_transport_closes_sender_even_if_dispatcher_close_fails():
    sender = Mock()
    transport = ScreenSyncTransport(Mock(), Mock(return_value=Future()), sender)
    transport.dispatcher.close = Mock(side_effect=RuntimeError("cancel failed"))
    with pytest.raises(RuntimeError, match="cancel failed"):
        transport.close()
    sender.close.assert_called_once()


@pytest.mark.asyncio
async def test_mapping_preparation_forwards_selected_profile_and_model(monkeypatch):
    from src.core.config import ScreenSyncConfig
    from src.features import sync_transport

    monkeypatch.setattr(sync_transport, "resolve_auto_display_profile", lambda settings: {
        "source_color_space": "display-p3", "device_profile": "mac", "cool_mix_bias": 0.05,
    })
    calls = []

    class Controller:
        async def prepare_screen_sync_mappers(self, ips, **kwargs):
            calls.append((ips, kwargs))

    settings = ScreenSyncConfig(mixing_model="physical")
    await prepare_screen_sync_mapping(Controller(), ["lamp"], settings)
    assert calls == [(["lamp"], {
        "source_color_space": "display-p3", "device_profile": "mac",
        "cool_mix_bias": 0.05, "mixing_model": "physical",
    })]


def test_gui_stop_detaches_callbacks_and_closes_transport():
    from src.gui.modern_app import WizLightModernGUI

    engine = Mock()
    transport = Mock()
    app = SimpleNamespace(
        screen_sync=engine, _screen_sync_transport=transport,
        _screen_sync_debug_id=None, _closing=True,
    )
    WizLightModernGUI._stop_screen_sync(app, update_status=False)
    assert app.screen_sync is None
    assert app._screen_sync_transport is None
    engine.stop.assert_called_once()
    transport.close.assert_called_once()
    WizLightModernGUI._stop_screen_sync(app, update_status=False)
    transport.close.assert_called_once()


def test_gui_close_closes_transport_before_async_loop():
    from src.gui.modern_app import WizLightModernGUI

    order = []
    root = Mock()
    runner = Mock()

    def complete_close(coro, **kwargs):
        coro.close()
        order.append("controller")

    async def close_controller():
        pass

    runner.run.side_effect = complete_close
    runner.shutdown.side_effect = lambda: order.append("loop")
    app = SimpleNamespace(
        _closing=False, _tray=None, _dashboard=None, _minimized_to_tray=False,
        screen_sync=Mock(), _screen_sync_transport=Mock(), clap_detector=None,
        _brightness_debounce_id=None, _temp_debounce_id=None,
        _screen_sync_settings_save_id=None, _screen_sync_reconfigure_id=None,
        _screen_sync_debug_id=None, _pending_tasks=[], _async_runner=runner,
        root=root, controller=SimpleNamespace(close_async=close_controller),
    )
    app._screen_sync_transport.close.side_effect = lambda: order.append("transport")
    app._stop_screen_sync = lambda **kwargs: WizLightModernGUI._stop_screen_sync(app, **kwargs)
    WizLightModernGUI._on_close(app)
    WizLightModernGUI._on_close(app)
    assert order == ["transport", "controller", "loop"]
    root.destroy.assert_called_once()



def test_failed_engine_stop_still_closes_transport_and_cancels_debug_timer():
    from src.gui.modern_app import WizLightModernGUI
    import pytest

    app = SimpleNamespace(screen_sync=Mock(), _screen_sync_transport=Mock(),
                          _screen_sync_debug_id="debug-timer", _closing=True, root=Mock())
    transport = app._screen_sync_transport
    app.screen_sync.stop.side_effect = RuntimeError("worker still stopping")
    with pytest.raises(RuntimeError, match="worker still stopping"):
        WizLightModernGUI._stop_screen_sync(app)
    transport.close.assert_called_once()
    app.root.after_cancel.assert_called_once_with("debug-timer")
    assert app._screen_sync_debug_id is None


@pytest.mark.parametrize("cancel_restart", [False, True])
def test_gui_stop_returns_during_launch_and_serializes_restart(cancel_restart):
    from src.gui.modern_app import WizLightModernGUI

    launched = Future()
    engine, transport = Mock(), Mock()
    widget = SimpleNamespace(
        _closing=False, screen_sync=engine, _screen_sync_transport=transport,
        _sync_launch_future=launched, _sync_launch_poll_id="launch", _screen_sync_debug_id=None,
        _set_status=Mock(), _set_sync_running_ui=Mock(), _refresh_screen_sync_debug=Mock(),
        _start_screen_sync=Mock(), _poll_sync_stop=Mock(), root=Mock(),
    )
    stopping_started = Event()
    engine.stop.side_effect = stopping_started.set
    WizLightModernGUI._stop_screen_sync(widget)
    stopping = widget._sync_stop_future
    try:
        widget.root.after_cancel.assert_called_once_with("launch")
        assert widget.screen_sync is None
        assert widget._screen_sync_transport is None
        assert not stopping.done()
        engine.stop.assert_not_called()
        WizLightModernGUI._start_screen_sync(widget)
        assert widget._sync_restart_pending
        if cancel_restart:
            WizLightModernGUI._stop_screen_sync(widget)
        WizLightModernGUI._poll_sync_stop(widget, stopping)
        widget._start_screen_sync.assert_not_called()
    finally:
        launched.set_result(None)
        stopping.result(timeout=2)
    assert stopping_started.is_set()
    transport.close.assert_called_once()
    WizLightModernGUI._poll_sync_stop(widget, stopping)
    assert widget._start_screen_sync.call_count == (0 if cancel_restart else 1)
