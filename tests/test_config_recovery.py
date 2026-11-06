import json
from unittest.mock import patch

import pytest

from src.core.config import Config


def test_corrupt_settings_are_preserved_before_recovery(tmp_path):
    path = tmp_path / "config.json"
    original = b'{"screen_sync":'
    path.write_bytes(original)
    config = Config.load(path)
    assert config._load_warning
    assert path.read_bytes() == original
    copies = list(tmp_path.glob("config.corrupt-*.json"))
    assert len(copies) == 1 and copies[0].read_bytes() == original
    config.save()
    assert json.loads(path.read_text())["bulbs"] == []
    assert copies[0].read_bytes() == original


def test_invalid_and_future_fields_do_not_discard_valid_settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "bulbs": [{"ip": "10.0.0.2", "name": "Desk", "future": True}, None],
        "screen_sync": {"smoothing": "bad", "max_fps": 28, "use_gpu": "false", "future": 42},
        "clap": {"double_clap": "false"},
    }))
    config = Config.load(path)
    assert config.bulbs[0].name == "Desk"
    assert config.screen_sync.max_fps == 28
    assert config.screen_sync.use_gpu is False
    assert config.clap.double_clap is False
    assert config.screen_sync.smoothing == .2


def test_failed_atomic_replace_keeps_previous_settings(tmp_path):
    path = tmp_path / "config.json"
    config = Config.load(path)
    old = path.read_bytes()
    config.default_brightness = 75
    with patch("src.core.config.os.replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError, match="disk error"):
            config.save()
    assert path.read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))


def test_rediscovery_preserves_name_and_zone_on_changed_ip(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_bulb("10.0.0.2", "My desk", "AA:BB")
    config.screen_sync.bulb_layout["10.0.0.2"] = "left"
    config.add_bulb("10.0.0.3", "Bulb 1", "aa:bb", preserve_name=True)
    assert config.bulbs[0].name == "My desk"
    assert config.screen_sync.bulb_layout == {"10.0.0.3": "left"}
