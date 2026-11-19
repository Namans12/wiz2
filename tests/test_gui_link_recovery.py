"""A bulb silent on the keep-awake link starts the address poll early."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.gui.modern_app import (
    LINK_CHECK_INTERVAL_MS,
    LINK_EARLY_RECOVERY_S,
    WizLightModernGUI,
)


def make_app(stats, token=None):
    app = SimpleNamespace(
        _closing=False,
        screen_sync=SimpleNamespace(is_running=True),
        _screen_sync_transport=SimpleNamespace(fast_sender=SimpleNamespace(link_stats=stats)),
        root=SimpleNamespace(after=Mock(return_value="timer"), after_cancel=Mock()),
        _bulb_recovery_token=token,
        _bulb_recovery_monitor_id="poll30",
        _link_check_id=None,
        _poll_sync_bulbs=Mock(),
    )
    app._schedule_link_check = lambda: WizLightModernGUI._schedule_link_check(app)
    app._check_link_silence = lambda: WizLightModernGUI._check_link_silence(app)
    return app


def tick(app):
    WizLightModernGUI._check_link_silence(app)


def test_two_second_gap_does_not_trigger_recovery():
    stats = [{"ip": "10.0.0.2", "silent_for_s": 2.0}]
    app = make_app(stats)
    tick(app)
    app._poll_sync_bulbs.assert_not_called()
    assert app.root.after.call_args.args[0] == LINK_CHECK_INTERVAL_MS


def test_five_second_silence_triggers_once_per_outage_and_again_after_return():
    stats = [{"ip": "10.0.0.2", "silent_for_s": LINK_EARLY_RECOVERY_S + 0.1}]
    app = make_app(stats)
    tick(app)
    app._poll_sync_bulbs.assert_called_once()
    app.root.after_cancel.assert_called_once_with("poll30")
    assert app._bulb_recovery_monitor_id is None
    stats[0]["silent_for_s"] = 9.0
    tick(app)
    tick(app)
    app._poll_sync_bulbs.assert_called_once()  # same outage
    stats[0]["silent_for_s"] = 0.05  # the bulb answered
    tick(app)
    stats[0]["silent_for_s"] = 6.0  # a second outage
    tick(app)
    assert app._poll_sync_bulbs.call_count == 2


def test_busy_recovery_defers_the_early_poll_without_losing_it():
    stats = [{"ip": "10.0.0.2", "silent_for_s": 7.0}]
    app = make_app(stats, token=object())
    tick(app)
    app._poll_sync_bulbs.assert_not_called()
    app._bulb_recovery_token = None
    tick(app)
    app._poll_sync_bulbs.assert_called_once()


def test_paused_or_missing_link_stats_never_trigger():
    app = make_app([{"ip": "10.0.0.2", "silent_for_s": None}])
    tick(app)
    # keep_awake off: the pool reports no link stats at all.
    app = make_app([])
    tick(app)
    app._poll_sync_bulbs.assert_not_called()
    app._screen_sync_transport = None
    tick(app)
    app._poll_sync_bulbs.assert_not_called()


def test_check_stops_when_sync_is_not_running():
    app = make_app([{"ip": "10.0.0.2", "silent_for_s": 30.0}])
    app.screen_sync = None
    tick(app)
    app._poll_sync_bulbs.assert_not_called()
    app.root.after.assert_not_called()
