"""Phone Events platform plugin for Hermes Agent.

Monitors a virtual Android phone for events (notifications, app switches,
UI changes, crashes) and injects them into the Hermes conversation loop.

Install: hermes plugins install <repo>/plugins/phone_events
"""

from .adapter import (
    PhoneEventAdapter,
    check_phone_events_requirements,
)


def register(ctx) -> None:
    ctx.register_platform(
        name="phone_events",
        label="Phone Events",
        adapter_factory=lambda cfg: PhoneEventAdapter(cfg),
        check_fn=check_phone_events_requirements,
        required_env=[],
        install_hint="Requires adb on PATH and an Android emulator running.",
    )
