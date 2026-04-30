"""Coalescing async dispatcher for real-time screen-sync updates."""

from __future__ import annotations

from concurrent.futures import Future
from threading import RLock
from typing import Callable, Coroutine, Optional, TypeVar

T = TypeVar("T")


class CoalescingAsyncDispatcher:
    """Keep at most one async update in flight and one newest pending update."""

    def __init__(
        self,
        submit: Callable[[Coroutine], Future],
        apply_update: Callable[[T], Coroutine],
    ) -> None:
        self._submit = submit
        self._apply_update = apply_update
        self._lock = RLock()
        self._future: Optional[Future] = None
        self._pending: Optional[T] = None
        self._closed = False

    def submit(self, update: T) -> None:
        """Submit an update, replacing any older pending update."""

        with self._lock:
            if self._closed:
                return
            if self._future is not None and not self._future.done():
                self._pending = update
                return
            self._start_locked(update)

    def _start_locked(self, update: T) -> None:
        future = self._submit(self._apply_update(update))
        self._future = future
        future.add_done_callback(self._on_done)

    def _on_done(self, _future: Future) -> None:
        with self._lock:
            if self._closed:
                return
            pending = self._pending
            self._pending = None
            if pending is None:
                self._future = None
                return
            self._start_locked(pending)

    def close(self) -> None:
        """Stop accepting updates and cancel any queued work."""

        with self._lock:
            self._closed = True
            self._pending = None
            if self._future is not None and not self._future.done():
                self._future.cancel()
            self._future = None
