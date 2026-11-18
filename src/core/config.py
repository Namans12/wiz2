"""Configuration management for WizLight."""

import json
import hashlib
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from threading import RLock
from typing import Optional

from .device_identity import mac_identity


SCREEN_SYNC_MODES = ("single", "zones")
SCREEN_SYNC_REGIONS = (
    "left",
    "center",
    "right",
    "top-left",
    "top",
    "top-right",
    "bottom-left",
    "bottom",
    "bottom-right",
)


@dataclass
class BulbConfig:
    """Configuration for a single WiZ bulb."""

    ip: str
    name: str
    mac: Optional[str] = None
    screen_sync_enabled: bool = True


COLOR_ALGORITHMS = ("auto", "weighted", "kmeans", "histogram")
COLOR_SOURCE_SPACES = ("auto", "srgb", "display-p3", "rec2020")
DEVICE_PROFILES = ("auto", "generic", "mac", "bravia", "wide_gamut_hdr")
SYNC_PROFILES = ("auto", "cinema", "action")
BRIGHTNESS_MODES = ("follow", "constant")
MIXING_MODELS = ("heuristic", "physical")


def _settings_from_json(settings_type, value):
    """Keep recognized valid fields while tolerating older/future schemas."""
    defaults = settings_type()
    if not isinstance(value, dict):
        return defaults
    kwargs = {}
    for definition in fields(settings_type):
        key = definition.name
        if key not in value:
            continue
        item, fallback = value[key], getattr(defaults, key)
        try:
            if isinstance(fallback, bool):
                if isinstance(item, str) and item.lower() in ("true", "false"):
                    item = item.lower() == "true"
                elif not isinstance(item, bool):
                    continue
            elif isinstance(fallback, (int, float)):
                if isinstance(item, bool) or not math.isfinite(float(item)):
                    continue
                item = type(fallback)(item)
            elif isinstance(fallback, dict):
                if not isinstance(item, dict):
                    continue
                item = {k: v for k, v in item.items() if isinstance(k, str) and isinstance(v, str)}
            elif item is not None and not isinstance(item, str):
                continue
            kwargs[key] = item
        except (ValueError, TypeError, OverflowError):
            continue
    return settings_type(**kwargs)


@dataclass
class ScreenSyncConfig:
    """Screen sync feature configuration."""

    enabled: bool = False
    mode: str = "single"
    fps: int = 24
    monitor: int = 1
    smoothing: float = 0.2
    sample_size: int = 56
    ignore_letterbox: bool = True
    edge_weight: float = 1.5
    color_boost: float = 1.18
    min_brightness: int = 20
    min_color_delta: int = 8
    bulb_layout: dict[str, str] = field(default_factory=dict)
    
    # V2 optimization settings
    use_gpu: bool = True
    adaptive_fps: bool = True
    min_fps: int = 10
    max_fps: int = 24
    color_algorithm: str = "auto"
    predictive_smoothing: bool = True
    source_color_space: str = "auto"
    hdr_tone_mapping: bool = False
    device_profile: str = "auto"
    sync_profile: str = "auto"
    brightness_follow: bool = True  # legacy; feeds brightness_mode default
    # "follow": bulb brightness tracks scene brightness (black turns it off).
    # "constant": brightness pinned at sync_brightness; only hue/sat track the
    # screen and black frames hold the last color.
    brightness_mode: Optional[str] = None
    sync_brightness: int = 180
    # "heuristic" (default): channel-gain + saturation-gated white mixing.
    # "physical" (experimental): solves the bulb's actual LED-primary mixing
    # equation instead -- see src/core/bulb_physical_model.py for the model
    # and its stated, unverified spectral assumptions. Try it live; it is
    # not proven better without a colorimeter.
    mixing_model: str = "heuristic"
    # Hold each bulb's Wi-Fi radio awake during sync with a chain of read-only
    # status queries, and use the replies to confirm delivery. Measured on a
    # WiZ ESP25 bulb: updates arrive in about 18 ms instead of about 117 ms.
    # Costs roughly 110 small packets per second per bulb while syncing.
    keep_awake: bool = True

    def __post_init__(self) -> None:
        self.keep_awake = bool(self.keep_awake)
        self.mode = self.mode if self.mode in SCREEN_SYNC_MODES else "single"
        self.fps = max(4, min(60, int(self.fps)))
        self.monitor = int(self.monitor)
        self.smoothing = max(0.05, min(1.0, float(self.smoothing)))
        self.sample_size = max(16, min(96, int(self.sample_size)))
        self.edge_weight = max(1.0, min(2.5, float(self.edge_weight)))
        self.color_boost = max(1.0, min(2.0, float(self.color_boost)))
        self.min_brightness = max(0, min(255, int(self.min_brightness)))
        self.min_color_delta = max(1, min(255, int(self.min_color_delta)))
        self.bulb_layout = {
            ip: region
            for ip, region in dict(self.bulb_layout).items()
            if region in SCREEN_SYNC_REGIONS
        }
        # V2 validation
        self.min_fps = max(4, min(30, int(self.min_fps)))
        self.max_fps = max(self.min_fps, min(60, int(self.max_fps)))
        self.color_algorithm = self.color_algorithm if self.color_algorithm in COLOR_ALGORITHMS else "auto"
        self.use_gpu = bool(self.use_gpu)
        self.adaptive_fps = bool(self.adaptive_fps)
        self.predictive_smoothing = bool(self.predictive_smoothing)
        normalized_space = str(self.source_color_space).lower().replace("_", "-")
        aliases = {
            "p3": "display-p3",
            "display_p3": "display-p3",
            "bt2020": "rec2020",
            "bt.2020": "rec2020",
            "rec-2020": "rec2020",
        }
        self.source_color_space = aliases.get(normalized_space, normalized_space)
        if self.source_color_space not in COLOR_SOURCE_SPACES:
            self.source_color_space = "auto"
        self.hdr_tone_mapping = bool(self.hdr_tone_mapping)
        normalized_profile = str(self.device_profile).lower().replace("-", "_")
        if normalized_profile not in DEVICE_PROFILES:
            normalized_profile = "auto"
        self.device_profile = normalized_profile
        normalized_sync = str(self.sync_profile).lower()
        self.sync_profile = normalized_sync if normalized_sync in SYNC_PROFILES else "auto"
        self.brightness_follow = bool(self.brightness_follow)
        if self.brightness_mode is None:
            self.brightness_mode = "follow" if self.brightness_follow else "constant"
        normalized_mode = str(self.brightness_mode).lower()
        self.brightness_mode = normalized_mode if normalized_mode in BRIGHTNESS_MODES else "follow"
        self.brightness_follow = self.brightness_mode == "follow"
        self.sync_brightness = max(30, min(255, int(self.sync_brightness)))
        normalized_mixing_model = str(self.mixing_model).lower()
        self.mixing_model = normalized_mixing_model if normalized_mixing_model in MIXING_MODELS else "heuristic"


@dataclass
class ClapConfig:
    """Clap detection feature configuration."""

    enabled: bool = False
    threshold: float = 0.055
    rms_threshold: float = 0.01
    min_peak_to_rms: float = 2.7
    adaptive_multiplier: float = 5.0
    max_duration: float = 0.2
    cooldown: float = 0.45
    double_clap: bool = True
    double_clap_window: float = 0.85

    def __post_init__(self) -> None:
        self.enabled = bool(self.enabled)
        self.threshold = float(self.threshold)
        if self.threshold > 0.2:
            self.threshold = 0.055
        self.threshold = max(0.01, min(0.2, self.threshold))
        self.rms_threshold = max(0.002, min(0.05, float(self.rms_threshold)))
        self.min_peak_to_rms = max(1.2, min(6.0, float(self.min_peak_to_rms)))
        self.adaptive_multiplier = max(1.5, min(12.0, float(self.adaptive_multiplier)))
        self.max_duration = max(0.05, min(0.4, float(self.max_duration)))
        self.cooldown = max(0.1, min(2.0, float(self.cooldown)))
        self.double_clap = bool(self.double_clap)
        self.double_clap_window = max(0.25, min(1.5, float(self.double_clap_window)))


@dataclass
class Config:
    """Main application configuration."""

    bulbs: list[BulbConfig] = field(default_factory=list)
    screen_sync: ScreenSyncConfig = field(default_factory=ScreenSyncConfig)
    clap: ClapConfig = field(default_factory=ClapConfig)
    default_brightness: int = 128
    default_color_temp: int = 4000
    _config_path: Path = field(default=None, repr=False)
    _load_warning: Optional[str] = field(default=None, repr=False)
    _save_lock: object = field(default_factory=RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._config_path is None:
            self._config_path = Path.home() / ".wizlight" / "config.json"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load configuration from file."""

        config_path = path or Path.home() / ".wizlight" / "config.json"
        if not config_path.exists():
            config = cls(_config_path=config_path)
            config.save()
            return config

        raw = config_path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
        except (ValueError, UnicodeError):
            backup = config_path.with_name(
                f"{config_path.stem}.corrupt-{hashlib.sha256(raw).hexdigest()[:16]}{config_path.suffix}"
            )
            try:
                with backup.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError:
                if backup.read_bytes() != raw:
                    raise OSError("Recovery backup does not match the original settings")
            return cls(_config_path=config_path, _load_warning=f"Settings could not be read; original saved as {backup.name}")

        bulb_values = data.get("bulbs", [])
        bulbs = []
        for bulb in bulb_values if isinstance(bulb_values, list) else []:
            if not isinstance(bulb, dict) or not isinstance(bulb.get("ip"), str) or not bulb["ip"].strip():
                continue
            bulbs.append(BulbConfig(
                ip=bulb["ip"].strip(),
                name=bulb.get("name") if isinstance(bulb.get("name"), str) else "WiZ Bulb",
                mac=bulb.get("mac") if isinstance(bulb.get("mac"), str) else None,
                screen_sync_enabled=bulb.get("screen_sync_enabled") is not False,
            ))
        screen_sync = _settings_from_json(ScreenSyncConfig, data.get("screen_sync", {}))
        clap = _settings_from_json(ClapConfig, data.get("clap", {}))

        return cls(
            bulbs=bulbs,
            screen_sync=screen_sync,
            clap=clap,
            default_brightness=data.get("default_brightness", 128),
            default_color_temp=data.get("default_color_temp", 4000),
            _config_path=config_path,
        )

    def save(self) -> None:
        """Save configuration to file."""

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "bulbs": [asdict(bulb) for bulb in self.bulbs],
            "screen_sync": asdict(self.screen_sync),
            "clap": asdict(self.clap),
            "default_brightness": self.default_brightness,
            "default_color_temp": self.default_color_temp,
        }

        temporary = None
        with self._save_lock:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".tmp",
                    dir=self._config_path.parent, delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._config_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def screen_sync_bulbs(self) -> list[BulbConfig]:
        return [bulb for bulb in self.bulbs if bulb.screen_sync_enabled]

    def set_bulb_screen_sync(self, ip: str, enabled: bool) -> bool:
        """Persist participation for a saved bulb and report whether it changed."""
        for bulb in self.bulbs:
            if bulb.ip == ip and bulb.screen_sync_enabled != enabled:
                bulb.screen_sync_enabled = enabled
                self.save()
                return True
        return False

    def add_bulb(self, ip: str, name: str, mac: Optional[str] = None, *, preserve_name: bool = False) -> None:
        """Add a bulb to configuration."""

        preserved_region = self.screen_sync.bulb_layout.get(ip)
        normalized_mac = mac.lower() if mac else None
        identity = mac_identity(mac)
        at_address = next((bulb for bulb in self.bulbs if bulb.ip == ip), None)
        known_bulb = next(
            (bulb for bulb in self.bulbs if identity and mac_identity(bulb.mac) == identity),
            None,
        )
        if known_bulb is None and at_address is not None:
            address_identity = mac_identity(at_address.mac)
            if not identity or not address_identity:
                known_bulb = at_address
        stale_ips = {ip}
        for bulb in self.bulbs:
            if bulb.ip == ip or (identity and mac_identity(bulb.mac) == identity):
                if preserve_name:
                    name = bulb.name
                if mac is None:
                    normalized_mac = bulb.mac
                    identity = mac_identity(bulb.mac)
        if identity:
            for bulb in self.bulbs:
                if mac_identity(bulb.mac) == identity:
                    stale_ips.add(bulb.ip)
                    preserved_region = preserved_region or self.screen_sync.bulb_layout.get(bulb.ip)

        self.bulbs = [bulb for bulb in self.bulbs if bulb.ip not in stale_ips]
        for stale_ip in stale_ips - {ip}:
            self.screen_sync.bulb_layout.pop(stale_ip, None)
        if preserved_region:
            self.screen_sync.bulb_layout[ip] = preserved_region
        self.bulbs.append(BulbConfig(
            ip=ip, name=name, mac=normalized_mac or mac,
            screen_sync_enabled=known_bulb.screen_sync_enabled if known_bulb is not None else True,
        ))
        self.save()

    def reassign_bulb_addresses(self, moves: list[tuple[str, str, str]]) -> int:
        """Apply a verified batch of address changes without losing identities or zones."""
        if not moves:
            return 0
        old_ips = [old for old, _, _ in moves]
        new_ips = [new for _, new, _ in moves]
        if len(set(old_ips)) != len(moves) or len(set(new_ips)) != len(moves):
            return 0
        by_ip = {bulb.ip: bulb for bulb in self.bulbs}
        if len(by_ip) != len(self.bulbs):
            return 0
        for old, new, mac in moves:
            bulb = by_ip.get(old)
            if (bulb is None or not mac_identity(mac) or mac_identity(bulb.mac) != mac_identity(mac)
                    or not new or old == new or (new in by_ip and new not in old_ips)):
                return 0
        mapping = dict(zip(old_ips, new_ips))
        self.bulbs = [replace(bulb, ip=mapping.get(bulb.ip, bulb.ip)) for bulb in self.bulbs]
        self.screen_sync.bulb_layout = {
            mapping.get(ip, ip): region for ip, region in self.screen_sync.bulb_layout.items()
            if ip not in new_ips or ip in old_ips
        }
        self.save()
        return len(moves)

    def remove_bulb(self, ip: str) -> bool:
        """Remove a bulb from configuration."""

        original_len = len(self.bulbs)
        self.bulbs = [bulb for bulb in self.bulbs if bulb.ip != ip]
        self.screen_sync.bulb_layout.pop(ip, None)
        if len(self.bulbs) < original_len:
            self.save()
            return True
        return False

    def remove_bulbs(self, ips: list[str]) -> int:
        """Remove multiple bulbs from configuration and return the count removed."""

        stale_ips = set(ips)
        if not stale_ips:
            return 0

        original_len = len(self.bulbs)
        self.bulbs = [bulb for bulb in self.bulbs if bulb.ip not in stale_ips]
        for ip in stale_ips:
            self.screen_sync.bulb_layout.pop(ip, None)
        removed = original_len - len(self.bulbs)
        if removed:
            self.save()
        return removed
