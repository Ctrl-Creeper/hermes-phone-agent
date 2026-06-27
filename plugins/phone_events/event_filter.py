"""Event filtering, deduplication, and rate limiting.

Prevents event floods from overwhelming the agent. All phone events
pass through this filter before reaching the conversation loop.
"""

from __future__ import annotations

import hashlib
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set


@dataclass
class PhoneEvent:
    """A normalized event from the phone."""

    event_type: str       # notification, app_switch, ui_change, broadcast, crash
    package: str = ""
    title: str = ""
    body: str = ""
    timestamp: float = 0.0
    raw: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        content = f"{self.event_type}:{self.package}:{self.title}:{self.body}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class EventFilter:
    """Rate limiter + deduplicator + package allowlist."""

    def __init__(
        self,
        max_events_per_second: float = 2.0,
        dedup_window_seconds: float = 10.0,
        allowed_packages: Optional[Set[str]] = None,
    ):
        self._max_rate = max_events_per_second
        self._dedup_window = dedup_window_seconds
        self._allowed_packages = allowed_packages
        self._recent_keys: Dict[str, float] = {}
        self._last_event_time = 0.0
        self._event_count_window_start = 0.0
        self._event_count = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "EventFilter":
        rate = float(os.environ.get("PHONE_EVENTS_RATE_LIMIT", "2"))
        pkgs_str = os.environ.get("PHONE_EVENTS_ALLOWED_PACKAGES", "")
        allowed = set(p.strip() for p in pkgs_str.split(",") if p.strip()) or None
        return cls(max_events_per_second=rate, allowed_packages=allowed)

    def should_forward(self, event: PhoneEvent) -> bool:
        """Return True if the event should be forwarded to the agent."""
        with self._lock:
            now = time.monotonic()

            # Package allowlist
            if self._allowed_packages and event.package:
                if event.package not in self._allowed_packages:
                    return False

            # Rate limiting (sliding window)
            if now - self._event_count_window_start > 1.0:
                self._event_count = 0
                self._event_count_window_start = now
            self._event_count += 1
            if self._event_count > self._max_rate:
                return False

            # Deduplication
            key = event.dedup_key
            last_seen = self._recent_keys.get(key)
            if last_seen is not None and (now - last_seen) < self._dedup_window:
                return False
            self._recent_keys[key] = now

            # Prune old dedup entries
            cutoff = now - self._dedup_window * 2
            self._recent_keys = {
                k: v for k, v in self._recent_keys.items() if v > cutoff
            }

            return True
