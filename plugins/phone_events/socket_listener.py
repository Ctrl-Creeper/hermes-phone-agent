"""Helper APK socket listener — Tier 2 event source.

Receives structured JSON events from the on-device helper APK via
an ADB-forwarded TCP socket. The helper APK opens a server socket
on a high port; we forward it to localhost via `adb forward`.

Security: the helper APK sends a session token on connect. We validate
it before accepting events. The token is generated per session and
passed to the APK via `adb shell am broadcast`.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

from plugins.phone_events.event_filter import PhoneEvent

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 18765
_HELPER_PACKAGE = "com.hermes.phoneagent"
_TOKEN_LENGTH = 32


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
        self._server_socket: Optional[socket.socket] = None

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

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.is_available:
            logger.info(
                "Helper APK (%s) not installed — Tier 2 events disabled. "
                "Install it with: adb install helper-apk/releases/phone-agent-helper.apk",
                _HELPER_PACKAGE,
            )
            return

        self._session_token = secrets.token_hex(_TOKEN_LENGTH)
        self._setup_adb_forward()
        self._send_token_to_apk()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="helper-socket-listener",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.close()
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
        subprocess.run(
            self._adb_cmd(
                "forward", f"tcp:{self._port}", f"tcp:{self._port}",
            ),
            capture_output=True, timeout=10,
        )

    def _teardown_adb_forward(self) -> None:
        try:
            subprocess.run(
                self._adb_cmd("forward", "--remove", f"tcp:{self._port}"),
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def _send_token_to_apk(self) -> None:
        """Send session token to the helper APK via a broadcast intent."""
        subprocess.run(
            self._adb_cmd(
                "shell", "am", "broadcast",
                "-a", f"{_HELPER_PACKAGE}.SET_TOKEN",
                "-n", f"{_HELPER_PACKAGE}/.TokenReceiver",
                "--es", "token", self._session_token,
            ),
            capture_output=True, timeout=10,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_socket.settimeout(2.0)
                self._server_socket.bind(("127.0.0.1", self._port))
                self._server_socket.listen(1)
                logger.info("Helper socket listening on 127.0.0.1:%d", self._port)
                self._accept_loop()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.warning("socket listener error: %s", e)
            finally:
                if self._server_socket:
                    try:
                        self._server_socket.close()
                    except Exception:
                        pass
                    self._server_socket = None

            if not self._stop_event.is_set():
                self._stop_event.wait(3.0)

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                self._handle_connection(conn)
            except Exception as e:
                logger.warning("helper connection error: %s", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        buf = b""
        authenticated = False

        while not self._stop_event.is_set():
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
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

                # First message must be authentication.
                if not authenticated:
                    if msg.get("type") == "auth" and msg.get("token") == self._session_token:
                        authenticated = True
                        logger.info("helper APK authenticated")
                        continue
                    else:
                        logger.warning("helper APK auth failed — closing connection")
                        return

                event = self._parse_helper_event(msg)
                if event and self._on_event:
                    self._on_event(event)

    @staticmethod
    def _parse_helper_event(msg: dict) -> Optional[PhoneEvent]:
        event_type = msg.get("type", "")
        if event_type not in ("notification", "ui_change", "toast", "broadcast"):
            return None
        return PhoneEvent(
            event_type=event_type,
            package=msg.get("package", ""),
            title=msg.get("title", ""),
            body=msg.get("body", ""),
            timestamp=msg.get("timestamp", time.time()),
            meta={k: v for k, v in msg.items()
                  if k not in ("type", "package", "title", "body", "timestamp", "token")},
        )
