"""Abstract backend interface for phone control.

Mirrors hermes's ComputerUseBackend pattern. Implementations (ADB, Appium,
future iOS) must conform to this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UIElement:
    """One element from the Android accessibility tree."""

    index: int
    class_name: str                   # e.g. android.widget.Button
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    package: str = ""
    clickable: bool = False
    scrollable: bool = False
    focusable: bool = False
    enabled: bool = True
    checked: Optional[bool] = None
    attributes: Dict[str, Any] = field(default_factory=dict)

    def center(self) -> Tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2

    @property
    def label(self) -> str:
        return self.text or self.content_desc or self.resource_id or self.class_name


@dataclass
class CaptureResult:
    mode: str
    width: int
    height: int
    png_b64: Optional[str] = None
    elements: List[UIElement] = field(default_factory=list)
    current_package: str = ""
    current_activity: str = ""
    png_bytes_len: int = 0


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str = ""
    capture: Optional[CaptureResult] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceInfo:
    serial: str
    model: str = ""
    android_version: str = ""
    sdk_version: int = 0
    screen_width: int = 0
    screen_height: int = 0
    density: int = 0
    is_emulator: bool = False


class PhoneBackend(ABC):
    """Lifecycle: start() before first use, stop() at shutdown."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def device_info(self) -> DeviceInfo: ...

    @abstractmethod
    def capture(self, mode: str = "som") -> CaptureResult: ...

    @abstractmethod
    def tap(self, *, element: Optional[int] = None,
            x: Optional[int] = None, y: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def long_press(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None,
                   duration_ms: int = 1000) -> ActionResult: ...

    @abstractmethod
    def double_tap(self, *, element: Optional[int] = None,
                   x: Optional[int] = None, y: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def swipe(self, *, direction: Optional[str] = None,
              from_xy: Optional[Tuple[int, int]] = None,
              to_xy: Optional[Tuple[int, int]] = None,
              duration_ms: int = 300,
              element: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def type_text(self, text: str, element: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def clear_text(self, element: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def set_text(self, text: str, element: Optional[int] = None) -> ActionResult: ...

    @abstractmethod
    def keyevent(self, keycode: str) -> ActionResult: ...

    @abstractmethod
    def launch_app(self, package: str, activity: Optional[str] = None) -> ActionResult: ...

    @abstractmethod
    def stop_app(self, package: str) -> ActionResult: ...

    @abstractmethod
    def list_apps(self, installed_only: bool = True) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def current_app(self) -> Dict[str, str]: ...

    @abstractmethod
    def install_apk(self, apk_path: str) -> ActionResult: ...

    @abstractmethod
    def shell(self, command: str) -> ActionResult: ...

    def wait(self, seconds: float) -> ActionResult:
        import time
        time.sleep(max(0.0, min(seconds, 30.0)))
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")
