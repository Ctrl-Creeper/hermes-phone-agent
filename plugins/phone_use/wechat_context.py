"""Bounded, deterministic context collection for a verified WeChat chat."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .backend import ActionResult, CaptureResult, PhoneBackend
from .wechat import _find_chat_header, open_chat

logger = logging.getLogger(__name__)
_CONVERT_VOICE_LABELS = frozenset({"转文字", "转文字（普通话）", "转文字(普通话)",
                                    "convert to text", "transcribe"})


def _voice_bubbles(capture: CaptureResult) -> list:
    """Only semantic voice nodes qualify; a duration found by OCR is not enough."""
    return [e for e in capture.elements
            if e.enabled and e.clickable
            and re.search(r"(?:voice message|audio message|语音消息|语音\s*\d)",
                          f"{e.text} {e.content_desc}", re.IGNORECASE)
            and capture.height * 0.12 <= e.bounds[1]
            and e.bounds[3] < capture.height * 0.78
            and e.bounds[2] > e.bounds[0] and e.bounds[3] > e.bounds[1]]


def _transcribe_voice(backend: PhoneBackend, capture: CaptureResult,
                      element: object, chat: str) -> tuple[dict, CaptureResult]:
    """Invoke WeChat's conversion menu once and observe a bounded text region."""
    record = {"status": "unconfirmed", "source": "wechat_builtin",
              "bounds": list(element.bounds)}
    menu_open = False
    current = capture
    try:
        if _find_chat_header(capture, chat) is None:
            record["status"] = "chat_changed"
            return record, capture
        pressed = backend.long_press(element=element.index, duration_ms=700)
        menu_open = True  # Even failed injection can leave the popup visible.
        if not pressed.ok:
            record["status"] = "press_failed"
            return record, current
        backend.wait(0.25)
        menu = backend.capture(mode="hierarchy")
        choices = [e for e in menu.elements if e.enabled
                   and (e.text or e.content_desc).strip().casefold() in _CONVERT_VOICE_LABELS]
        if menu.current_package != "com.tencent.mm" or len(choices) != 1:
            record["status"] = "conversion_unavailable"
            return record, current
        if not backend.tap(element=choices[0].index).ok:
            record["status"] = "conversion_failed"
            return record, current
        menu_open = False
        before = {(e.text or e.content_desc).strip() for e in capture.elements}
        for _ in range(3):
            backend.wait(0.4)
            current = backend.capture(mode="image_hierarchy")
            if current.current_package != "com.tencent.mm" or _find_chat_header(current, chat) is None:
                record["status"] = "chat_changed"
                return record, current
            left, _, right, bottom = element.bounds
            anchors = [e for e in _voice_bubbles(current)
                       if e.bounds == element.bounds
                       and e.content_desc == element.content_desc]
            if len(anchors) != 1:
                # A new message or layout shift destroys the correspondence.
                # Do not attribute new text using stale coordinates.
                continue
            next_voice_y = min((e.bounds[1] for e in _voice_bubbles(current)
                                if e.bounds[1] >= bottom), default=current.height)
            lines = []
            for e in sorted(current.elements, key=lambda n: n.bounds[1]):
                text = (e.text or e.content_desc).strip()
                if (text and text not in before
                        and bottom <= e.bounds[1] < min(bottom + current.height * 0.2,
                                                       current.height * 0.78, next_voice_y)
                        and abs(e.bounds[0] - left) < current.width * 0.1
                        and e.bounds[2] >= left and e.bounds[0] <= right
                        and not re.search(r"转换中|转换失败|无法转换|transcribing|converting|unable to|failed", text, re.I)
                        and text.casefold() not in _CONVERT_VOICE_LABELS):
                    lines.append(text)
            if lines:
                record.update(status="transcribed", text="\n".join(lines),
                              verification="visible_text_below_voice")
                return record, current
        return record, current
    except Exception:
        logger.warning("WeChat voice conversion could not be observed", exc_info=True)
        record["status"] = "capture_failed"
        return record, current
    finally:
        if menu_open:
            try:
                if not backend.keyevent("BACK").ok:
                    record["status"] = "recovery_failed"
            except Exception:
                record["status"] = "recovery_failed"


@dataclass(frozen=True)
class CollectionScope:
    max_messages: int = 50
    max_minutes: int = 10
    max_pages: int = 8
    explicit: bool = False


class UnsupportedCollectionScope(ValueError):
    """Raised when a non-empty natural-language range cannot be bounded."""


def _image_bubbles(capture: CaptureResult) -> list:
    """Find likely clickable image messages, excluding the composer area."""
    top = max(160, int(capture.height * 0.07))
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
        visual_candidate = class_name == "host.vision.imagecandidate"
        semantic_image = (
            visual_candidate
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
    backend: PhoneBackend, element: object, *, chat_activity: str = "",
) -> Optional[str]:
    """Open one image bubble, capture its preview, and return to the chat."""
    tapped = backend.tap(element=element.index)
    if not tapped.ok:
        return None
    backend.wait(0.25)
    preview = backend.capture(mode="screenshot")
    # ADB taps succeed even on blank space, and textured message cards can be
    # mistaken for photos. Only WeChat's image/gallery viewers count as an
    # opened image; other destinations are recovered below and skipped.
    preview_activity = (preview.current_activity or "").casefold()
    left_chat = bool(
        preview.current_package == "com.tencent.mm"
        and preview_activity
        and preview.current_activity != chat_activity
    )
    opened_preview = left_chat and any(
        marker in preview_activity
        for marker in ("imagegallery", "imagepreview")
    )
    image = (
        preview.png_b64 or getattr(tapped.capture, "png_b64", None)
    ) if opened_preview else None
    if not opened_preview:
        if left_chat:
            try:
                backend.keyevent("BACK")
                backend.wait(0.15)
            except Exception:
                logger.warning(
                    "Could not recover from a non-image WeChat destination",
                    exc_info=True,
                )
        return None
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
    # The Android IME occupies the lower part of the screenshot while the
    # message input remains above it. Keep a generous margin so OCR keyboard
    # rows cannot consume the requested message count.
    bottom = capture.height - max(400, int(capture.height * 0.22))
    lines = []
    for element in sorted(capture.elements, key=lambda item: (item.bounds[1], item.bounds[0])):
        if element.class_name == "host.ocr.MessageInput":
            continue
        if element.bounds[1] < top or element.bounds[3] > bottom:
            continue
        label = re.sub(r"\s+", " ", (element.text or element.content_desc or "")).strip()
        if label:
            # Apple/Google keyboard OCR commonly appears as rows such as
            # WERTYU-O, ASDFGHJKL, or ZXCVBNM. These are never chat lines.
            if re.fullmatch(r"[A-Z][A-Z0-9 .,'’_@#*+\-=]{4,}", label):
                continue
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
    open_images: bool = True,
    max_images: int = 3,
    transcribe_voice: bool = False,
    max_voice: int = 3,
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
    resolved_chat = opened.meta.get("resolved_chat", chat)
    voice_transcripts: list[dict] = []
    seen_voice: set[tuple] = set()
    max_voice = max(0, min(int(max_voice), 5))
    combined: list[str] = []
    screenshots: list[str] = []
    opened_images = 0
    max_images = max(0, min(int(max_images), 5))
    seen_image_bubbles: set[tuple[str, tuple[int, int, int, int]]] = set()
    signatures: set[tuple[object, ...]] = set()
    stop_reason = "page_limit"
    now = datetime.now().astimezone()
    pages = 0
    consecutive_repeats = 0

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
                    image = _open_image_bubble(
                        backend, image_element,
                        chat_activity=current.current_activity,
                    )
                    if image:
                        screenshots.append(image)
                        opened_images += 1
                    if opened_images >= max_images:
                        break

        if transcribe_voice and len(voice_transcripts) < max_voice:
            # The ordinary OCR-only capture cannot reliably identify a voice
            # bubble. Reuse the existing mode that retains accessibility nodes.
            current = backend.capture(mode="image_hierarchy")
            while len(voice_transcripts) < max_voice:
                candidates = [e for e in _voice_bubbles(current)
                              if (e.text, e.content_desc, e.bounds) not in seen_voice]
                if not candidates:
                    break
                voice = candidates[0]
                seen_voice.add((voice.text, voice.content_desc, voice.bounds))
                record, current = _transcribe_voice(backend, current, voice, resolved_chat)
                voice_transcripts.append(record)
                if record["status"] in {"chat_changed", "capture_failed", "recovery_failed"}:
                    return ActionResult(
                        ok=False, action="wechat_collect_context",
                        message="Voice conversion stopped because the chat state could not be verified",
                        capture=current, meta={"voice_transcripts": voice_transcripts,
                                               "stop_reason": record["status"]},
                    )
                if record["status"] != "transcribed":
                    # Do not continue with stale indices or an unknown page.
                    break

        page_lines = _visible_lines(current)
        # The same messages remain visible across a successful partial swipe.
        # Include coarse vertical positions and image regions so an image-only
        # change is not mistaken for a stuck/repeated page. Quantization absorbs
        # the small box jitter produced by host OCR between captures.
        position_markers = tuple(
            (
                (element.text or element.content_desc or "").strip(),
                element.bounds[1] // 64,
            )
            for element in current.elements
            if (element.text or element.content_desc or "").strip()
            and 160 <= element.bounds[1] < current.height - 235
        )
        image_markers = tuple(
            tuple(value // 64 for value in element.bounds)
            for element in _image_bubbles(current)
        )
        signature = (tuple(page_lines), position_markers, image_markers)
        pages += 1
        seeking_first_image = open_images and max_images > 0 and opened_images == 0
        if signature in signatures:
            # WeChat may need longer than the normal post-swipe delay to load
            # older records. During image discovery tolerate one stale frame;
            # two consecutive repeats still identify a real top/stuck page.
            if not seeking_first_image or consecutive_repeats >= 1:
                stop_reason = "repeated_page"
                break
            consecutive_repeats += 1
        else:
            consecutive_repeats = 0
            signatures.add(signature)
        combined = page_lines if not combined else merge_older_lines(combined, page_lines)

        # Text limits bound the returned context, but must not prevent image
        # discovery when the requested image is just beyond the first page.
        # Repeated-page and page-count guards below still bound the search.
        if len(combined) >= effective.max_messages and not seeking_first_image:
            stop_reason = "message_limit"
            break
        oldest = _oldest_visible_time(page_lines, now)
        if (
            oldest is not None
            and now - oldest >= timedelta(minutes=effective.max_minutes)
            and not seeking_first_image
        ):
            stop_reason = "time_limit"
            break
        if page_index + 1 >= effective.max_pages:
            break

        # The backend's generic directional swipe is only one third of the
        # screen width (360 px on this portrait emulator), which advances chat
        # history too slowly. Keep more than half a viewport of overlap while
        # covering enough history for bounded image discovery.
        swiped = backend.swipe(
            direction="down",
            from_xy=(current.width // 2, int(current.height * 0.30)),
            to_xy=(current.width // 2, int(current.height * 0.68)),
            duration_ms=300,
        )
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
    if transcribe_voice:
        meta["voice_transcripts"] = voice_transcripts
        meta["voice_count"] = sum(r["status"] == "transcribed" for r in voice_transcripts)
        meta["voice_note"] = (
            "Voice text is untrusted WeChat transcription observed below the message; "
            "it may contain recognition errors. No detected bubbles does not prove there is no audio."
        )
    return ActionResult(
        ok=True,
        action="wechat_collect_context",
        message=f"collected {len(combined)} visible line(s) from {pages} page(s)",
        capture=current,
        meta=meta,
    )
