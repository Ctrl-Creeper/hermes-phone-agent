"""ADB-based backend for phone_use.

All subprocess calls use argument lists (never shell=True, never f-string
interpolation into shell commands). Every user-supplied string is validated
through sanitize.py before reaching subprocess.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .backend import (
    ActionResult,
    CaptureResult,
    DeviceInfo,
    PhoneBackend,
    UIElement,
)
from .sanitize import (
    validate_activity_name,
    validate_apk_path,
    validate_coordinate,
    validate_keycode,
    validate_package_name,
    validate_shell_command,
    validate_text_input,
)

logger = logging.getLogger(__name__)

_REMOTE_SCREENSHOT = "/data/local/tmp/hermes_screen.png"
_REMOTE_UIDUMP = "/data/local/tmp/hermes_uidump.xml"


def adb_available() -> bool:
    return shutil.which("adb") is not None


class AdbBackend(PhoneBackend):

    def __init__(self, serial: Optional[str] = None):
        self._serial = serial or os.environ.get("ANDROID_SERIAL")
        self._started = False
        self._device_info: Optional[DeviceInfo] = None
        self._last_elements: List[UIElement] = []

    # ── Subprocess helpers (argument-list only, never shell=True) ───

    def _adb_cmd(self, *args: str) -> List[str]:
        cmd = ["adb"]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(args)
        return cmd

    def _adb(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._adb_cmd(*args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _adb_shell(self, *shell_args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """Run an adb shell command with arguments as a list (not concatenated)."""
        return subprocess.run(
            self._adb_cmd("shell", *shell_args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if not adb_available():
            raise RuntimeError(
                "adb not found on PATH. Install Android SDK platform-tools."
            )
        result = self._adb("devices")
        if result.returncode != 0:
            raise RuntimeError(f"adb devices failed: {result.stderr}")
        lines = [
            l for l in result.stdout.strip().splitlines()[1:]
            if l.strip() and "\tdevice" in l
        ]
        if not lines:
            raise RuntimeError(
                "No Android device/emulator connected. Start one via "
                "Android Studio or `emulator -avd <name>`."
            )
        if not self._serial:
            self._serial = lines[0].split("\t")[0]
        self._started = True
        self._device_info = self._fetch_device_info()

    def stop(self) -> None:
        self._started = False

    def is_available(self) -> bool:
        if not adb_available():
            return False
        try:
            result = self._adb("get-state", timeout=5)
            return result.returncode == 0 and "device" in result.stdout
        except Exception:
            return False

    def device_info(self) -> DeviceInfo:
        if self._device_info is None:
            self._device_info = self._fetch_device_info()
        return self._device_info

    def _fetch_device_info(self) -> DeviceInfo:
        def prop(key: str) -> str:
            r = self._adb_shell("getprop", key)
            return r.stdout.strip() if r.returncode == 0 else ""

        model = prop("ro.product.model")
        version = prop("ro.build.version.release")
        sdk = prop("ro.build.version.sdk")
        serial = self._serial or "unknown"

        w, h, density = 0, 0, 0
        wm = self._adb_shell("wm", "size")
        if wm.returncode == 0:
            m = re.search(r"(\d+)x(\d+)", wm.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
        dm = self._adb_shell("wm", "density")
        if dm.returncode == 0:
            m = re.search(r"(\d+)", dm.stdout)
            if m:
                density = int(m.group(1))

        is_emu = bool(prop("ro.kernel.qemu")) or "emulator" in serial.lower()
        return DeviceInfo(
            serial=serial, model=model, android_version=version,
            sdk_version=int(sdk) if sdk.isdigit() else 0,
            screen_width=w, screen_height=h, density=density,
            is_emulator=is_emu,
        )

    # ── Capture ─────────────────────────────────────────────────────

    def capture(self, mode: str = "som") -> CaptureResult:
        info = self.device_info()
        png_b64 = None
        elements: List[UIElement] = []

        if mode in ("som", "screenshot"):
            png_b64 = self._take_screenshot()

        if mode in ("som", "hierarchy"):
            elements = self._dump_ui_hierarchy()
            self._last_elements = elements

        fg = self._get_foreground_app()
        return CaptureResult(
            mode=mode, width=info.screen_width, height=info.screen_height,
            png_b64=png_b64, elements=elements,
            current_package=fg.get("package", ""),
            current_activity=fg.get("activity", ""),
            png_bytes_len=len(base64.b64decode(png_b64)) if png_b64 else 0,
        )

    def _take_screenshot(self) -> Optional[str]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            local_path = f.name
        try:
            self._adb_shell("screencap", "-p", _REMOTE_SCREENSHOT)
            result = self._adb("pull", _REMOTE_SCREENSHOT, local_path)
            if result.returncode != 0:
                logger.warning("screenshot pull failed: %s", result.stderr)
                return None
            self._adb_shell("rm", _REMOTE_SCREENSHOT)
            with open(local_path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            logger.warning("screenshot failed: %s", e)
            return None
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    def _dump_ui_hierarchy(self) -> List[UIElement]:
        self._adb_shell("uiautomator", "dump", _REMOTE_UIDUMP)
        result = self._adb_shell("cat", _REMOTE_UIDUMP)
        if result.returncode != 0:
            logger.warning("uiautomator dump failed: %s", result.stderr)
            return []
        self._adb_shell("rm", _REMOTE_UIDUMP)
        return self._parse_hierarchy_xml(result.stdout)

    def _parse_hierarchy_xml(self, xml_str: str) -> List[UIElement]:
        elements: List[UIElement] = []
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as e:
            logger.warning("UI hierarchy parse failed: %s", e)
            return []

        idx = 0
        for node in root.iter("node"):
            idx += 1
            bounds = self._parse_bounds(node.get("bounds", "[0,0][0,0]"))
            elements.append(UIElement(
                index=idx,
                class_name=node.get("class", ""),
                resource_id=node.get("resource-id", ""),
                text=node.get("text", ""),
                content_desc=node.get("content-desc", ""),
                bounds=bounds,
                package=node.get("package", ""),
                clickable=node.get("clickable", "false") == "true",
                scrollable=node.get("scrollable", "false") == "true",
                focusable=node.get("focusable", "false") == "true",
                enabled=node.get("enabled", "true") == "true",
                checked=(
                    node.get("checked") == "true"
                    if node.get("checked") is not None else None
                ),
            ))
        return elements

    @staticmethod
    def _parse_bounds(bounds_str: str) -> Tuple[int, int, int, int]:
        m = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
        if len(m) == 2:
            return (int(m[0][0]), int(m[0][1]), int(m[1][0]), int(m[1][1]))
        return (0, 0, 0, 0)

    def _get_foreground_app(self) -> Dict[str, str]:
        result = self._adb_shell(
            "dumpsys", "activity", "activities"
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "mResumedActivity" in line:
                    m = re.search(r"(\S+)/(\S+)", line)
                    if m:
                        return {"package": m.group(1), "activity": m.group(2)}
        return {"package": "", "activity": ""}

    # ── Element resolution ──────────────────────────────────────────

    def _resolve_target(
        self, element: Optional[int], x: Optional[int], y: Optional[int],
    ) -> Tuple[int, int]:
        if element is not None:
            for e in self._last_elements:
                if e.index == element:
                    return e.center()
            raise ValueError(f"element #{element} not found in last capture")
        if x is not None and y is not None:
            info = self.device_info()
            validate_coordinate(x, y, info.screen_width, info.screen_height)
            return x, y
        raise ValueError("provide either 'element' or 'coordinate' [x, y]")

    # ── Touch actions ───────────────────────────────────────────────

    def tap(self, *, element: Optional[int] = None,
            x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
        tx, ty = self._resolve_target(element, x, y)
        result = self._adb_shell("input", "tap", str(tx), str(ty))
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="tap", message=f"tap ({tx}, {ty})")

    def long_press(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None,
                   duration_ms: int = 1000) -> ActionResult:
        tx, ty = self._resolve_target(element, x, y)
        duration_ms = max(100, min(duration_ms, 5000))
        result = self._adb_shell(
            "input", "swipe", str(tx), str(ty), str(tx), str(ty), str(duration_ms),
        )
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="long_press",
                            message=f"long_press ({tx}, {ty}) {duration_ms}ms")

    def double_tap(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
        tx, ty = self._resolve_target(element, x, y)
        self._adb_shell("input", "tap", str(tx), str(ty))
        time.sleep(0.05)
        result = self._adb_shell("input", "tap", str(tx), str(ty))
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="double_tap",
                            message=f"double_tap ({tx}, {ty})")

    def swipe(self, *, direction: Optional[str] = None,
              from_xy: Optional[Tuple[int, int]] = None,
              to_xy: Optional[Tuple[int, int]] = None,
              duration_ms: int = 300,
              element: Optional[int] = None) -> ActionResult:
        info = self.device_info()
        cx, cy = info.screen_width // 2, info.screen_height // 2
        dist = min(info.screen_width, info.screen_height) // 3
        duration_ms = max(100, min(duration_ms, 5000))

        if from_xy and to_xy:
            fx, fy = from_xy
            tx, ty = to_xy
        elif direction:
            if element is not None:
                cx, cy = self._resolve_target(element, None, None)
            offsets = {
                "up": (0, -dist), "down": (0, dist),
                "left": (-dist, 0), "right": (dist, 0),
            }
            if direction not in offsets:
                return ActionResult(ok=False, action="swipe",
                                    message=f"invalid direction: {direction!r}")
            dx, dy = offsets[direction]
            fx, fy = cx, cy
            tx, ty = max(0, cx + dx), max(0, cy + dy)
        else:
            return ActionResult(ok=False, action="swipe",
                                message="provide direction or coordinates")

        result = self._adb_shell(
            "input", "swipe", str(fx), str(fy), str(tx), str(ty), str(duration_ms),
        )
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="swipe",
                            message=f"swipe ({fx},{fy})→({tx},{ty}) {duration_ms}ms")

    # ── Text input ──────────────────────────────────────────────────

    def type_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        validate_text_input(text)
        if element is not None:
            self.tap(element=element)
            time.sleep(0.3)
        escaped = text.replace("%", "%%").replace(" ", "%s")
        result = self._adb_shell("input", "text", escaped)
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="type",
                            message=f"typed {len(text)} chars")

    def clear_text(self, element: Optional[int] = None) -> ActionResult:
        if element is not None:
            self.tap(element=element)
            time.sleep(0.3)
        # Select all (Ctrl+A) then delete. keycombination requires API 28+
        # (our minSdk is 26, but most emulators are 28+).
        r = self._adb_shell("input", "keycombination", "KEYCODE_CTRL_LEFT", "KEYCODE_A")
        if r.returncode != 0:
            # Fallback for API 26-27: triple-tap to select all
            self._adb_shell("input", "keyevent", "KEYCODE_MOVE_HOME")
            self._adb_shell(
                "input", "swipe", "0", "0", "0", "0", "3000",
            )
        time.sleep(0.1)
        self._adb_shell("input", "keyevent", "KEYCODE_DEL")
        return ActionResult(ok=True, action="clear_text", message="cleared field")

    def set_text(self, text: str, element: Optional[int] = None) -> ActionResult:
        self.clear_text(element=element)
        time.sleep(0.1)
        return self.type_text(text, element=None)

    # ── Keyevent ────────────────────────────────────────────────────

    def keyevent(self, keycode: str) -> ActionResult:
        validated = validate_keycode(keycode)
        result = self._adb_shell("input", "keyevent", validated)
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="keyevent", message=f"keyevent {validated}")

    # ── App management ──────────────────────────────────────────────

    def launch_app(self, package: str, activity: Optional[str] = None) -> ActionResult:
        validate_package_name(package)
        if activity:
            validate_activity_name(activity)
            result = self._adb_shell("am", "start", "-n", f"{package}/{activity}")
        else:
            result = self._adb_shell(
                "monkey", "-p", package,
                "-c", "android.intent.category.LAUNCHER", "1",
            )
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="launch_app", message=f"launched {package}")

    def stop_app(self, package: str) -> ActionResult:
        validate_package_name(package)
        result = self._adb_shell("am", "force-stop", package)
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="stop_app", message=f"stopped {package}")

    def list_apps(self, installed_only: bool = True) -> List[Dict[str, Any]]:
        args = ["pm", "list", "packages"]
        if installed_only:
            args.append("-3")
        result = self._adb_shell(*args)
        if result.returncode != 0:
            return []
        apps = []
        for line in result.stdout.strip().splitlines():
            pkg = line.replace("package:", "").strip()
            if pkg:
                apps.append({"package": pkg})
        return apps

    def current_app(self) -> Dict[str, str]:
        return self._get_foreground_app()

    def install_apk(self, apk_path: str) -> ActionResult:
        validate_apk_path(apk_path)
        if not os.path.isfile(apk_path):
            return ActionResult(ok=False, action="install_apk",
                                message=f"file not found: {apk_path}")
        result = self._adb("install", "-r", apk_path, timeout=120)
        ok = result.returncode == 0
        return ActionResult(ok=ok, action="install_apk",
                            message=result.stdout or result.stderr)

    # ── Shell (requires approval) ───────────────────────────────────

    def shell(self, command: str) -> ActionResult:
        validate_shell_command(command)
        parts = command.split()
        result = self._adb_shell(*parts, timeout=30)
        output = (result.stdout or result.stderr)[:2000]
        return ActionResult(
            ok=result.returncode == 0, action="shell", message=output,
        )
