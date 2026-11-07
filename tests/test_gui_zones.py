"""Regression test for the zone/multi-bulb GUI wiring bug.

Previously ``_save_screen_sync_settings`` hardcoded ``settings.mode = "single"``
and ``settings.bulb_layout = {}`` on every save, and ``_start_screen_sync``
hardcoded ``active_regions = ()`` / ``mode = "single"`` and passed them to
``build_bulb_color_map`` -- so the zone-layout UI (per-bulb region dropdowns)
was fully built, populated, and completely disconnected from the running
engine. This exercises the real GUI methods end-to-end (no capture thread,
no network) to prove the selected mode and per-bulb region assignments now
actually reach ``ScreenSyncConfig``.
"""

import tempfile
from pathlib import Path

import pytest

from src.core.config import BulbConfig, Config
from src.features.screen_sync import effective_screen_sync_mode, resolve_active_regions

ctk = pytest.importorskip("customtkinter")

from src.gui import modern_app as modern_app_module  # noqa: E402 -- after the importorskip guard


@pytest.fixture(scope="module")
def gui(monkeypatch_module):
    """A single real WizLightModernGUI, reused across this module's tests.

    Tkinter/Tcl only tolerates one interpreter cleanly per process; creating
    and destroying a ctk.CTk() root per-test destabilizes global Tcl state
    once other test modules have already run. One shared root, reset between
    tests, avoids that without weakening what's being verified.
    """

    tmp_path = Path(tempfile.mkdtemp()) / "config.json"
    monkeypatch_module.setattr(
        Config, "load", classmethod(lambda cls, path=None: Config(_config_path=path or tmp_path))
    )
    monkeypatch_module.setattr(modern_app_module, "is_tray_available", lambda: False)

    app = modern_app_module.WizLightModernGUI()
    try:
        yield app
    finally:
        app._on_close()


@pytest.fixture(autouse=True)
def _reset_gui_state(gui):
    """Clear per-test mutable state so the shared fixture behaves like a fresh app."""

    gui.config.bulbs = []
    gui.config.screen_sync.bulb_layout = {}
    gui.config.screen_sync.mode = "single"
    gui.config.screen_sync.mixing_model = "heuristic"
    gui._screen_layout_vars = {}
    gui.sync_mode_value.set("Single bulb")
    gui.mixing_model_value.set("Standard")
    yield


@pytest.fixture(scope="module")
def monkeypatch_module():
    """A module-scoped monkeypatch (pytest's builtin `monkeypatch` is function-scoped)."""

    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_zone_layout_persists_to_config(gui):
    gui.config.bulbs = [
        BulbConfig(ip="10.0.0.1", name="Left Lamp"),
        BulbConfig(ip="10.0.0.2", name="Right Lamp"),
    ]
    gui._refresh_screen_sync_layout_controls()
    gui._screen_layout_vars["10.0.0.1"].set("Left")
    gui._screen_layout_vars["10.0.0.2"].set("Right")
    gui.sync_mode_value.set("Left / Right zones")

    layout = gui._current_screen_layout()
    assert layout == {"10.0.0.1": "left", "10.0.0.2": "right"}

    gui._save_screen_sync_settings()

    # This is the exact bug: mode/bulb_layout used to be hardcoded regardless
    # of GUI state.
    assert gui.config.screen_sync.mode == "zones"
    assert gui.config.screen_sync.bulb_layout == {"10.0.0.1": "left", "10.0.0.2": "right"}

    # And the resolved runtime mode (what actually reaches the engine +
    # build_bulb_color_map) must follow through.
    ips = [b.ip for b in gui.config.bulbs]
    active_regions = resolve_active_regions(gui.config.screen_sync.bulb_layout, ips)
    mode = effective_screen_sync_mode(gui.config.screen_sync.mode, active_regions)
    assert active_regions == ("left", "right")
    assert mode == "zones"


def test_single_bulb_mode_ignores_unassigned_layout(gui):
    gui.config.bulbs = [BulbConfig(ip="10.0.0.1", name="Only Lamp")]
    gui._refresh_screen_sync_layout_controls()
    gui.sync_mode_value.set("Single bulb")

    gui._save_screen_sync_settings()

    assert gui.config.screen_sync.mode == "single"
    # Only one bulb assigned -> resolve_active_regions requires >=2 distinct
    # regions, so this must fall back to single even if "zones" were selected.
    ips = [b.ip for b in gui.config.bulbs]
    active_regions = resolve_active_regions(gui.config.screen_sync.bulb_layout, ips)
    assert effective_screen_sync_mode("zones", active_regions) == "single"


def test_layout_frame_widget_exists_and_populates(gui):
    """`self.layout_frame` must be a real widget (it was never assigned)."""

    assert hasattr(gui, "layout_frame")
    gui.config.bulbs = [BulbConfig(ip="10.0.0.9", name="Some Lamp")]
    gui._refresh_screen_sync_layout_controls()
    assert gui.layout_frame.winfo_children()


def test_mixing_model_toggle_persists_to_config(gui):
    """The Color Mixing toggle must reach ScreenSyncConfig.mixing_model."""

    assert gui.config.screen_sync.mixing_model == "heuristic"
    gui.mixing_model_value.set("Physical (experimental)")
    gui._save_screen_sync_settings()
    assert gui.config.screen_sync.mixing_model == "physical"

    gui.mixing_model_value.set("Standard")
    gui._save_screen_sync_settings()
    assert gui.config.screen_sync.mixing_model == "heuristic"


def test_mixing_model_hint_updates_on_toggle(gui):
    gui.mixing_model_value.set("Standard")
    gui._on_mixing_model_change()
    assert "proven default" in gui.mixing_model_hint.cget("text")

    gui.mixing_model_value.set("Physical (experimental)")
    gui._on_mixing_model_change()
    assert "Not proven better" in gui.mixing_model_hint.cget("text")


@pytest.mark.parametrize("size", ["860x580", "1080x780"])
def test_desktop_navigation_keeps_primary_action_and_status_visible(gui, size):
    gui.root.geometry(size)
    gui._switch_view("sync")
    gui.root.update()
    assert gui.sync_primary_button.winfo_viewable()
    assert gui.status_label.winfo_viewable()
    assert gui.sync_primary_button.winfo_rooty() + gui.sync_primary_button.winfo_height() <= gui.status_label.winfo_rooty()
    for view in ("lights", "tools", "sync"):
        gui._view_buttons[view].invoke()
        gui.root.update()
        assert gui._current_view == view
        assert gui._page_title_var.get() == gui._page_labels[view][0]
        assert gui.status_label.winfo_viewable()
        assert all(button.winfo_viewable() for button in gui._view_buttons.values())



def test_picture_settings_jump_scrolls_to_controls(gui):
    gui._scroll_to_sync_settings()
    gui.root.update()
    assert gui._current_view == "sync"
    assert gui._controls_frame._parent_canvas.yview()[0] > 0
    assert gui._sync_settings_card.winfo_rooty() < gui.status_label.winfo_rooty()
    gui._controls_frame._parent_canvas.yview_moveto(0)


def test_live_details_expand_and_collapse_without_hiding_controls(gui):
    gui._switch_view("sync")
    gui.root.update()
    assert not gui._live_details.winfo_ismapped()
    gui._live_details_button.invoke()
    gui.root.update()
    assert gui._live_details.winfo_ismapped()
    assert gui.sync_primary_button.winfo_viewable()
    gui._live_details_button.invoke()
    gui.root.update()
    assert not gui._live_details.winfo_ismapped()
    assert gui.sync_primary_button.winfo_viewable()
