"""Hybrid ADB + Appium backend.

Uses ADB for fast operations (screenshot, tap, swipe, keyevent, app management)
and Appium for cases where ADB falls short:
  - WebView context switching and interaction
  - Reliable Unicode text input (ADB input text only handles ASCII)
  - Fallback UI hierarchy when uiautomator dump returns null root node

The Appium server is auto-started as a subprocess when first needed and
shut down on stop(). If Appium is not installed, falls back to pure ADB
with a warning.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .adb_backend import AdbBackend
from .backend import (
    ActionResult,
    CaptureResult,
    DeviceInfo,
    PhoneBackend,
    UIElement,
)
from .sanitize import validate_text_input

logger = logging.getLogger(__name__)

_ASCII_ONLY = re.compile(r"^[\x20-\x7E]+$")


class HybridBackend(PhoneBackend):
    """ADB-first backend that escalates to Appium when needed."""

    def __init__(self, serial: Optional[str] = None):
        self._serial = serial or os.environ.get("ANDROID_SERIAL")
        self._adb = AdbBackend(serial=self._serial)
        self._appium_driver = None
        self._appium_lock = threading.Lock()
        self._appium_available: Optional[bool] = None
        self._started = False

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        self._adb.start()
        self._started = True
        self._check_appium_availability()

    def stop(self) -> None:
        self._teardown_appium()
        self._adb.stop()
        self._started = False

    def is_available(self) -> bool:
        return self._adb.is_available()

    def device_info(self) -> DeviceInfo:
        return self._adb.device_info()

    # ── Capture (ADB primary, Appium fallback) ─────────────────────

    def capture(self, mode: str = "som") -> CaptureResult:
        result = self._adb.capture(mode=mode)
        if mode in ("som", "hierarchy") and not result.elements:
            appium_elements = self._appium_get_hierarchy()
            if appium_elements:
                result.elements = appium_elements
                self._adb._last_elements = appium_elements
        return result

    # ── Touch actions (always ADB — fast and reliable) ─────────────

    def tap(self, *, element: Optional[int] = None,
            x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
        return self._adb.tap(element=element, x=x, y=y)

    def long_press(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None,
                   duration_ms: int = 1000) -> ActionResult:
        return self._adb.long_press(element=element, x=x, y=y, duration_ms=duration_ms)

    def double_tap(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
        return self._adb.double_tap(element=element, x=x, y=y)

    def swipe(self, *, direction: Optional[str] = None,
              from_xy: Optional[Tuple[int, int]] = None,
              to_xy: Optional[Tuple[int, int]] = None,
              duration_ms: int = 300,
              element: Optional[int] = None) -> ActionResult:
        return self._adb.swipe(
            direction=direction, from_xy=from_xy, to_xy=to_xy,
            duration_ms=duration_ms, element=element,
        )

    # ── Text input (ADB for ASCII, Appium for Unicode) ─────────────

    def type_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        validate_text_input(text)
        if _ASCII_ONLY.match(text):
            return self._adb.type_text(text, element=element)
        return self._appium_type_text(text, element=element)

    def clear_text(self, element: Optional[int] = None) -> ActionResult:
        return self._adb.clear_text(element=element)

    def set_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        validate_text_input(text)
        self.clear_text(element=element)
        time.sleep(0.1)
        if _ASCII_ONLY.match(text):
            return self._adb.type_text(text, element=None)
        return self._appium_type_text(text, element=None)

    # ── Keyevent (ADB) ─────────────────────────────────────────────

    def keyevent(self, keycode: str) -> ActionResult:
        return self._adb.keyevent(keycode)

    # ── App management (ADB) ───────────────────────────────────────

    def launch_app(self, package: str, activity: Optional[str] = None) -> ActionResult:
        return self._adb.launch_app(package, activity=activity)

    def stop_app(self, package: str) -> ActionResult:
        return self._adb.stop_app(package)

    def list_apps(self, installed_only: bool = True) -> List[Dict[str, Any]]:
        return self._adb.list_apps(installed_only=installed_only)

    def current_app(self) -> Dict[str, str]:
        return self._adb.current_app()

    def install_apk(self, apk_path: str) -> ActionResult:
        return self._adb.install_apk(apk_path)

    # ── Shell (ADB) ────────────────────────────────────────────────

    def shell(self, command: str) -> ActionResult:
        return self._adb.shell(command)

    # ── Appium internals ───────────────────────────────────────────

    def _check_appium_availability(self) -> None:
        from .appium_manager import appium_installed, appium_python_client_available

        if not appium_installed():
            self._appium_available = False
            logger.info(
                "Appium not found — running in pure ADB mode. "
                "Install for Unicode/WebView support: npm install -g appium"
            )
            return

        if not appium_python_client_available():
            self._appium_available = False
            logger.info(
                "appium-python-client not installed — Appium features disabled. "
                "Install: pip install Appium-Python-Client"
            )
            return

        self._appium_available = True
        logger.info("Appium available — will auto-start when needed")

    def _ensure_appium_driver(self):
        """Lazy-init the Appium driver, starting the server if needed."""
        with self._appium_lock:
            if self._appium_driver is not None:
                try:
                    self._appium_driver.session_id
                    return self._appium_driver
                except Exception:
                    self._appium_driver = None

            if not self._appium_available:
                return None

            from .appium_manager import get_appium_server

            server = get_appium_server()
            if not server.ensure_running():
                logger.warning("Could not start Appium server")
                self._appium_available = False
                return None

            try:
                from appium import webdriver as appium_webdriver
                from appium.options.android import UiAutomator2Options

                options = UiAutomator2Options()
                options.platform_name = "Android"
                options.no_reset = True
                options.auto_grant_permissions = True
                if self._serial:
                    options.udid = self._serial

                self._appium_driver = appium_webdriver.Remote(
                    command_executor=server.url,
                    options=options,
                )
                logger.info("Appium driver session created")
                return self._appium_driver
            except Exception as e:
                logger.warning("Failed to create Appium session: %s", e)
                self._appium_available = False
                return None

    def _teardown_appium(self) -> None:
        with self._appium_lock:
            if self._appium_driver is not None:
                try:
                    self._appium_driver.quit()
                except Exception:
                    pass
                self._appium_driver = None

        from .appium_manager import get_appium_server
        try:
            get_appium_server().stop()
        except Exception:
            pass

    def _appium_type_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        """Type Unicode text via Appium. Falls back to ADB if Appium unavailable."""
        if element is not None:
            self._adb.tap(element=element)
            time.sleep(0.3)

        driver = self._ensure_appium_driver()
        if driver is None:
            logger.info("Appium unavailable for Unicode input, falling back to ADB")
            return self._adb.type_text(text, element=None)

        try:
            focused = driver.switch_to.active_element
            if focused:
                focused.send_keys(text)
                return ActionResult(
                    ok=True, action="type",
                    message=f"typed {len(text)} chars (Appium/Unicode)",
                )
        except Exception as e:
            logger.warning("Appium send_keys failed: %s — falling back to ADB", e)

        return self._adb.type_text(text, element=None)

    def _appium_get_hierarchy(self) -> List[UIElement]:
        """Get UI hierarchy via Appium when ADB uiautomator dump fails."""
        driver = self._ensure_appium_driver()
        if driver is None:
            return []

        try:
            source = driver.page_source
            if source:
                return self._adb._parse_hierarchy_xml(source)
        except Exception as e:
            logger.warning("Appium page_source failed: %s", e)

        return []
