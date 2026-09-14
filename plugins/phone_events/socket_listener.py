"""Helper APK socket listener — Tier 2 event source.

Receives structured JSON events from the on-device helper APK via
an ADB-forwarded TCP socket. The helper APK owns the server socket;
the host connects to the local end of `adb forward`.

Security: the host generates a per-session token, passes it to the APK via
an ADB-only protected service start, then authenticates its forwarded socket
connection with the same token before accepting events.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

from .event_filter import PhoneEvent

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 18765
_HELPER_PACKAGE = "com.hermes.phoneagent"
_TOKEN_LENGTH = 32
_AUTH_READY_TIMEOUT_SECONDS = 12.0


class SocketListener:
    """Listens for events from the helper APK via ADB-forwarded socket."""

    def __init__(
        self,
        serial: Optional[str] = None,
        port: Optional[int] = None,
        on_event: Optional[Callable[[PhoneEvent], None]] = None,
    ):
        self._serial = serial or os.environ.get("ANDROID_SERIAL")
        self._port = port or int(os.environ.get("PHONE_EVENTS_HELPER_PORT", str(_DEFAULT_PORT)))
        self._on_event = on_event
        self._session_token: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._authenticated_event = threading.Event()
        self._connection: Optional[socket.socket] = None

    @property
    def is_available(self) -> bool:
        """Check if the helper APK is installed on the device."""
        cmd = ["adb"]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(["shell", "pm", "list", "packages", _HELPER_PACKAGE])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return _HELPER_PACKAGE in result.stdout
        except Exception:
            return False

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated_event.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.is_available:
            logger.info(
                "Helper APK (%s) not installed — Tier 2 events disabled. "
                "Install Hermes Phone Agent v0.2.4 from: "
                "https://github.com/Ctrl-Creeper/hermes-phone-agent/"
                "releases/tag/v0.2.4",
                _HELPER_PACKAGE,
            )
            return

        self._session_token = secrets.token_hex(_TOKEN_LENGTH)
        self._setup_adb_forward()
        self._start_helper_service()

        self._stop_event.clear()
        self._authenticated_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="helper-socket-listener",
        )
        self._thread.start()
        if not self._authenticated_event.wait(_AUTH_READY_TIMEOUT_SECONDS):
            self.stop()
            raise RuntimeError(
                "helper socket did not authenticate before startup timeout"
            )

    def stop(self) -> None:
        self._stop_event.set()
        self._authenticated_event.clear()
        if self._connection:
            try:
                self._connection.close()
            except Exception:
                pass
        self._teardown_adb_forward()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _adb_cmd(self, *args: str) -> list:
        cmd = ["adb"]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(args)
        return cmd

    def _setup_adb_forward(self) -> None:
        result = subprocess.run(
            self._adb_cmd(
                "forward", f"tcp:{self._port}", f"tcp:{self._port}",
            ),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"adb forward failed: {(result.stderr or result.stdout).strip()}"
            )

    def _teardown_adb_forward(self) -> None:
        try:
            subprocess.run(
                self._adb_cmd("forward", "--remove", f"tcp:{self._port}"),
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def _start_helper_service(self) -> None:
        """Start the ADB-protected helper service with its session token."""
        result = subprocess.run(
            self._adb_cmd(
                "shell", "am", "start-foreground-service",
                "-n", f"{_HELPER_PACKAGE}/.EventSocketService",
                "--es", "token", self._session_token,
            ),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or "Starting service" not in result.stdout:
            raise RuntimeError(
                f"helper service start failed: {(result.stderr or result.stdout).strip()}"
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # `adb forward` owns the local listening port. The host is
                # therefore the client, and the APK is the server.
                connection = socket.create_connection(
                    ("127.0.0.1", self._port), timeout=5.0
                )
                self._connection = connection
                connection.settimeout(2.0)
                nonce = secrets.token_hex(_TOKEN_LENGTH)
                challenge = json.dumps({
                    "type": "auth_challenge", "nonce": nonce,
                }).encode("utf-8") + b"\n"
                connection.sendall(challenge)
                self._read_events(connection, nonce=nonce)
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.warning("socket listener error: %s", e)
                    try:
                        # The emulator or helper process may have restarted.
                        # Recreate the forward and start the ADB-protected
                        # service with this listener's existing session token.
                        self._setup_adb_forward()
                        self._start_helper_service()
                    except Exception as recovery_error:
                        logger.warning(
                            "helper socket recovery failed: %s", recovery_error,
                        )
            finally:
                self._authenticated_event.clear()
                if self._connection:
                    try:
                        self._connection.close()
                    except Exception:
                        pass
                    self._connection = None

            if not self._stop_event.is_set():
                self._stop_event.wait(3.0)

    def _read_events(self, conn: socket.socket, nonce: Optional[str] = None) -> None:
        buf = b""
        expected_proof = None
        if nonce and self._session_token:
            expected_proof = hmac.new(
                self._session_token.encode("utf-8"),
                f"helper:{nonce}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        host_proved_identity = False

        while not self._stop_event.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                logger.warning("helper socket closed by peer")
                break
            buf += data

            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("invalid JSON from helper: %s", line_str[:100])
                    continue

                if msg.get("type") == "auth_proof":
                    proof = str(msg.get("proof") or "")
                    helper_nonce = str(msg.get("nonce") or "")
                    if expected_proof is None or not hmac.compare_digest(
                        proof, expected_proof,
                    ):
                        raise PermissionError("invalid helper authentication proof")
                    if not 32 <= len(helper_nonce) <= 256 or not self._session_token:
                        raise PermissionError("invalid helper authentication nonce")
                    response = hmac.new(
                        self._session_token.encode("utf-8"),
                        f"host:{helper_nonce}".encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    conn.sendall(json.dumps({
                        "type": "auth_response", "proof": response,
                    }).encode("utf-8") + b"\n")
                    host_proved_identity = True
                    continue
                if msg.get("type") == "auth_ok":
                    if not host_proved_identity:
                        raise PermissionError("helper accepted an unproved host")
                    self._authenticated_event.set()
                    logger.info(
                        "Authenticated helper socket on 127.0.0.1:%d", self._port,
                    )
                    continue
                if not self._authenticated_event.is_set():
                    logger.warning("event received before helper authentication")
                    continue

                event = self._parse_helper_event(msg)
                if event and self._on_event:
                    logger.info(
                        "Received helper event: type=%s package=%s",
                        event.event_type, event.package,
                    )
                    self._on_event(event)

    @staticmethod
    def _parse_helper_event(msg: dict) -> Optional[PhoneEvent]:
        event_type = msg.get("type", "")
        if event_type not in ("notification", "ui_change", "toast", "broadcast"):
            return None
        meta = {
            k: v for k, v in msg.items()
            if k not in ("type", "package", "title", "body", "timestamp", "token")
        }
        meta["_transport"] = "helper_socket"
        return PhoneEvent(
            event_type=event_type,
            package=msg.get("package", ""),
            title=msg.get("title", ""),
            body=msg.get("body", ""),
            timestamp=msg.get("timestamp", time.time()),
            meta=meta,
        )
