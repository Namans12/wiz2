"""Live diagnostics should not redraw unchanged widgets every polling tick."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.gui.modern_app import WizLightModernGUI


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
