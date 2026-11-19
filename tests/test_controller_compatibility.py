"""Retain the desktop controller's previously public control methods."""

from unittest.mock import AsyncMock, Mock

import pytest

from src.core.bulb_controller import BulbController


@pytest.mark.asyncio
async def test_legacy_brightness_and_scene_controls():
    controller = BulbController()
    bulb = Mock()
    bulb.turn_on = AsyncMock()
    controller._get_bulb = lambda ip: bulb
    await controller.set_brightness("lamp", 999)
    await controller.set_scene("lamp", 1)
    assert bulb.turn_on.await_count == 2


@pytest.mark.asyncio
async def test_legacy_bulk_color_methods_forward_each_light():
    controller = BulbController()
    controller.set_rgb = AsyncMock()
    controller.set_rgb_exact = AsyncMock()
    await controller.set_rgb_all_exact(["a", "b"], 1, 2, 3, 80)
    await controller.set_rgb_map({"a": (4, 5, 6)}, 90)
    await controller.set_rgb_map_exact({"b": (7, 8, 9)}, 100)
    controller.set_rgb.assert_awaited_once_with("a", 4, 5, 6, 90)
    assert controller.set_rgb_exact.await_count == 3
    controller.set_rgb_exact.assert_any_await("b", 7, 8, 9, 100)
