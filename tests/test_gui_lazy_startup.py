"""Startup builds only the Screen sync page; the other pages are built on demand.

Tests in this module share one real GUI (Tk tolerates one interpreter per
process) and run in definition order: the early tests rely on the pages not
having been visited yet.
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.config import BulbConfig, Config

ctk = pytest.importorskip("customtkinter")

from src.gui import modern_app as modern_app_module  # noqa: E402 -- after the importorskip guard

REPO_ROOT = Path(__file__).resolve().parents[1]
LIGHTS_WIDGETS = (
    "toggle_power_button", "turn_off_button", "brightness_slider", "color_preview",
    "pick_color_button", "preset_menu", "temp_slider",
)
BULBS_WIDGETS = ("discover_button", "remove_stale_button", "add_bulb_entry", "bulb_info")


@pytest.fixture(scope="module")
def gui():
    tmp_path = Path(tempfile.mkdtemp()) / "config.json"
    mp = pytest.MonkeyPatch()
    mp.setattr(Config, "load", classmethod(lambda cls, path=None: Config(_config_path=path or tmp_path)))
    mp.setattr(modern_app_module, "is_tray_available", lambda: False)
    app = modern_app_module.WizLightModernGUI()
    try:
        yield app
    finally:
        app._on_close()
        mp.undo()


def _discard_page(gui, view, names):
    """Throw a page away so it can be built again from scratch."""
    gui._view_frames.pop(view).destroy()
    for name in names:
        if hasattr(gui, name):
            delattr(gui, name)


def _cancel_prebuild(gui):
    if gui._prebuild_after_id:
        gui.root.after_cancel(gui._prebuild_after_id)
        gui._prebuild_after_id = None


def test_startup_builds_only_the_sync_page(gui):
    assert set(gui._view_frames) == {"sync"}
    assert gui._current_view == "sync"
    assert gui._dashboard is None
    for name in LIGHTS_WIDGETS + BULBS_WIDGETS:
        assert not hasattr(gui, name), name
    assert gui.sync_primary_button.winfo_exists()


def test_status_and_bulb_refresh_before_pages_exist_do_not_raise(gui):
    gui.config.bulbs = [BulbConfig(ip="10.0.0.1", name="Lamp")]
    gui._update_bulb_status()
    gui._refresh_after_bulb_config_change()
    gui._update_dashboard()
    engine = Mock()
    gui.screen_sync = engine
    gui._update_sync_preview(engine, (1, 2, 3))
    gui.screen_sync = None
    assert gui._exclude_bulb_before_power_off("10.0.0.1") is None
    gui._set_bulb_screen_sync("10.0.0.1", False)
    assert set(gui._view_frames) == {"sync"}
    assert gui.header_bulbs_var.get() == "Bulbs: 1"


def test_pages_built_late_pick_up_bulb_state(gui):
    gui.config.bulbs = [BulbConfig(ip="10.0.0.1", name="Lamp")]
    gui._switch_view("lights")
    assert gui.toggle_power_button.cget("state") == "normal"
    assert gui.preset_menu.cget("state") == "normal"
    gui._switch_view("sync")


def test_tray_quick_actions_build_the_lights_page_when_needed(gui):
    _discard_page(gui, "lights", LIGHTS_WIDGETS)
    gui.config.bulbs = []
    gui._set_brightness_quick(100)
    assert "lights" in gui._view_frames
    assert int(gui.brightness_slider.get()) == 100
    _discard_page(gui, "lights", LIGHTS_WIDGETS)
    gui._set_temp_quick(3000)
    assert "lights" in gui._view_frames
    assert int(gui.temp_slider.get()) == 3000
    assert gui.toggle_power_button.cget("state") == "disabled"


@pytest.mark.parametrize("view", ["lights", "bulbs", "tools", "sync"])
def test_each_page_exists_after_switching_to_it(gui, view):
    gui.config.bulbs = []
    gui._switch_view(view)
    gui.root.update_idletasks()
    assert view in gui._view_frames
    assert gui._current_view == view
    assert gui._view_host.winfo_children()[-1] == gui._view_frames[view]._parent_frame
    if view == "lights":
        assert all(hasattr(gui, name) for name in LIGHTS_WIDGETS)
    if view == "bulbs":
        assert all(hasattr(gui, name) for name in BULBS_WIDGETS)
        assert gui._dashboard is not None
        assert gui.bulb_info.cget("text") == "No bulbs found"
        assert gui.remove_stale_button.cget("state") == "disabled"


def test_background_prebuild_builds_remaining_pages_without_polling_bulbs(gui):
    gui._dashboard.stop_auto_refresh()
    _discard_page(gui, "tools", ())
    _discard_page(gui, "bulbs", BULBS_WIDGETS)
    gui._dashboard = None
    gui.config.bulbs = [BulbConfig(ip="10.0.0.1", name="Lamp")]
    gui._switch_view("sync")

    gui._prebuild_next_page()  # tools
    _cancel_prebuild(gui)
    assert "tools" in gui._view_frames
    gui._prebuild_next_page()  # bulbs
    _cancel_prebuild(gui)
    assert "bulbs" in gui._view_frames and gui._dashboard is not None

    gui._dashboard.refresh_status = Mock()
    gui._prebuild_next_page()  # dashboard cards, built without polling
    _cancel_prebuild(gui)
    assert gui._dashboard.has_cards()
    gui._dashboard.refresh_status.assert_not_called()
    assert gui._current_view == "sync"

    gui._prebuild_next_page()  # nothing left: the chain ends
    assert gui._prebuild_after_id is None

    gui._switch_view("bulbs")  # reuses the prebuilt cards
    assert len(gui._dashboard._cards) == 1
    gui._dashboard.stop_auto_refresh()
    gui._switch_view("sync")


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    prelude = f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r});"
    return subprocess.run(
        [sys.executable, "-I", "-c", prelude + code],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_importing_clap_detector_does_not_import_sounddevice():
    result = _run_isolated(
        "import src.features.clap_detector as c;"
        "assert 'sounddevice' not in sys.modules, 'sounddevice imported eagerly';"
        "assert callable(c.list_audio_devices)"
    )
    assert result.returncode == 0, result.stderr


def test_importing_the_gui_module_does_not_import_sounddevice():
    result = _run_isolated(
        "import src.gui.modern_app;"
        "assert 'sounddevice' not in sys.modules, 'sounddevice imported eagerly'"
    )
    assert result.returncode == 0, result.stderr
