"""Shared color/brightness handoff for GUI and standalone screen sync."""

from dataclasses import dataclass
from threading import RLock
from typing import Optional

from .screen_sync_v2 import resolve_auto_display_profile
from .sync_dispatcher import CoalescingAsyncDispatcher


@dataclass(frozen=True)
class ScreenSyncUpdate:
    colors: dict[str, tuple[int, int, int]]
    brightness: Optional[int]


async def prepare_screen_sync_mapping(controller, ips, settings) -> None:
    """Use the same display/model contract in every desktop launcher."""
    resolved = resolve_auto_display_profile(settings)
    source = resolved.get("source_color_space", settings.source_color_space)
    profile = resolved.get("device_profile", "generic")
    await controller.prepare_screen_sync_mappers(
        ips,
        source_color_space="srgb" if source in (None, "", "auto") else source,
        device_profile="generic" if profile in (None, "", "auto") else profile,
        cool_mix_bias=float(resolved.get("cool_mix_bias", 0.0)),
        mixing_model=settings.mixing_model,
    )


class ScreenSyncTransport:
    """Own fast and fallback senders with one immutable update snapshot."""

    def __init__(self, controller, submit, fast_sender):
        self._controller = controller
        self.fast_sender = fast_sender
        self._lock = RLock()
        self._closed = False
        self.dispatcher = CoalescingAsyncDispatcher(submit, self._apply_update)

    async def _apply_update(self, update: ScreenSyncUpdate) -> None:
        await self._controller.set_screen_sync_map(update.colors, update.brightness)

    def send(self, colors, brightness: Optional[int]) -> None:
        with self._lock:
            if self._closed or not colors:
                return
            update = ScreenSyncUpdate(dict(colors), brightness)
            payloads = self._controller.map_screen_sync_payloads_cached(update.colors)
            if len(payloads) == len(update.colors):
                self.fast_sender.send_payload_map(payloads, update.brightness)
            else:
                self.dispatcher.submit(update)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.dispatcher.close()
        finally:
            self.fast_sender.close()
