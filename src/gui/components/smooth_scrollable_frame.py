"""Scrollable frame helpers for smoother Windows redraws."""

from __future__ import annotations

import sys

import customtkinter as ctk


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """CTkScrollableFrame with explicit repaint hooks for Windows scroll glitches."""

    def __init__(self, *args, **kwargs):
        self._repaint_after_id = None
        super().__init__(*args, **kwargs)

        if self._orientation == "vertical":
            self._scrollbar.configure(command=self._scroll_yview_and_repaint)
        else:
            self._scrollbar.configure(command=self._scroll_xview_and_repaint)

        self.bind("<Configure>", lambda _event: self._queue_canvas_repaint(), add="+")
        self._parent_canvas.bind("<Configure>", lambda _event: self._queue_canvas_repaint(), add="+")
        self._scrollbar.bind("<ButtonRelease-1>", lambda _event: self._queue_canvas_repaint(), add="+")

    def destroy(self):
        if self._repaint_after_id is not None:
            try:
                self.after_cancel(self._repaint_after_id)
            except Exception:
                pass
            self._repaint_after_id = None
        super().destroy()

    def _mouse_wheel_all(self, event):
        before = self._current_view()
        super()._mouse_wheel_all(event)
        if self._current_view() != before:
            self._queue_canvas_repaint()

    def _current_view(self) -> tuple[float, float]:
        return self._parent_canvas.yview() if self._orientation == "vertical" else self._parent_canvas.xview()

    def _scroll_yview_and_repaint(self, *args):
        self._parent_canvas.yview(*args)
        self._queue_canvas_repaint()

    def _scroll_xview_and_repaint(self, *args):
        self._parent_canvas.xview(*args)
        self._queue_canvas_repaint()

    def _queue_canvas_repaint(self):
        # A burst of wheel, resize and scrollbar events needs one repaint.
        # Keeping the already-scheduled callback also avoids starving it while
        # events keep arriving.
        if self._repaint_after_id is None:
            self._repaint_after_id = self.after_idle(self._force_canvas_repaint)

    def _force_canvas_repaint(self):
        self._repaint_after_id = None
        scroll_region = self._parent_canvas.bbox("all")
        current_region = self._parent_canvas.tk.splitlist(self._parent_canvas.cget("scrollregion"))
        if scroll_region and tuple(map(int, current_region)) != scroll_region:
            self._parent_canvas.configure(scrollregion=scroll_region)

        if sys.platform.startswith("win"):
            try:
                import ctypes

                user32 = ctypes.windll.user32
                # Let the normal Windows message loop paint these windows;
                # UpdateWindow and nested update_idletasks block the UI thread.
                user32.InvalidateRect(int(self._parent_canvas.winfo_id()), 0, True)
                user32.InvalidateRect(int(self._parent_frame.winfo_id()), 0, True)
            except Exception:
                pass
