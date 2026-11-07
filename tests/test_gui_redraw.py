"""Live diagnostics should not redraw unchanged widgets every polling tick."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.gui.modern_app import WizLightModernGUI
from src.gui.components.dashboard import BulbDashboard, BulbInfo
from src.gui.components.smooth_scrollable_frame import SmoothScrollableFrame
from src.gui.components.animations import AnimationMixin, ease_in_out_quad
from src.gui.components.tray import SystemTrayManager


def test_unchanged_metric_and_preview_do_not_redraw():
    var = Mock()
    var.get.return_value = "60"
    gui = SimpleNamespace(_sync_metric_vars={"fps": var})
    WizLightModernGUI._set_metric(gui, "fps", "60")
    var.set.assert_not_called()
    WizLightModernGUI._set_metric(gui, "fps", "59")
    var.set.assert_called_once_with("59")

    swatch = Mock()
    swatch.cget.return_value = "#010203"
    label = Mock()
    label.get.return_value = "RGB(1, 2, 3)"
    panel = SimpleNamespace(_swatch=swatch, _label_var=label)
    gui._format_debug_color = lambda color: f"RGB{color}"
    WizLightModernGUI._set_color_panel(gui, panel, (1, 2, 3))
    swatch.configure.assert_not_called()
    label.set.assert_not_called()
    WizLightModernGUI._set_color_panel(gui, panel, None)
    swatch.configure.assert_called_once_with(fg_color="#000000")
    label.set.assert_called_once_with("RGB(-)")


def test_running_button_is_updated_only_on_transitions():
    button, header = Mock(), Mock()
    gui = SimpleNamespace(sync_primary_button=button, header_runtime_var=header)
    WizLightModernGUI._set_sync_running_ui(gui, True)
    WizLightModernGUI._set_sync_running_ui(gui, True)
    assert button.configure.call_count == 1
    assert header.set.call_count == 1
    WizLightModernGUI._set_sync_running_ui(gui, False)
    assert button.configure.call_count == 2
    header.set.assert_called_with("Sync: idle")


def test_dashboard_preserves_cards_and_live_state_when_reopened():
    current = BulbInfo(ip="10.0.0.1", name="Desk", is_on=True, brightness=80)
    dashboard = SimpleNamespace(
        _bulbs=[current], _cards={current.ip: object()}, _auto_refresh=True,
        _rebuild_cards=Mock(), refresh_status=Mock(),
    )
    BulbDashboard.set_bulbs(dashboard, [BulbInfo(ip=current.ip, name=current.name)])
    assert dashboard._bulbs[0] is current
    dashboard._rebuild_cards.assert_not_called()
    dashboard.refresh_status.assert_called_once()

    renamed = BulbInfo(ip=current.ip, name="Renamed")
    BulbDashboard.set_bulbs(dashboard, [renamed])
    assert dashboard._bulbs == [renamed]
    dashboard._rebuild_cards.assert_called_once()


def test_hidden_diagnostics_do_not_update_detail_widgets():
    snapshot = {
        "target_colors": {}, "current_colors": {}, "capture_status": "OK",
        "currentFps": 10, "requestedFps": 24, "current_fps": 10,
        "average_frame_time_ms": 5.0, "send_rate_hz": 2.0,
        "motion_score": 0.0, "capture_method": "mss",
    }
    gui = SimpleNamespace(
        _screen_sync_debug_id=None,
        screen_sync=SimpleNamespace(is_running=True, debug_snapshot=snapshot),
        sync_target_preview=Mock(), sync_output_preview=Mock(),
        _set_color_panel=Mock(), _set_metric=Mock(), _set_sync_running_ui=Mock(),
        header_runtime_var=Mock(), advanced_debug_var=Mock(), root=Mock(),
        status_indicator=Mock(), _sync_status_color=Mock(return_value="green"),
        screen_sync_debug_runtime=Mock(), _refresh_screen_sync_debug=Mock(),
    )
    gui.advanced_debug_var.get.return_value = False
    WizLightModernGUI._refresh_screen_sync_debug(gui)
    gui.screen_sync_debug_runtime.set.assert_not_called()
    gui.root.after.assert_called_once_with(350, gui._refresh_screen_sync_debug)
    gui.status_indicator.configure.assert_called_once_with(fg_color="green")


def test_scroll_repaint_requests_are_coalesced():
    frame = SimpleNamespace(_repaint_after_id=None, after_idle=Mock(return_value="paint"),
                            _force_canvas_repaint=Mock())
    for _ in range(30):
        SmoothScrollableFrame._queue_canvas_repaint(frame)
    frame.after_idle.assert_called_once_with(frame._force_canvas_repaint)
    assert frame._repaint_after_id == "paint"


def test_animation_and_tray_compatibility_helpers_remain_available():
    assert ease_in_out_quad(0.5) == 0.5
    widget = Mock()
    widget.cget.side_effect = lambda key: {"border_color": "blue", "border_width": 3}[key]
    AnimationMixin().pulse_widget(widget)
    widget.configure.assert_called_once_with(border_color="#4CAF50", border_width=2)
    widget.after.call_args.args[1]()
    widget.configure.assert_called_with(border_color="blue", border_width=3)
    assert callable(SystemTrayManager.update_icon)
