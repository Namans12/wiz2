"""Entry point for running GUI as a module."""

from ..core.async_runtime import configure_event_loop_policy
configure_event_loop_policy()

from .modern_app import main  # noqa: E402

if __name__ == "__main__":
    main()
