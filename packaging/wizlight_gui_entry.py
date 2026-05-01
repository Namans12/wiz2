"""Frozen Windows GUI entrypoint for WizLight."""

from src.core.async_runtime import configure_event_loop_policy


configure_event_loop_policy()

from src.gui.modern_app import main  # noqa: E402


if __name__ == "__main__":
    main()
