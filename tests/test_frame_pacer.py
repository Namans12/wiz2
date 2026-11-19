import pytest

from src.core import frame_pacer
from src.features import screen_sync_v2


def test_pacer_uses_and_closes_timer_once(monkeypatch):
    class Timer:
        waits = []
        closed = 0

        def wait(self, seconds):
            self.waits.append(seconds)
            return True

        def close(self):
            self.closed += 1

    timer = Timer()
    monkeypatch.setattr(frame_pacer, "_make_timer", lambda: timer)
    pacer = frame_pacer.FramePacer()
    pacer.sleep(0.01)
    pacer.close()
    pacer.close()
    assert timer.waits == [0.01]
    assert timer.closed == 1


def test_failed_timer_falls_back_for_remaining_wait(monkeypatch):
    class Timer:
        closed = False

        def wait(self, seconds):
            return False

        def close(self):
            self.closed = True

    timer = Timer()
    sleeps = []
    clock = iter([1.0, 1.004])
    monkeypatch.setattr(frame_pacer, "_make_timer", lambda: timer)
    monkeypatch.setattr(frame_pacer.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(frame_pacer.time, "sleep", sleeps.append)
    pacer = frame_pacer.FramePacer()
    pacer.sleep(0.01)
    assert timer.closed
    assert sleeps == pytest.approx([0.006])


def test_unavailable_windows_timer_uses_normal_sleep(monkeypatch):
    def unavailable():
        raise OSError("unsupported")

    sleeps = []
    monkeypatch.setattr(frame_pacer.sys, "platform", "win32")
    monkeypatch.setattr(frame_pacer, "_WindowsHighResolutionTimer", unavailable)
    monkeypatch.setattr(frame_pacer.time, "sleep", sleeps.append)
    pacer = frame_pacer.FramePacer()
    pacer.sleep(0.01)
    pacer.close()
    assert sleeps == [0.01]


def test_sync_loop_releases_pacer_after_worker_error(monkeypatch):
    class Pacer:
        closed = False

        def close(self):
            self.closed = True

    pacer = Pacer()
    monkeypatch.setattr(screen_sync_v2, "FramePacer", lambda: pacer)
    engine = screen_sync_v2.OptimizedScreenSync(lambda colors: None)

    def fail():
        raise RuntimeError("capture failed")

    monkeypatch.setattr(engine, "_sync_loop_inner", fail)
    with pytest.raises(RuntimeError, match="capture failed"):
        engine._sync_loop()
    assert pacer.closed
    assert engine._frame_pacer is None
