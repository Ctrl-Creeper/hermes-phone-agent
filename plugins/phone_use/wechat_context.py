"""Bounded, deterministic context collection for a verified WeChat chat."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .backend import ActionResult, CaptureResult, PhoneBackend
from .wechat import open_chat


@dataclass(frozen=True)
class CollectionScope:
    max_messages: int = 50
    max_minutes: int = 10
    max_pages: int = 8
    explicit: bool = False


class UnsupportedCollectionScope(ValueError):
    """Raised when a non-empty natural-language range cannot be bounded."""


_IMAGE_LABELS = frozenset({"image", "photo", "picture", "图片", "照片"})


def _image_bubbles(capture: CaptureResult) -> list:
    """Find likely clickable image messages, excluding the composer area."""
    top = max(220, int(capture.height * 0.10))
    bottom = capture.height - 235
    candidates = []
    for element in capture.elements:
        label = (element.text or element.content_desc or "").strip().casefold()
        class_name = (element.class_name or "").casefold()
        resource_id = (element.resource_id or "").casefold()
        left, y1, right, y2 = element.bounds
        area = max(0, right - left) * max(0, y2 - y1)
        large_image_view = (
            "image" in class_name
            and area >= 30_000
            and (right - left) >= 120
            and (y2 - y1) >= 120
        )
        semantic_image = (
            label in _IMAGE_LABELS
            or "image" in resource_id
            or "photo" in resource_id
            or "图片" in resource_id
            or large_image_view
        )
        if (
            element.clickable
            and semantic_image
            and y1 >= top
            and y2 <= bottom
            and area >= 4_000
        ):
            candidates.append(element)
    return candidates


def _open_image_bubble(
    backend: PhoneBackend, element: object,
) -> Optional[str]:
    """Open one image bubble, capture its preview, and return to the chat."""
    tapped = backend.tap(element=element.index)
    if not tapped.ok:
        return None
    backend.wait(0.25)
    preview = backend.capture(mode="screenshot")
    image = preview.png_b64 or getattr(tapped.capture, "png_b64", None)
    try:
        backend.keyevent("BACK")
        backend.wait(0.15)
    except Exception:
        logger.warning("Could not return from WeChat image preview", exc_info=True)
    return image


def parse_collection_scope(
    scope: str = "",
    *,
    max_messages: int = 50,
    max_pages: int = 8,
    max_minutes: int = 10,
) -> CollectionScope:
    text = (scope or "").strip()
    messages = max(1, min(int(max_messages), 200))
    pages = max(1, min(int(max_pages), 12))
    minutes = max(1, min(int(max_minutes), 1440))
    if not text:
        return CollectionScope(messages, minutes, pages, False)

    count_match = re.search(r"(?:最近|上面)\s*(\d+)\s*条", text)
    minute_match = re.search(r"最近\s*(\d+)\s*分钟", text)
    hour_match = re.search(r"最近\s*(\d+)\s*(?:小时|个小时)", text)
    recent_history_intent = (
        "刚刚" in text
        and ("发生了啥" in text or "发生了什么" in text)
    )
    if count_match:
        messages = max(1, min(int(count_match.group(1)), 200))
    if minute_match:
        minutes = max(1, min(int(minute_match.group(1)), 1440))
    elif hour_match:
        minutes = max(1, min(int(hour_match.group(1)) * 60, 1440))
    elif "今天" in text:
        minutes = 1440
    elif recent_history_intent:
        return CollectionScope(messages, minutes, pages, False)
    elif not count_match:
        raise UnsupportedCollectionScope(text)
    return CollectionScope(messages, minutes, pages, True)


def merge_older_lines(existing: list[str], older_page: list[str]) -> list[str]:
    """Prepend an older screen while removing only its overlap with existing."""
    max_overlap = min(len(existing), len(older_page))
    for overlap in range(max_overlap, 0, -1):
        if older_page[-overlap:] == existing[:overlap]:
            return older_page[:-overlap] + existing
    return older_page + existing


def _visible_lines(capture: CaptureResult) -> list[str]:
    top = max(220, int(capture.height * 0.10))
    bottom = capture.height - 235
    lines = []
    for element in sorted(capture.elements, key=lambda item: (item.bounds[1], item.bounds[0])):
        if element.class_name == "host.ocr.MessageInput":
            continue
        if element.bounds[1] < top or element.bounds[3] > bottom:
            continue
        label = re.sub(r"\s+", " ", (element.text or element.content_desc or "")).strip()
        if label:
            lines.append(label)
    return lines


def _oldest_visible_time(lines: list[str], now: datetime) -> Optional[datetime]:
    observed: list[datetime] = []
    for line in lines:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([AP]M)?", line, re.IGNORECASE)
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        suffix = (match.group(3) or "").upper()
        if suffix:
            hour = hour % 12 + (12 if suffix == "PM" else 0)
        if hour > 23 or minute > 59:
            continue
        value = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if value > now + timedelta(minutes=1):
            value -= timedelta(days=1)
        observed.append(value)
    return min(observed, default=None)


def collect_context(
    backend: PhoneBackend,
    chat: str,
    *,
    scope: str = "",
    max_messages: int = 50,
    max_pages: int = 8,
    max_minutes: int = 10,
    include_images: bool = True,
    open_images: bool = False,
    max_images: int = 3,
) -> ActionResult:
    try:
        effective = parse_collection_scope(
            scope,
            max_messages=max_messages,
            max_pages=max_pages,
            max_minutes=max_minutes,
        )
    except UnsupportedCollectionScope:
        return ActionResult(
            ok=False,
            action="wechat_collect_context",
            message=f"needs_clarification: unsupported collection scope {scope!r}",
            meta={"stop_reason": "needs_clarification"},
        )

    opened = open_chat(backend, chat)
    if not opened.ok or opened.capture is None:
        return ActionResult(
            ok=False,
            action="wechat_collect_context",
            message=opened.message or "could not open WeChat chat",
            capture=opened.capture,
        )

    current = opened.capture
    combined: list[str] = []
    screenshots: list[str] = []
    opened_images = 0
    max_images = max(0, min(int(max_images), 5))
    seen_image_bubbles: set[tuple[str, tuple[int, int, int, int]]] = set()
    signatures: set[tuple[str, ...]] = set()
    stop_reason = "page_limit"
    now = datetime.now().astimezone()
    pages = 0

    for page_index in range(effective.max_pages):
        if include_images:
            # Image inspection needs the real accessibility nodes (ImageView
            # bounds/click targets), while ordinary context collection can use
            # the faster OCR-backed SOM capture.
            visual = backend.capture(
                mode="image_hierarchy" if open_images else "som"
            )
            if visual.png_b64 and not open_images and len(screenshots) < 5:
                screenshots.append(visual.png_b64)
            current = visual

            if open_images and opened_images < max_images:
                for image_element in _image_bubbles(current):
                    signature = (
                        (image_element.text or image_element.content_desc or "").strip(),
                        image_element.bounds,
                    )
                    if signature in seen_image_bubbles:
                        continue
                    seen_image_bubbles.add(signature)
                    image = _open_image_bubble(backend, image_element)
                    if image:
                        screenshots.append(image)
                        opened_images += 1
                    if opened_images >= max_images:
                        break

        page_lines = _visible_lines(current)
        signature = tuple(page_lines)
        pages += 1
        if signature in signatures:
            stop_reason = "repeated_page"
            break
        signatures.add(signature)
        combined = page_lines if not combined else merge_older_lines(combined, page_lines)

        if len(combined) >= effective.max_messages:
            stop_reason = "message_limit"
            break
        oldest = _oldest_visible_time(page_lines, now)
        if oldest is not None and now - oldest >= timedelta(minutes=effective.max_minutes):
            stop_reason = "time_limit"
            break
        if page_index + 1 >= effective.max_pages:
            break

        swiped = backend.swipe(direction="down", duration_ms=300)
        if not swiped.ok:
            return ActionResult(
                ok=False,
                action="wechat_collect_context",
                message=swiped.message or "could not scroll WeChat history",
                capture=current,
            )
        backend.wait(0.2)
        current = backend.capture(mode="hierarchy")

    combined = combined[-effective.max_messages:]
    coverage = "complete" if stop_reason in {"message_limit", "time_limit", "chat_top"} else "partial"
    meta = {
        "chat": chat,
        "scope": scope,
        "lines": combined,
        "pages": pages,
        "max_messages": effective.max_messages,
        "max_minutes": effective.max_minutes,
        "max_pages": effective.max_pages,
        "coverage": coverage,
        "stop_reason": stop_reason,
        "screenshots": screenshots,
        "image_count": opened_images if open_images else len(screenshots),
    }
    return ActionResult(
        ok=True,
        action="wechat_collect_context",
        message=f"collected {len(combined)} visible line(s) from {pages} page(s)",
        capture=current,
        meta=meta,
    )
