import asyncio
import threading

import pytest

from src.core.async_runtime import BackgroundAsyncLoop


def test_shutdown_cancels_pending_work_and_runs_cleanup():
    runner = BackgroundAsyncLoop()
    entered, cleaned = threading.Event(), threading.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    future = runner.submit(work())
    assert entered.wait(2)
    runner.shutdown()
    assert cleaned.is_set()
    assert future.cancelled()
    assert not runner._thread.is_alive()


def test_rejected_coroutine_is_closed_after_shutdown():
    runner = BackgroundAsyncLoop()
    runner.shutdown()
    coroutine = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit(coroutine)
    assert coroutine.cr_frame is None
