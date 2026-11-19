"""Prepare screen-sync colour profiles before starting the capture worker."""

from .screen_sync_v2 import resolve_auto_display_profile


async def prepare_screen_sync_mapping(controller, ips, settings):
    resolved = resolve_auto_display_profile(settings)
    source = resolved.get("source_color_space", settings.source_color_space)
    profile = resolved.get("device_profile", "generic")
    kwargs = {
        "source_color_space": "srgb" if source in (None, "", "auto") else source,
        "device_profile": "generic" if profile in (None, "", "auto") else profile,
        "cool_mix_bias": float(resolved.get("cool_mix_bias", 0.0)),
    }
    if hasattr(settings, "mixing_model"):
        kwargs["mixing_model"] = settings.mixing_model
    await controller.prepare_screen_sync_mappers(ips, **kwargs)
