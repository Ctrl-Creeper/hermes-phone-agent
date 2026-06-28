"""Appium server lifecycle management.

Auto-starts an Appium server as a subprocess when first needed,
and shuts it down when the backend stops. If Appium is not installed,
all methods degrade gracefully — the hybrid backend falls back to ADB.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 4723
_STARTUP_TIMEOUT = 30
_HEALTH_PATH = "/status"


def appium_installed() -> bool:
    return shutil.which("appium") is not None


def appium_python_client_available() -> bool:
    try:
        import appium  # noqa: F401
        return True
    except ImportError:
        return False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class AppiumServer:
    """Manages an Appium server subprocess."""

    def __init__(self, port: int = _DEFAULT_PORT):
        self.port = port
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            return _port_in_use(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def ensure_running(self) -> bool:
        """Start the server if not already running. Returns True if ready."""
        with self._lock:
            if self.is_running:
                return True

            if not appium_installed():
                logger.info(
                    "Appium not installed — hybrid features disabled. "
                    "Install: npm install -g appium"
                )
                return False

            return self._start_server()

    def _start_server(self) -> bool:
        """Start Appium server as a subprocess."""
        logger.info("Starting Appium server on port %d...", self.port)

        cmd = [
            "appium",
            "--port", str(self.port),
            "--address", "127.0.0.1",
            "--log-level", "warn",
            "--relaxed-security",
        ]

        uiautomator2_installed = self._check_driver("uiautomator2")
        if not uiautomator2_installed:
            logger.info("Installing Appium UiAutomator2 driver...")
            install_result = subprocess.run(
                ["appium", "driver", "install", "uiautomator2"],
                capture_output=True, text=True, timeout=120,
            )
            if install_result.returncode != 0:
                logger.error(
                    "Failed to install UiAutomator2 driver: %s",
                    install_result.stderr,
                )
                return False

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            atexit.register(self.stop)
        except FileNotFoundError:
            logger.error("appium binary not found")
            return False
        except Exception as e:
            logger.error("Failed to start Appium: %s", e)
            return False

        if not self._wait_for_ready():
            self.stop()
            return False

        self._started = True
        logger.info("Appium server ready on %s", self.url)
        return True

    def _wait_for_ready(self) -> bool:
        """Poll the Appium status endpoint until it responds."""
        import urllib.request
        import urllib.error

        deadline = time.monotonic() + _STARTUP_TIMEOUT
        url = f"{self.url}{_HEALTH_PATH}"

        while time.monotonic() < deadline:
            if self._process and self._process.poll() is not None:
                stderr = self._process.stderr.read() if self._process.stderr else ""
                logger.error("Appium exited during startup: %s", stderr[:500])
                return False
            try:
                resp = urllib.request.urlopen(url, timeout=2)
                if resp.status == 200:
                    return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)

        logger.error("Appium server did not become ready within %ds", _STARTUP_TIMEOUT)
        return False

    @staticmethod
    def _check_driver(driver_name: str) -> bool:
        try:
            result = subprocess.run(
                ["appium", "driver", "list", "--installed", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            return driver_name in result.stdout
        except Exception:
            return False

    def stop(self) -> None:
        with self._lock:
            if self._process is not None:
                logger.info("Stopping Appium server...")
                try:
                    self._process.terminate()
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                except Exception:
                    pass
                self._process = None
            self._started = False


_server_instance: Optional[AppiumServer] = None
_server_lock = threading.Lock()


def get_appium_server(port: Optional[int] = None) -> AppiumServer:
    """Get or create the singleton Appium server."""
    global _server_instance
    with _server_lock:
        if _server_instance is None:
            p = port or int(os.environ.get("APPIUM_PORT", str(_DEFAULT_PORT)))
            _server_instance = AppiumServer(port=p)
        return _server_instance
