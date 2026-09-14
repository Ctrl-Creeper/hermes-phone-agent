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
    signatures: set[tuple[str, ...]] = set()
    stop_reason = "page_limit"
    now = datetime.now().astimezone()
    pages = 0

    for page_index in range(effective.max_pages):
        if include_images:
            visual = backend.capture(mode="som")
            if visual.png_b64 and len(screenshots) < 5:
                screenshots.append(visual.png_b64)
            current = visual

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
    }
    return ActionResult(
        ok=True,
        action="wechat_collect_context",
        message=f"collected {len(combined)} visible line(s) from {pages} page(s)",
        capture=current,
        meta=meta,
    )
