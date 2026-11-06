"""Asyncio helpers for Windows-compatible bulb control."""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import Future, TimeoutError
from threading import Event, Lock, Thread, current_thread
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def configure_event_loop_policy() -> None:
    """Use the selector event loop on Windows for UDP-based bulb traffic."""
    if sys.platform != "win32" or not hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        return

    selector_policy = asyncio.WindowsSelectorEventLoopPolicy
    current_policy = asyncio.get_event_loop_policy()
    if not isinstance(current_policy, selector_policy):
        asyncio.set_event_loop_policy(selector_policy())


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from sync code with the correct loop policy."""
    configure_event_loop_policy()
    return asyncio.run(coro)


class BackgroundAsyncLoop:
    """Run a long-lived asyncio event loop in a background thread."""

    def __init__(self) -> None:
        configure_event_loop_policy()
        self._loop = asyncio.new_event_loop()
        self._started = Event()
        self._closed = False
        self._state_lock = Lock()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        if not self._started.wait(timeout=2.0):
            raise RuntimeError("Background asyncio loop failed to start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            # The owner thread drains cleanup before closing its loop. In
            # particular, never close it from a caller while it is still alive.
            self._loop.run_until_complete(self._cancel_pending_tasks())
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule a coroutine on the background loop."""
        with self._state_lock:
            if self._closed or self._loop.is_closed():
                coro.close()
                raise RuntimeError("Background asyncio loop is closed")
            try:
                return asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                coro.close()
                raise

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Schedule a coroutine and wait for its result."""
        if current_thread() is self._thread:
            coro.close()
            raise RuntimeError("Cannot block on the background loop from its own thread")
        future = self.submit(coro)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def _cancel_pending_tasks(self) -> None:
        current_task = asyncio.current_task()
        pending_tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current_task and not task.done()
        ]

        for task in pending_tasks:
            task.cancel()

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    def cancel_pending(self, timeout: float = 2.0) -> None:
        """Cancel outstanding tasks while keeping the event loop usable."""

        if self._closed or self._loop.is_closed() or not self._loop.is_running():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._cancel_pending_tasks(),
                self._loop,
            )
            future.result(timeout=timeout)
        except (RuntimeError, TimeoutError):
            pass

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop the background loop and cancel any remaining tasks."""
        with self._state_lock:
            if not self._closed:
                # Serialize rejection of new work with the final stop request.
                self._closed = True
                self._loop.call_soon_threadsafe(self._loop.stop)
        if current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        """Alias for shutdown()."""
        self.shutdown()
