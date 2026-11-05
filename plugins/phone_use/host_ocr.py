"""Headless host OCR for Android screenshots.

The helper reads PNG bytes captured through ADB. It never captures the Mac
display and does not require a visible emulator window.
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import re
import select
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

from .backend import UIElement

logger = logging.getLogger(__name__)

_DEFAULT_HELPER_PATH = Path.home() / ".hermes" / "bin" / "phone-ocr"


def add_semantic_regions(
    elements: List[UIElement],
    *,
    package: str,
    width: int,
    height: int,
) -> List[UIElement]:
    """Add controls that are visually blank but stable in known app layouts."""
    if package != "com.tencent.mm" or not elements or width <= 0 or height <= 0:
        return elements

    # Host OCR does not retain Android's bubble hierarchy. Mark only clearly
    # side-aligned text for workflows that need message direction.
    for element in elements:
        if element.class_name != "host.ocr.Text":
            continue
        left, _top, right, _bottom = element.bounds
        if left <= int(width * .46) and right <= int(width * .88):
            element.attributes["message_direction"] = "incoming"
        elif left >= int(width * .45):
            element.attributes["message_direction"] = "outgoing"

    title_limit = max(220, int(height * 0.1))
    title_candidates = [
        element
        for element in elements
        if element.bounds[1] < title_limit
        and int(width * 0.2) <= element.center()[0] <= int(width * 0.8)
        and element.text.strip()
    ]
    if not title_candidates:
        return elements
    # Contacts/Discover/Me have titles and content but no composer. Require
    # both navigation labels in their bottom slots, not arbitrary message text.
    has_chat_tab = any(
        e.bounds[1] >= height * .9 and e.center()[0] < width * .25
        and e.text.strip().casefold() in {'wechat', '微信', 'chats', '聊天'}
        for e in elements
    )
    has_contacts_tab = any(
        e.bounds[1] >= height * .9 and width * .25 <= e.center()[0] < width * .5
        and e.text.strip().casefold() in {'contacts', '通讯录'}
        for e in elements
    )
    is_conversation_list = any(
        re.fullmatch(
            r"(?:wechat|微信)\s*(?:[（(]\d+[）)])?",
            element.text.strip(),
            re.IGNORECASE,
        )
        for element in title_candidates
    )
    if is_conversation_list:
        search_region = UIElement(
            index=max(element.index for element in elements) + 1,
            class_name="host.ocr.WeChatSearch",
            text="[WeChat search]",
            bounds=(int(width * 0.77), int(height * 0.035), int(width * 0.90), int(height * 0.085)),
            clickable=True,
            attributes={"source": "layout", "app": "com.tencent.mm"},
        )
        return [*elements, search_region]

    if has_chat_tab and has_contacts_tab:
        return elements

    has_message_content = any(
        title_limit <= element.bounds[1] < height - 300
        and element.text.strip()
        for element in elements
    )
    if not has_message_content:
        return elements

    input_region = UIElement(
        index=max(element.index for element in elements) + 1,
        class_name="host.ocr.MessageInput",
        text="[WeChat message input]",
        bounds=(int(width * 0.102), height - 235, int(width * 2 / 3), height - 75),
        clickable=True,
        focusable=True,
        attributes={"source": "layout", "app": "com.tencent.mm"},
    )
    return [*elements, input_region]


# phone-ocr --serve holds ~370 MB once Vision is loaded, so it is started on
# demand and dropped after this long without a request. Captures inside one
# reply arrive seconds apart and share a warm process.
_SERVER_IDLE_SECONDS = 60.0


class _OcrServer:
    """Persistent ``phone-ocr --serve`` process (see phone_ocr.swift).

    Vision's cold start and the per-frame pixel passes dominated one-shot OCR.
    ``request`` returns None whenever the caller should use the one-shot
    helper instead: a binary built before ``--serve`` existed, a crash, a
    timeout or an error reply. Unsupported is remembered until restart.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._timer: Optional[threading.Timer] = None
        self._last_used = 0.0
        self._unsupported = False

    def request(
        self, helper: Path, image_format: str, data, *,
        width: int = 0, height: int = 0, timeout: float = 20.0,
    ) -> Optional[dict]:
        header = json.dumps({
            "format": image_format, "length": len(data), "width": width, "height": height,
        }).encode() + b"\n"
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._unsupported:
                return None
            fresh = (self._proc is None or self._proc.poll() is not None
                     or self._path != helper)
            try:
                proc = self._ensure(helper)
                self._write(proc, header, deadline)
                self._write(proc, data, deadline)
                payload = json.loads(self._read_line(proc, deadline))
            except (TimeoutError, ValueError) as exc:
                # Before OSError: TimeoutError is a subclass of it, and a slow
                # frame must not disable the resident helper for good.
                self._stop()
                logger.warning("Host OCR server failed (%s); using one-shot helper", exc)
                return None
            except (OSError, EOFError) as exc:
                # A fresh process that dies without answering cannot serve.
                self._stop()
                self._unsupported = self._unsupported or fresh
                logger.info("Host OCR server unavailable (%s); using one-shot helper", exc)
                return None
            self._last_used = time.monotonic()
            self._arm_idle_timer()
            if not isinstance(payload, dict) or "error" in payload:
                # An error reply may leave unread request bytes in the pipe.
                self._stop()
                return None
            return payload

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _ensure(self, helper: Path) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None and self._path == helper:
            return self._proc
        self._stop()
        self._proc = subprocess.Popen(
            [str(helper), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._path = helper
        os.set_blocking(self._proc.stdin.fileno(), False)
        return self._proc

    @staticmethod
    def _write(proc: subprocess.Popen, data, deadline: float) -> None:
        fd = proc.stdin.fileno()
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(fd, view[:1 << 20]):]
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                    raise TimeoutError("write timed out") from None

    @staticmethod
    def _read_line(proc: subprocess.Popen, deadline: float) -> bytes:
        fd = proc.stdout.fileno()
        buffer = bytearray()
        while not buffer.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("read timed out")
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise EOFError("helper exited")
            buffer += chunk
        return bytes(buffer)

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # EOF ends the helper's serve loop
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _arm_idle_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(_SERVER_IDLE_SECONDS, self._close_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _close_if_idle(self) -> None:
        with self._lock:
            if time.monotonic() - self._last_used >= _SERVER_IDLE_SECONDS:
                self._stop()


_server = _OcrServer()
atexit.register(_server.close)


def _recognize_payload(
    png_b64: str,
    *,
    helper_path: Optional[Path] = None,
) -> Optional[dict]:
    """Run the local helper without opening or following any image content.

    Only the default helper is kept resident; an explicit ``helper_path``
    runs one-shot.
    """
    helper = Path(helper_path) if helper_path is not None else _DEFAULT_HELPER_PATH
    if not helper.is_file() or not os.access(helper, os.X_OK):
        logger.debug("Host OCR helper is unavailable at %s", helper)
        return None

    try:
        image_bytes = base64.b64decode(png_b64, validate=True)
    except (ValueError, TypeError) as exc:
        logger.warning("Host OCR received invalid screenshot data: %s", exc)
        return None

    if helper_path is None:
        payload = _server.request(helper, "png", image_bytes)
        if payload is not None:
            return payload

    image_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
            image_file.write(image_bytes)
            image_path = image_file.name

        result = subprocess.run(
            [str(helper), image_path],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.warning("Host OCR helper failed: %s", result.stderr.strip())
            return None
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("Host OCR failed: %s", exc)
        return None
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except OSError:
                pass

    return payload if isinstance(payload, dict) else None


def analyze_image(png_b64: str, *, helper_path: Optional[Path] = None) -> dict:
    """Extract bounded text and QR payloads as untrusted data, never actions."""
    payload = _recognize_payload(png_b64, helper_path=helper_path)
    if payload is None:
        return {"status": "unavailable", "text": [], "qr_codes": []}
    lines = [item['text'][:2000] for item in payload.get('items', [])
             if isinstance(item, dict) and isinstance(item.get('text'), str)][:100]
    codes = []
    for value in payload.get('qrCodes', []):
        if isinstance(value, str) and value and value not in codes:
            codes.append(value)
    return {
        "status": "complete", "text": lines,
        "qr_status": "complete" if 'qrCodes' in payload else "helper_upgrade_required",
        "qr_codes": [{"text": value[:4096], "truncated": len(value) > 4096}
                     for value in codes[:10]],
        "untrusted_content": True,
    }


def recognize_text(
    png_b64: str, *, helper_path: Optional[Path] = None,
) -> List[UIElement]:
    """Return clickable OCR boxes; QR payloads never become UI targets."""
    payload = _recognize_payload(png_b64, helper_path=helper_path) or {}
    return _elements_from_payload(payload)


def recognize_rgba(rgba, width: int, height: int) -> Optional[List[UIElement]]:
    """OCR a raw RGBA frame (``adb exec-out screencap``).

    Skips the device-side PNG encode and every temp file/base64 hop. Returns
    None when the resident helper cannot serve it; callers then fall back to
    :func:`recognize_text` on a PNG.
    """
    helper = _DEFAULT_HELPER_PATH
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return None
    payload = _server.request(helper, "rgba", rgba, width=width, height=height)
    return None if payload is None else _elements_from_payload(payload)


def _elements_from_payload(payload: dict) -> List[UIElement]:
    raw_items = payload.get("items", [])
    parsed = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        bounds = raw.get("bounds")
        if not text or not isinstance(bounds, list) or len(bounds) != 4:
            continue
        try:
            left, top, right, bottom = (int(value) for value in bounds)
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        parsed.append((top, left, text, confidence, (left, top, right, bottom)))

    parsed.sort(key=lambda item: (item[0], item[1]))
    elements = [
        UIElement(
            index=index,
            class_name="host.ocr.Text",
            text=text,
            bounds=bounds,
            clickable=True,
            attributes={"source": "ocr", "confidence": confidence},
        )
        for index, (_, _, text, confidence, bounds) in enumerate(parsed, start=1)
    ]
    raw_regions = payload.get("visualRegions", []) if isinstance(payload, dict) else []
    for raw in raw_regions:
        if not isinstance(raw, dict):
            continue
        bounds = raw.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            continue
        try:
            left, top, right, bottom = (int(value) for value in bounds)
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        elements.append(UIElement(
            index=len(elements) + 1,
            class_name="host.vision.ImageCandidate",
            bounds=(left, top, right, bottom),
            clickable=True,
            attributes={"source": "vision", "confidence": confidence},
        ))
    return elements
