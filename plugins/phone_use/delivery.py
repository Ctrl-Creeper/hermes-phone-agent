"""Durable reply receipts for authenticated Hermes phone tasks.

Only hashes and delivery states are stored, never message bodies. A journal-wide
OS lock serializes writers and is released automatically after process death.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def state_directory() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    path = home / "phone" / "delivery"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class DeliveryJournal:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "receipts.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, state TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def receipt(self, identity: str, device: str, chat: str, text: str,
                quote_identity: str = ""):
        import fcntl
        parts = [identity, device, chat.strip(), text]
        if quote_identity:
            parts.append(quote_identity)
        key = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        # One journal-wide lock bounds filesystem growth and serializes crash
        # recovery. Phone operations are already serialized by the device queue.
        fd = os.open(self.directory / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield Receipt(self, key)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Receipt:
    def __init__(self, journal: DeliveryJournal, key: str):
        self.journal, self.key = journal, key

    @property
    def state(self) -> str:
        with self.journal.connect() as db:
            row = db.execute("SELECT state FROM receipts WHERE key = ?", (self.key,)).fetchone()
        return row[0] if row else "prepared"

    def record(self, state: str) -> None:
        if state not in {"prepared", "attempted", "confirmed", "uncertain"}:
            raise ValueError("invalid delivery state")
        with self.journal.connect() as db:
            db.execute("INSERT INTO receipts VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET state = excluded.state",
                       (self.key, state))
