"""Persistent, profile-scoped admission gate for Hermes phone operations."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_STATE_LOCK = threading.RLock()


class PhonePaused(BaseException):
    """Cancellation that composite workflows must not swallow as an action error."""


def _default_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "phone-pause.json"
    except ImportError:
        return Path.home() / ".hermes" / "phone-pause.json"


class PhonePauseGate:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _default_path()

    def snapshot(self) -> tuple[bool, int]:
        with _STATE_LOCK:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (not isinstance(data, dict) or type(data.get("paused")) is not bool
                        or type(data.get("generation")) is not int
                        or data["generation"] < 0):
                    raise ValueError("invalid phone pause state")
                return data["paused"], data["generation"]
            except FileNotFoundError:
                return False, 0
            except (OSError, ValueError, TypeError):
                logger.exception("Phone pause state unreadable; failing closed: %s", self.path)
                return True, 0

    def _transition(self, paused: bool) -> tuple[bool, int]:
        with _STATE_LOCK:
            previous, generation = self.snapshot()
            if previous == paused and self.path.is_file():
                # Repair malformed data even when its fail-closed value is paused.
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if data == {"paused": paused, "generation": generation}:
                        return paused, generation
                except (OSError, ValueError):
                    pass
            elif previous == paused:
                return paused, generation
            generation += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".phone-pause-", delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    os.chmod(temp_path, 0o600)
                    json.dump({"paused": paused, "generation": generation}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            return paused, generation

    def pause(self) -> tuple[bool, int]:
        return self._transition(True)

    def resume(self) -> tuple[bool, int]:
        return self._transition(False)

    def ensure_active(self, generation: Optional[int] = None) -> int:
        paused, current = self.snapshot()
        if paused or (generation is not None and generation != current):
            raise PhonePaused("Phone control is paused or this phone turn was cancelled")
        return current


pause_gate = PhonePauseGate()
