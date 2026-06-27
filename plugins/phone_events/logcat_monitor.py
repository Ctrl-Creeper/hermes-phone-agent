"""ADB logcat stream monitor — Tier 1 event source.

Runs `adb logcat` as a background subprocess and parses notification
and activity lifecycle events from the stream.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

from .event_filter import PhoneEvent

logger = logging.getLogger(__name__)

# Logcat patterns for events we care about.
_NOTIFICATION_PATTERN = re.compile(
    r"NotificationService.*enqueueNotificationInternal:\s*"
    r"pkg=(\S+)\s.*tag=\S+\s.*id=\d+"
)
_NOTIFICATION_CONTENT = re.compile(
    r"Notification\(.*?text=(.*?)\)"
)
_ACTIVITY_START = re.compile(
    r"ActivityManager.*START.*cmp=(\S+)/(\S+)"
)
_ACTIVITY_CRASH = re.compile(
    r"ActivityManager.*Process\s+(\S+).*has died"
)
_TOAST_PATTERN = re.compile(
    r"NotificationService.*Toast.*pkg=(\S+).*text=(.+)"
)


class LogcatMonitor:
    """Persistent `adb logcat` parser that emits PhoneEvents."""

    def __init__(
        self,
        serial: Optional[str] = None,
        on_event: Optional[Callable[[PhoneEvent], None]] = None,
    ):
        self._serial = serial or os.environ.get("ANDROID_SERIAL")
        self._on_event = on_event
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="logcat-monitor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        cmd = ["adb"]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend([
            "logcat",
            "-s",
            "NotificationService:I",
            "ActivityManager:I",
            "-v", "time",
        ])

        while not self._stop_event.is_set():
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                self._read_stream()
            except Exception as e:
                logger.warning("logcat monitor error: %s", e)
            finally:
                if self._process:
                    try:
                        self._process.terminate()
                    except Exception:
                        pass
                    self._process = None

            if not self._stop_event.is_set():
                logger.info("logcat stream ended, reconnecting in 3s…")
                self._stop_event.wait(3.0)

    def _read_stream(self) -> None:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            event = self._parse_line(line)
            if event and self._on_event:
                self._on_event(event)

    def _parse_line(self, line: str) -> Optional[PhoneEvent]:
        now = time.time()

        m = _NOTIFICATION_PATTERN.search(line)
        if m:
            package = m.group(1)
            content_match = _NOTIFICATION_CONTENT.search(line)
            body = content_match.group(1) if content_match else ""
            return PhoneEvent(
                event_type="notification",
                package=package,
                body=body,
                timestamp=now,
                raw=line[:500],
            )

        m = _ACTIVITY_START.search(line)
        if m:
            return PhoneEvent(
                event_type="app_switch",
                package=m.group(1),
                title=m.group(2),
                timestamp=now,
                raw=line[:500],
            )

        m = _ACTIVITY_CRASH.search(line)
        if m:
            return PhoneEvent(
                event_type="crash",
                package=m.group(1),
                timestamp=now,
                raw=line[:500],
            )

        m = _TOAST_PATTERN.search(line)
        if m:
            return PhoneEvent(
                event_type="toast",
                package=m.group(1),
                body=m.group(2),
                timestamp=now,
                raw=line[:500],
            )

        return None
