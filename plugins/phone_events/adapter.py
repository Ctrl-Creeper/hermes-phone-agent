"""Phone Events platform adapter for Hermes gateway.

Subclasses BasePlatformAdapter to inject phone events (notifications,
app switches, UI changes) into the Hermes conversation loop, exactly
like Telegram/Discord adapters inject chat messages.

All text from the phone is wrapped as [PHONE_DATA] to prevent prompt
injection — the system prompt instructs the model to treat it as
untrusted data, never as instructions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Lazy imports — the gateway framework may not be available during plugin
# discovery. Import at class/function level.

_DATA_PREFIX = "[PHONE_DATA]"


def _wrap_phone_data(text: str) -> str:
    """Wrap phone-originated text to mark it as untrusted data."""
    return f"{_DATA_PREFIX} {text}"


def check_phone_events_requirements() -> bool:
    """Check if ADB is available (minimum requirement)."""
    import shutil
    return shutil.which("adb") is not None


class PhoneEventAdapter:
    """Platform adapter that monitors phone events and injects them.

    This adapter is intentionally simplified compared to full chat platform
    adapters (Telegram, Discord) because:
    - Events are one-way: phone → agent (no "send message back to phone")
    - The agent responds via the phone_use tool, not by "replying"
    - There's no concept of "chat rooms" or "users" on the phone side

    The adapter bridges events from LogcatMonitor (Tier 1) and
    SocketListener (Tier 2) into hermes gateway's message dispatch.
    """

    def __init__(self, config: Any = None):
        self._config = config
        self._connected = False
        self._logcat_monitor = None
        self._socket_listener = None
        self._event_filter = None
        self._message_callback = None
        self._redact_otp = os.environ.get(
            "PHONE_EVENTS_REDACT_OTP", "true"
        ).lower() in ("true", "1", "yes")

    def set_message_callback(self, callback) -> None:
        """Set the callback for injecting events into the gateway."""
        self._message_callback = callback

    async def connect(self) -> bool:
        """Start event monitors."""
        from plugins.phone_events.event_filter import EventFilter
        from plugins.phone_events.logcat_monitor import LogcatMonitor
        from plugins.phone_events.socket_listener import SocketListener

        self._event_filter = EventFilter.from_env()

        self._logcat_monitor = LogcatMonitor(on_event=self._on_raw_event)
        self._logcat_monitor.start()

        self._socket_listener = SocketListener(on_event=self._on_raw_event)
        self._socket_listener.start()

        self._connected = True
        logger.info("Phone event monitors started")
        return True

    async def disconnect(self) -> None:
        """Stop all monitors."""
        if self._logcat_monitor:
            self._logcat_monitor.stop()
        if self._socket_listener:
            self._socket_listener.stop()
        self._connected = False
        logger.info("Phone event monitors stopped")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _on_raw_event(self, event) -> None:
        """Called from monitor threads. Filter, redact, then dispatch."""
        if not self._event_filter or not self._event_filter.should_forward(event):
            return

        from plugins.phone_events.redact import (
            redact_sensitive,
            truncate_notification_body,
        )

        if event.title:
            event.title = redact_sensitive(event.title, enable_otp=self._redact_otp)
        if event.body:
            event.body = redact_sensitive(event.body, enable_otp=self._redact_otp)
            event.body = truncate_notification_body(event.body)

        formatted = self._format_event(event)
        if formatted and self._message_callback:
            try:
                self._message_callback(formatted)
            except Exception as e:
                logger.warning("Failed to dispatch phone event: %s", e)

    @staticmethod
    def _format_event(event) -> Optional[str]:
        """Format a PhoneEvent as a text message for the agent.

        All content is wrapped with [PHONE_DATA] to clearly mark it as
        untrusted data from the phone, not user instructions.
        """
        if event.event_type == "notification":
            parts = [f"📱 Phone notification from {event.package}"]
            if event.title:
                parts.append(f"  Title: {_wrap_phone_data(event.title)}")
            if event.body:
                parts.append(f"  Body: {_wrap_phone_data(event.body)}")
            return "\n".join(parts)

        if event.event_type == "app_switch":
            return (
                f"📱 App switched to {event.package}"
                + (f" ({_wrap_phone_data(event.title)})" if event.title else "")
            )

        if event.event_type == "crash":
            return f"📱 App crashed: {event.package}"

        if event.event_type == "toast":
            return (
                f"📱 Toast from {event.package}: "
                f"{_wrap_phone_data(event.body)}"
            )

        if event.event_type == "ui_change":
            return (
                f"📱 UI changed in {event.package}: "
                f"{_wrap_phone_data(event.body)}"
            )

        if event.event_type == "broadcast":
            return (
                f"📱 System broadcast: {_wrap_phone_data(event.title)}"
                + (f" — {_wrap_phone_data(event.body)}" if event.body else "")
            )

        return None
