"""Regression tests: a partial zone update must only touch its own lamp."""

import numpy as np

from src.features import screen_sync_v2 as sync_module
from src.features.screen_sync import build_bulb_color_map
from src.features.screen_sync_v2 import OptimizedCaptureConfig, OptimizedScreenSync

LAYOUT = {"A": "left", "B": "right"}
RED = (255, 0, 0)
BLUE = (0, 0, 255)


def test_one_zone_update_only_touches_its_lamp():
    assert build_bulb_color_map(["A", "B"], {"right": BLUE}, "zones", LAYOUT) == {"B": BLUE}
    assert build_bulb_color_map(["A", "B"], {"left": RED}, "zones", LAYOUT) == {"A": RED}


def test_both_zone_update_routes_each_lamp():
    colors = {"left": RED, "right": BLUE}
    assert build_bulb_color_map(["A", "B"], colors, "zones", LAYOUT) == {"A": RED, "B": BLUE}


def test_all_key_paints_every_bulb():
    assert build_bulb_color_map(["A", "B"], {"all": RED}, "zones", LAYOUT) == {"A": RED, "B": RED}


def test_single_mode_paints_every_bulb():
    assert build_bulb_color_map(["A", "B"], {"all": RED}, "single", {}) == {"A": RED, "B": RED}
    # Zones requested but fewer than two regions configured falls back to single.
    assert build_bulb_color_map(["A", "B"], {"all": BLUE}, "zones", {"A": "left"}) == {"A": BLUE, "B": BLUE}


def test_unassigned_bulb_partial_update_is_omitted():
    layout = {"A": "left", "B": "right"}
    result = build_bulb_color_map(["A", "B", "C"], {"right": BLUE}, "zones", layout)
    assert result == {"B": BLUE}


def test_unassigned_bulb_full_update_gets_average():
    layout = {"A": "left", "B": "right"}
    result = build_bulb_color_map(["A", "B", "C"], {"left": (200, 0, 0), "right": (0, 0, 100)}, "zones", layout)
    assert result["A"] == (200, 0, 0)
    assert result["B"] == (0, 0, 100)
    assert result["C"] == (100, 0, 50)


def test_empty_inputs():
    assert build_bulb_color_map([], {"right": BLUE}, "zones", LAYOUT) == {}
    assert build_bulb_color_map(["A", "B"], {}, "zones", LAYOUT) == {}
    assert build_bulb_color_map([], {}, "single", {}) == {}


def test_engine_partial_updates_never_paint_left_lamp_with_right_colour(monkeypatch):
    ips = ["A", "B"]
    sent_to_lamps: list[dict[str, tuple[int, int, int]]] = []
    raw_updates: list[dict[str, tuple[int, int, int]]] = []

    def on_change(colors):
        raw_updates.append(dict(colors))
        sent_to_lamps.append(build_bulb_color_map(ips, colors, "zones", LAYOUT))

    engine = OptimizedScreenSync(
        on_change,
        OptimizedCaptureConfig(mode="zones", active_regions=("left", "right"),
                               use_gpu=False, parallel_regions=False, adaptive_fps=False,
                               predictive_smoothing=False, smoothing=1.0),
    )
    right_colors = iter([BLUE, (0, 255, 0), (255, 255, 0), (0, 255, 255)])
    frames = {"count": 0}

    def extract(_frame):
        frames["count"] += 1
        if frames["count"] > 4:
            engine._running = False
        return {"left": RED, "right": next(right_colors, BLUE)}

    monkeypatch.setattr(engine, "_capture_frame", lambda: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(engine, "_extract_colors", extract)
    monkeypatch.setattr(engine._motion_detector, "update", lambda frame: 0.0)
    monkeypatch.setattr(sync_module.time, "sleep", lambda duration: None)
    engine._running = True
    engine._sync_loop()

    partial = [m for raw, m in zip(raw_updates, sent_to_lamps) if "left" not in raw]
    assert partial, "expected at least one right-only update"
    for lamp_map in sent_to_lamps:
        assert lamp_map.get("A", RED) == RED
    for lamp_map in partial:
        assert "A" not in lamp_map
