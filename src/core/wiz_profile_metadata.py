"""Defensive parsing of optional WiZ device profile hints."""

from dataclasses import dataclass


def _integer(value, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if minimum <= parsed <= maximum else default


@dataclass(frozen=True)
class WizProfileHints:
    white_channels: int
    white_to_color_ratio: int
    rgb_channel_current: tuple[int, int, int]
    white_channel_current: tuple[int, ...]
    render_factor: tuple[int, ...]
    fade_in_ms: int
    fade_out_ms: int


def parse_wiz_profile_hints(model: dict, user: dict, defaults) -> WizProfileHints:
    """Use safe defaults for missing or malformed fields from bulb firmware."""
    current = defaults.rgb_channel_current
    white_current: tuple[int, ...] = ()
    drivers = model.get("i2cDrv")
    if isinstance(drivers, list) and drivers and isinstance(drivers[0], dict):
        values = drivers[0].get("curr")
        if isinstance(values, (list, tuple)) and len(values) >= 3:
            current = tuple(_integer(v, current[i], 1, 65535) for i, v in enumerate(values[:3]))
            white_current = tuple(
                parsed for parsed in (_integer(v, 0, 1, 65535) for v in values[3:5]) if parsed > 0
            )

    raw_factors = model.get("renderFactor")
    factors = ()
    if isinstance(raw_factors, (list, tuple)):
        try:
            factors = tuple(int(v) for v in raw_factors)
        except (TypeError, ValueError, OverflowError):
            pass
    return WizProfileHints(
        white_channels=_integer(model.get("nowc"), defaults.white_channels, 0, 2),
        white_to_color_ratio=_integer(model.get("wcr"), defaults.white_to_color_ratio, 0, 100),
        rgb_channel_current=current,
        white_channel_current=white_current,
        render_factor=factors,
        fade_in_ms=_integer(user.get("fadeIn"), 0, 0, 2147483647),
        fade_out_ms=_integer(user.get("fadeOut"), 0, 0, 2147483647),
    )
