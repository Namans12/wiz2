"""Display repeatable colour changes for recording screen-to-light response.

This tool only draws on the screen. It does not discover or control lights.
Run with the sync app already active, then record the display and lights in
the same slow-motion video. Escape closes the window.
"""

from __future__ import annotations

import argparse
import time
import tkinter as tk

PATTERNS = (
    ("Black", "#000000", "#ffffff"),
    ("White", "#ffffff", "#000000"),
    ("Red", "#ff0000", "#ffffff"),
    ("Green", "#00ff00", "#000000"),
    ("Blue", "#0000ff", "#ffffff"),
    ("Warm white", "#ffe4bc", "#000000"),
    ("Cool white", "#d4e7ff", "#000000"),
    ("Mid gray", "#808080", "#ffffff"),
)


def pattern_at(elapsed: float, hold_seconds: float) -> tuple[int, str, str, str]:
    if hold_seconds <= 0:
        raise ValueError("hold time must be positive")
    step = int(max(0.0, elapsed) / hold_seconds)
    name, background, foreground = PATTERNS[step % len(PATTERNS)]
    return step, name, background, foreground


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold", type=float, default=2.0, help="Seconds per colour (default: 2)")
    parser.add_argument("--windowed", action="store_true", help="Use a window instead of full screen")
    args = parser.parse_args()
    if not 0.1 <= args.hold <= 60.0:
        parser.error("--hold must be between 0.1 and 60 seconds")

    root = tk.Tk()
    root.title("WizLight colour and response test")
    root.attributes("-fullscreen", not args.windowed)
    if args.windowed:
        root.geometry("960x540")
    root.bind("<Escape>", lambda _: root.destroy())
    label = tk.Label(root, font=("Arial", 22), justify="center")
    label.place(relx=0.5, rely=0.5, anchor="center")
    started = time.monotonic()

    def update() -> None:
        elapsed = time.monotonic() - started
        step, name, background, foreground = pattern_at(elapsed, args.hold)
        root.configure(bg=background)
        label.configure(bg=background, fg=foreground,
                        text=f"{name}  |  step {step}\n{elapsed:.2f} s\nEscape to close")
        root.after(16, update)

    update()
    root.mainloop()


if __name__ == "__main__":
    main()
