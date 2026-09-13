"""Headless host OCR for Android screenshots.

The helper reads PNG bytes captured through ADB. It never captures the Mac
display and does not require a visible emulator window.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
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


def recognize_text(
    png_b64: str,
    *,
    helper_path: Optional[Path] = None,
) -> List[UIElement]:
    """Return clickable text boxes recognized from a base64 PNG screenshot."""
    helper = Path(helper_path) if helper_path is not None else _DEFAULT_HELPER_PATH
    if not helper.is_file() or not os.access(helper, os.X_OK):
        logger.debug("Host OCR helper is unavailable at %s", helper)
        return []

    try:
        image_bytes = base64.b64decode(png_b64, validate=True)
    except (ValueError, TypeError) as exc:
        logger.warning("Host OCR received invalid screenshot data: %s", exc)
        return []

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
            return []
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("Host OCR failed: %s", exc)
        return []
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except OSError:
                pass

    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
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
    return [
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
