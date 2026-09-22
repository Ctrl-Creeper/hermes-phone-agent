"""Deterministic WeChat flows built on the phone backend."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Callable, Optional

from .backend import ActionResult, CaptureResult, PhoneBackend, UIElement

logger = logging.getLogger(__name__)

WECHAT_PACKAGE = "com.tencent.mm"
_MESSAGE_INPUT_CLASS = "host.ocr.MessageInput"
_SEARCH_CONTROL_CLASS = "host.ocr.WeChatSearch"
_SEND_LABELS = frozenset({"发送", "send", "发送消息", "send message"})
_VOICE_INPUT_LABELS = frozenset({"hold to talk", "按住说话"})
_VOICE_TRANSCRIPTION_PREFIXES = ("tap to convert to text", "轻触转文字")
_MIN_TITLE_SIMILARITY = 0.80
_SEARCH_RESULT_SECTIONS = frozenset({
    "top hits", "最佳匹配", "最常使用",
    "contacts", "联系人",
    "group chats", "群聊",
    "official accounts", "公众号",
    "mini programs", "小程序",
    "chat history", "chat histories", "聊天记录",
})
_CHAT_RESULT_SECTIONS = frozenset({
    "top hits", "最佳匹配", "最常使用", "contacts", "联系人", "group chats", "群聊",
})
_TOP_HIT_LABELS = frozenset({"top hits", "最佳匹配", "最常使用"})
_CONTACT_LABELS = frozenset({"contacts", "通讯录"})
_CONVERSATION_TAB_LABELS = frozenset({"wechat", "微信", "chats", "聊天"})
_NEW_FRIENDS_LABELS = frozenset({"new friends", "新的朋友"})
_ACCEPT_LABELS = frozenset({"accept", "接受", "添加"})
_ADDED_LABELS = frozenset({"added", "accepted", "已添加", "已通过"})
_SYMBOL_CHAT_HINT_TTL_SECONDS = 120.0
_symbol_chat_hint: Optional[tuple[str, str, float]] = None
_EMOJI_PLACEHOLDER = re.compile(r"\[(?:emoji|sticker|表情)\]", re.IGNORECASE)


def _has_emoji(value: str) -> bool:
    return any(unicodedata.category(c) == "So" for c in value)


def _matches_placeholder_title(observed: str, requested: str) -> bool:
    """An unknown emoji may match symbols, never missing text or another name."""
    parts = _EMOJI_PLACEHOLDER.split(_normalize_chat_title(requested))
    if len(parts) < 2 or not any(any(c.isalnum() for c in p) for p in parts):
        return False
    match = re.fullmatch("(.+?)".join(re.escape(p) for p in parts),
                         _normalize_chat_title(observed))
    if match is None:
        return False
    return all(_has_emoji(group) and all(
        unicodedata.category(c) in {"So", "Sk", "Mn", "Me"}
        or c == "\u200d" for c in group
    ) for group in match.groups())


def _label(element: UIElement) -> str:
    return (element.text or element.content_desc or "").strip()


def _find_text(elements: list[UIElement], text: str) -> Optional[UIElement]:
    if _EMOJI_PLACEHOLDER.search(text):
        matches = [e for e in elements if _matches_placeholder_title(_label(e), text)]
        return matches[0] if len(matches) == 1 else None
    wanted = _normalize_chat_title(text)
    exact = [
        element for element in elements
        if _normalize_chat_title(_label(element)) == wanted
    ]
    if exact:
        return exact[0] if len(exact) == 1 else None

    # Short names are too collision-prone for fuzzy matching. For longer chat
    # titles, accept OCR substitutions only when one candidate is clearly best.
    if len(wanted) < 5 or _has_emoji(wanted):
        return None
    matches = [
        (
            SequenceMatcher(
                None, wanted, _normalize_chat_title(_label(element)),
            ).ratio(),
            element,
        )
        for element in elements
        if _normalize_chat_title(_label(element))
    ]
    matches.sort(key=lambda match: match[0], reverse=True)
    if not matches or matches[0][0] < _MIN_TITLE_SIMILARITY:
        return None
    if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.05:
        return None
    return matches[0][1]


def _normalize_chat_title(value: str) -> str:
    """Normalize WeChat group counts and common OCR punctuation noise."""
    normalized = re.sub(r"\s+", "", value).casefold()
    normalized = normalized.translate(str.maketrans({"（": "(", "）": ")"}))
    # WeChat appends a volatile member count to group titles. Depending on
    # locale and OCR, this may be `(24)`, `(24人)`, `(24 members)`, have a
    # missing closing bracket, or carry stray glyphs after it. Remove that
    # metadata before applying the 80% title similarity threshold.
    normalized = re.sub(
        r"[\(\[【]\d{1,4}(?:(?:member|people)s?|人)?[\)\]】]?.*$",
        "",
        normalized,
    )
    return normalized


def _is_symbol_only_title(value: str) -> bool:
    normalized = _normalize_chat_title(value)
    return bool(normalized) and not any(char.isalnum() for char in normalized)


def _symbol_header_signature(capture: CaptureResult) -> str:
    title_limit = max(220, int(capture.height * 0.12))
    candidates = [
        element for element in capture.elements
        if element.bounds[1] < title_limit
        and int(capture.width * 0.18) <= element.center()[0] <= int(capture.width * 0.82)
        and _normalize_chat_title(_label(element))
    ]
    if not candidates:
        return ""
    centered = min(
        candidates,
        key=lambda element: abs(element.center()[0] - capture.width // 2),
    )
    return _normalize_chat_title(_label(centered))


def _remember_symbol_chat(chat: str, capture: CaptureResult) -> None:
    global _symbol_chat_hint
    if not _is_symbol_only_title(chat) or _find_input(capture.elements) is None:
        return
    signature = _symbol_header_signature(capture)
    if signature:
        _symbol_chat_hint = (
            _normalize_chat_title(chat), signature, time.monotonic(),
        )


def _clear_symbol_chat_hint(chat: str = "") -> None:
    global _symbol_chat_hint
    if (
        not chat
        or _symbol_chat_hint is None
        or _symbol_chat_hint[0] == _normalize_chat_title(chat)
    ):
        _symbol_chat_hint = None


def _matches_recent_symbol_chat(chat: str, capture: CaptureResult) -> bool:
    global _symbol_chat_hint
    if _symbol_chat_hint is None or not _is_symbol_only_title(chat):
        return False
    wanted, signature, observed_at = _symbol_chat_hint
    if time.monotonic() - observed_at > _SYMBOL_CHAT_HINT_TTL_SECONDS:
        _symbol_chat_hint = None
        return False
    if wanted != _normalize_chat_title(chat):
        return False
    observed = _symbol_header_signature(capture)
    return bool(observed) and observed == signature


def _find_chat_header(capture: CaptureResult, chat: str) -> Optional[UIElement]:
    """Find the conversation title, as opposed to a list row or message body."""
    if _EMOJI_PLACEHOLDER.search(chat):
        return None  # Resolve through search, never trust the current chat alone.
    wanted = _normalize_chat_title(chat)
    if not wanted:
        return None
    title_limit = max(220, int(capture.height * 0.12))
    candidates = [
        element for element in capture.elements
        if element.bounds[1] < title_limit
        and int(capture.width * 0.18) <= element.center()[0] <= int(capture.width * 0.82)
    ]
    for element in candidates:
        observed = _normalize_chat_title(_label(element))
        if observed == wanted or (
            len(wanted) >= 5
            and not _has_emoji(wanted)
            and SequenceMatcher(None, wanted, observed).ratio()
            >= _MIN_TITLE_SIMILARITY
        ):
            return element
    if _matches_recent_symbol_chat(chat, capture):
        return min(
            candidates,
            key=lambda element: abs(element.center()[0] - capture.width // 2),
            default=None,
        )
    return None


def _find_input(elements: list[UIElement]) -> Optional[UIElement]:
    return next(
        (element for element in elements if element.class_name == _MESSAGE_INPUT_CLASS),
        None,
    )


def _is_conversation_list(capture: CaptureResult) -> bool:
    title_limit = max(220, int(capture.height * 0.12))
    return any(
        element.bounds[1] < title_limit
        and _normalize_chat_title(_label(element)) in {"wechat", "微信"}
        for element in capture.elements
    ) and _find_input(capture.elements) is None


def _find_conversation_tab(capture: CaptureResult) -> Optional[UIElement]:
    """Find the bottom Chats/WeChat tab on any top-level WeChat page."""
    min_y = int(capture.height * 0.84)
    return next((
        element for element in capture.elements
        if element.bounds[1] >= min_y
        and _normalize_chat_title(_label(element)) in _CONVERSATION_TAB_LABELS
    ), None)


def _find_search_control(capture: CaptureResult) -> Optional[UIElement]:
    semantic = next(
        (
            element for element in capture.elements
            if element.class_name == _SEARCH_CONTROL_CLASS
        ),
        None,
    )
    if semantic is not None:
        return semantic
    title_limit = max(220, int(capture.height * 0.12))
    return next(
        (
            element for element in capture.elements
            if element.bounds[1] < title_limit
            and element.center()[0] > int(capture.width * 0.72)
            and _label(element).strip().casefold() in {"q", "search", "搜索"}
        ),
        None,
    )


def _is_search_page(capture: CaptureResult) -> bool:
    labels = " ".join(_label(element).casefold() for element in capture.elements)
    title_limit = max(220, int(capture.height * 0.12))
    has_result_section = any(
        element.bounds[1] >= title_limit
        and element.bounds[1] < int(capture.height * 0.7)
        and _label(element).casefold() in _SEARCH_RESULT_SECTIONS
        for element in capture.elements
    )
    return has_result_section or (
        "search local or internet results" in labels
        or "搜索本地或互联网结果" in labels
    )


def _settle_capture(
    backend: PhoneBackend,
    capture: CaptureResult,
    predicate: Callable[[CaptureResult], bool],
    attempts: int = 2,
) -> CaptureResult:
    current = capture
    for _ in range(attempts):
        if predicate(current):
            break
        try:
            current = backend.capture(mode="hierarchy")
        except Exception as exc:
            logger.warning("WeChat settle capture failed: %s", exc)
            break
    return current


def _return_to_conversation_list(
    backend: PhoneBackend,
    capture: CaptureResult,
    *,
    max_steps: int = 4,
) -> ActionResult:
    """Normalize arbitrary WeChat state to the conversation list."""
    current = capture
    for _ in range(max_steps):
        if _is_conversation_list(current):
            return ActionResult(
                ok=True, action="wechat_open_chat", capture=current,
                message="WeChat conversation list is ready",
            )

        # Contacts, Discover/Moments, and Me expose the stable bottom tab.
        # Prefer it over Back because Back can leave WeChat or merely dismiss
        # a keyboard without changing pages.
        conversation_tab = _find_conversation_tab(current)
        if conversation_tab is not None:
            moved = _run_and_capture(
                backend, "tap",
                lambda target=conversation_tab: backend.tap(element=target.index),
            )
        elif current.current_activity.casefold().endswith("launcherui"):
            moved = _run_and_capture(
                backend, "tap",
                lambda: backend.tap(
                    x=int(current.width * 0.125),
                    y=int(current.height * 0.95),
                ),
            )
        else:
            moved = _run_and_capture(
                backend, "keyevent", lambda: backend.keyevent("BACK"),
            )
        if not moved.ok or moved.capture is None:
            return ActionResult(
                ok=False, action="wechat_open_chat",
                message=moved.message or "could not return to WeChat conversation list",
                capture=moved.capture or current,
            )
        current = moved.capture

    return ActionResult(
        ok=False, action="wechat_open_chat",
        message="could not reach the WeChat conversation list after recovery",
        capture=current,
    )


def _chat_is_open(capture: CaptureResult, chat: str) -> bool:
    return (
        not _is_search_page(capture)
        and _find_chat_header(capture, chat) is not None
        and _find_input(capture.elements) is not None
    )


def _find_label(
    capture: CaptureResult,
    labels: frozenset[str],
    *,
    min_y: int = 0,
) -> Optional[UIElement]:
    return next((
        element for element in capture.elements
        if element.bounds[1] >= min_y and _label(element).casefold() in labels
    ), None)


def _find_row_action(
    capture: CaptureResult,
    row: UIElement,
    labels: frozenset[str],
) -> Optional[UIElement]:
    row_y = row.center()[1]
    candidates = [
        element for element in capture.elements
        if _label(element).casefold() in labels
        and abs(element.center()[1] - row_y) <= 140
    ]
    return min(
        candidates,
        key=lambda element: abs(element.center()[1] - row_y),
        default=None,
    )


def _find_top_hit(capture: CaptureResult) -> Optional[UIElement]:
    """Find the first exact-search hit when OCR cannot read its symbol title."""
    header = next((
        element for element in capture.elements
        if _label(element).casefold() in _TOP_HIT_LABELS
    ), None)
    if header is None:
        return None

    next_section_y = min((
        element.bounds[1] for element in capture.elements
        if element.bounds[1] > header.bounds[3]
        and _label(element).casefold() in _SEARCH_RESULT_SECTIONS
    ), default=int(capture.height * 0.7))
    candidates = [
        element for element in capture.elements
        if element.bounds[1] > header.bounds[3]
        and element.bounds[3] < next_section_y
        and _label(element).casefold() not in _SEARCH_RESULT_SECTIONS
    ]
    return min(candidates, key=lambda element: element.bounds[1], default=None)


def _search_for_chat(
    backend: PhoneBackend,
    capture: CaptureResult,
    chat: str,
) -> ActionResult:
    placeholder = bool(_EMOJI_PLACEHOLDER.search(chat))
    query = chat
    if placeholder:
        # Keep a contiguous text fragment; joining both sides of an emoji can
        # produce a query which is not present in WeChat's name index.
        anchors = [part.strip() for part in _EMOJI_PLACEHOLDER.split(chat)
                   if any(c.isalnum() for c in part)]
        if not anchors:
            return ActionResult(ok=False, action="wechat_open_chat",
                                message="Emoji placeholder has no searchable name text; provide the original name or a unique WeChat remark")
        query = max(anchors, key=len)
    resolved_chat = chat
    search_control = _find_search_control(capture)
    if search_control is None:
        return ActionResult(
            ok=False,
            action="wechat_open_chat",
            message=f"chat {chat!r} is not visible and WeChat search was not found",
            capture=capture,
        )

    opened_search = _run_and_capture(
        backend, "tap", lambda: backend.tap(element=search_control.index),
    )
    if not opened_search.ok or opened_search.capture is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=opened_search.message or "could not open WeChat search",
            capture=opened_search.capture,
        )
    search_page = _settle_capture(
        backend, opened_search.capture, _is_search_page,
    )
    if not _is_search_page(search_page):
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message="WeChat search page did not become ready", capture=search_page,
        )

    typed = _run_and_capture(
        backend, "set_text", lambda: backend.set_text(query),
    )
    if not typed.ok or typed.capture is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=typed.message or "could not enter the WeChat search query",
            capture=typed.capture,
        )

    def find_result(value: CaptureResult) -> Optional[UIElement]:
        sections = sorted((e for e in value.elements
                           if _label(e).casefold() in _SEARCH_RESULT_SECTIONS),
                          key=lambda e: e.bounds[1])

        def is_chat_result(element: UIElement) -> bool:
            if _label(element).casefold() in _SEARCH_RESULT_SECTIONS:
                return False
            preceding = [e for e in sections if e.bounds[1] <= element.bounds[1]]
            # Some hierarchy/OCR frames omit all section labels. Preserve that
            # path; when sections exist, never treat history snippets as names.
            return not sections or bool(
                preceding and _label(preceding[-1]).casefold() in _CHAT_RESULT_SECTIONS
            )

        candidates = [
            element for element in value.elements
            if element.bounds[1] >= max(220, int(value.height * 0.12))
            and element.bounds[3] < value.height - 180
            and is_chat_result(element)
        ]
        matched = _find_text(candidates, chat)
        if matched is not None:
            return matched
        if _is_symbol_only_title(chat):
            return _find_top_hit(value)
        return None

    def desired_chat_is_open(value: CaptureResult) -> bool:
        if _chat_is_open(value, resolved_chat):
            return True
        # Exact search is still trustworthy for a pure-symbol title even when
        # Vision renders the glyph as a letter on both the result and header.
        return (
            _is_symbol_only_title(chat)
            and not _is_search_page(value)
            and _find_input(value.elements) is not None
        )

    results = _settle_capture(
        backend, typed.capture, lambda value: find_result(value) is not None,
    )
    target = find_result(results)
    if target is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=f"WeChat search found no unambiguous chat {chat!r}",
            capture=results,
        )

    if placeholder:
        resolved_chat = _label(target)

    selected = _run_and_capture(
        backend, "tap", lambda: backend.tap(element=target.index),
    )
    if not selected.ok or selected.capture is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=selected.message or f"could not open search result {chat!r}",
            capture=selected.capture,
        )
    selected_capture = selected.capture
    if not desired_chat_is_open(selected_capture) and _is_search_page(selected_capture):
        # OCR can make the query field look like a chat header and can add a
        # synthetic input region to result pages. If the first tap was a no-op,
        # locate the row again from the fresh capture and retry it once.
        retry_target = find_result(selected_capture)
        if retry_target is not None:
            retried = _run_and_capture(
                backend, "tap", lambda: backend.tap(element=retry_target.index),
            )
            if not retried.ok or retried.capture is None:
                return ActionResult(
                    ok=False, action="wechat_open_chat",
                    message=retried.message or f"could not retry search result {chat!r}",
                    capture=retried.capture,
                )
            selected_capture = retried.capture
    selected_capture = _settle_capture(
        backend, selected_capture, desired_chat_is_open,
    )
    if not desired_chat_is_open(selected_capture):
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=f"search result did not open the requested WeChat chat {chat!r}",
            capture=selected_capture,
        )
    _remember_symbol_chat(chat, selected_capture)
    return ActionResult(
        ok=True, action="wechat_open_chat",
        message=f"opened WeChat chat {chat!r} via search",
        capture=selected_capture,
        meta={"resolved_chat": resolved_chat} if placeholder else {},
    )


def _find_send(capture: CaptureResult) -> Optional[UIElement]:
    elements = capture.elements
    labelled = next(
        (element for element in elements if _label(element).casefold() in _SEND_LABELS),
        None,
    )
    if labelled is not None:
        return labelled

    # Host OCR can garble the short label while still returning its text box.
    # The send button sits at the right edge immediately above the keyboard.
    # Use screen-relative bounds so this fallback works across resolutions.
    candidates = [
        element for element in elements
        if element.clickable
        and element.bounds[0] >= int(capture.width * 0.72)
        and int(capture.height * 0.45) <= element.bounds[1]
        < int(capture.height * 0.67)
        and element.bounds[2] <= capture.width
    ]
    return min(candidates, key=lambda element: element.bounds[1], default=None)


def _contains_reply(capture: CaptureResult, text: str) -> bool:
    """Check that the sent reply is visible in the post-send chat capture."""
    elements = capture.elements
    wanted = re.sub(r"\s+", "", text).casefold()
    if not wanted:
        return False

    input_bounds = [
        element.bounds for element in elements
        if element.class_name == _MESSAGE_INPUT_CLASS
    ]

    def overlaps_input(element: UIElement) -> bool:
        left, top, right, bottom = element.bounds
        return any(
            left < input_right
            and right > input_left
            and top < input_bottom
            and bottom > input_top
            for input_left, input_top, input_right, input_bottom in input_bounds
        )

    candidates = []
    for element in elements:
        observed = re.sub(r"\s+", "", _label(element)).casefold()
        if (
            not observed
            or element.class_name == _MESSAGE_INPUT_CLASS
            or element.bounds[1] >= int(capture.height * 0.92)
            or overlaps_input(element)
            or observed in _SEND_LABELS
        ):
            continue
        if wanted in observed:
            return True
        candidates.append((element, observed))

    # Host OCR returns a wrapped outgoing bubble as one element per visual
    # line. Reassemble only adjacent, horizontally overlapping lines so text
    # from unrelated messages cannot satisfy the delivery check.
    candidates.sort(key=lambda item: (item[0].bounds[1], item[0].bounds[0]))
    groups: list[list[tuple[UIElement, str]]] = []
    for candidate in candidates:
        element = candidate[0]
        if not groups:
            groups.append([candidate])
            continue
        previous = groups[-1][-1][0]
        previous_height = max(1, previous.bounds[3] - previous.bounds[1])
        current_height = max(1, element.bounds[3] - element.bounds[1])
        vertical_gap = element.bounds[1] - previous.bounds[3]
        overlap = min(previous.bounds[2], element.bounds[2]) - max(
            previous.bounds[0], element.bounds[0],
        )
        same_block = (
            -min(previous_height, current_height) <= vertical_gap
            <= max(40, previous_height, current_height)
            and overlap >= min(
                previous.bounds[2] - previous.bounds[0],
                element.bounds[2] - element.bounds[0],
            ) * 0.4
        )
        if same_block:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])

    return any(
        wanted in "".join(observed for _, observed in group)
        for group in groups
    )


def _find_voice_input_control(capture: CaptureResult) -> Optional[UIElement]:
    """Find the center control when WeChat is not in keyboard input mode."""
    bottom_threshold = int(capture.height * 0.75)
    for element in capture.elements:
        label = _label(element).casefold()
        if element.bounds[1] < bottom_threshold:
            continue
        if label in _VOICE_INPUT_LABELS or label.startswith(
            _VOICE_TRANSCRIPTION_PREFIXES
        ):
            return element
    return None


def _run_and_capture(
    backend: PhoneBackend,
    action: str,
    operation: Callable[[], ActionResult],
) -> ActionResult:
    """Run one state change and observe its resulting screen even on failure."""
    try:
        result = operation()
    except Exception as exc:
        result = ActionResult(ok=False, action=action, message=str(exc))
    try:
        result.capture = backend.capture(mode="hierarchy")
    except Exception as exc:
        logger.warning("WeChat follow-up OCR failed after %s: %s", action, exc)
        if result.ok:
            result.ok = False
            result.message = f"{action} succeeded but follow-up OCR failed: {exc}"
    return result


def _prepare_text_input(
    backend: PhoneBackend,
    capture: CaptureResult,
) -> ActionResult:
    """Focus WeChat's text field, switching out of either voice mode first."""
    current = capture
    for _ in range(3):
        voice_control = _find_voice_input_control(current)
        if voice_control is None:
            break
        label = _label(voice_control).casefold()
        if label.startswith(_VOICE_TRANSCRIPTION_PREFIXES):
            target_x, target_y = voice_control.center()
        else:
            target_x = int(current.width * 0.052)
            target_y = voice_control.center()[1]
        switched = _run_and_capture(
            backend,
            "tap",
            lambda x=target_x, y=target_y: backend.tap(x=x, y=y),
        )
        if not switched.ok or switched.capture is None:
            return switched
        current = switched.capture

    if _find_voice_input_control(current) is not None:
        return ActionResult(
            ok=False,
            action="tap",
            message="could not switch WeChat from voice input to text input",
            capture=current,
        )

    message_input = _find_input(current.elements)
    if message_input is None:
        return ActionResult(
            ok=False,
            action="tap",
            message="WeChat message input was not found",
            capture=current,
        )

    if current is not capture:
        return ActionResult(ok=True, action="tap", capture=current)

    try:
        focused = backend.tap(element=message_input.index)
    except Exception as exc:
        return ActionResult(
            ok=False, action="tap", message=str(exc), capture=current,
        )
    focused.capture = current
    return focused


def open_chat(backend: PhoneBackend, chat: str) -> ActionResult:
    """Launch WeChat and open one visible conversation using host OCR."""
    chat = chat.strip()
    if not chat:
        return ActionResult(ok=False, action="wechat_open_chat", message="chat is required")

    launch = _run_and_capture(
        backend,
        "launch_app",
        lambda: backend.launch_app(WECHAT_PACKAGE),
    )
    if not launch.ok or launch.capture is None:
        return ActionResult(
            ok=False,
            action="wechat_open_chat",
            message=launch.message or "could not launch WeChat",
            capture=launch.capture,
        )

    capture = launch.capture
    # Android's launcher command can return before WeChat becomes foreground.
    # Never run WeChat recovery gestures against a stale Launcher screenshot.
    for _ in range(3):
        if not capture.current_package or capture.current_package == WECHAT_PACKAGE:
            break
        try:
            capture = backend.capture(mode="hierarchy")
        except Exception as exc:
            logger.warning("WeChat foreground wait capture failed: %s", exc)
            break
    if capture.current_package and capture.current_package != WECHAT_PACKAGE:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message="WeChat did not become the foreground app after launch",
            capture=capture,
        )

    if _EMOJI_PLACEHOLDER.search(chat):
        # Search all returned candidates even when one matching list row or
        # current conversation is visible: the lost emoji is not an identity.
        if not _is_conversation_list(capture):
            recovered = _return_to_conversation_list(backend, capture)
            if not recovered.ok or recovered.capture is None:
                return recovered
            capture = recovered.capture
        return _search_for_chat(backend, capture, chat)

    input_element = _find_input(capture.elements)
    if input_element is not None and _find_chat_header(capture, chat) is not None:
        return ActionResult(
            ok=True,
            action="wechat_open_chat",
            message=f"opened WeChat chat {chat!r}",
            capture=capture,
        )

    # A cold/resumed LauncherUI may expose only a handful of OCR elements in
    # its first frame. Wait for enough structure to identify a chat or list.
    for _ in range(2):
        list_elements = [
            element for element in capture.elements
            if element.bounds[1] >= max(220, int(capture.height * 0.12))
            and element.bounds[3] < capture.height - 180
        ]
        if input_element is not None or _find_text(list_elements, chat) is not None:
            break
        if _is_search_page(capture) or _find_conversation_tab(capture) is not None:
            break
        if len(capture.elements) >= 10:
            break
        try:
            capture = backend.capture(mode="hierarchy")
        except Exception as exc:
            logger.warning("WeChat launch settle capture failed: %s", exc)
            break
        input_element = _find_input(capture.elements)
        if input_element is not None and _find_chat_header(capture, chat) is not None:
            return ActionResult(
                ok=True,
                action="wechat_open_chat",
                message=f"opened WeChat chat {chat!r}",
                capture=capture,
            )

    # WeChat often exposes a partially rendered chat frame while LauncherUI
    # resumes. Never navigate away from a chat based on that transient title.
    if input_element is not None:
        try:
            settled = backend.capture(mode="hierarchy")
        except Exception as exc:
            logger.warning("WeChat settle capture failed: %s", exc)
        else:
            capture = settled
            input_element = _find_input(capture.elements)
            if input_element is not None and _find_chat_header(capture, chat) is not None:
                return ActionResult(
                    ok=True,
                    action="wechat_open_chat",
                    message=f"opened WeChat chat {chat!r}",
                    capture=capture,
                )

    list_elements = [
        element for element in capture.elements
        if element.bounds[1] >= max(220, int(capture.height * 0.12))
        and element.bounds[3] < capture.height - 180
    ]
    target = _find_text(list_elements, chat)
    page_needs_recovery = (
        _is_search_page(capture)
        or _find_input(capture.elements) is not None
        or _find_conversation_tab(capture) is not None
    )
    if not _is_conversation_list(capture) and (page_needs_recovery or target is None):
        recovered = _return_to_conversation_list(backend, capture)
        if not recovered.ok or recovered.capture is None:
            return recovered
        capture = recovered.capture

    # Only use a fresh list-row match. A message body or notification preview
    # with the same text must never be treated as a conversation target.
    list_elements = [
        element for element in capture.elements
        if element.bounds[1] >= max(220, int(capture.height * 0.12))
        and element.bounds[3] < capture.height - 180
    ]
    target = _find_text(list_elements, chat)
    if target is None:
        return _search_for_chat(backend, capture, chat)

    opened = _run_and_capture(
        backend,
        "tap",
        lambda: backend.tap(element=target.index),
    )
    if not opened.ok or opened.capture is None:
        return ActionResult(
            ok=False,
            action="wechat_open_chat",
            message=opened.message or f"could not open chat {chat!r}",
            capture=opened.capture,
        )
    opened_capture = opened.capture
    for _ in range(2):
        if (
            _find_chat_header(opened_capture, chat) is not None
            and _find_input(opened_capture.elements) is not None
        ):
            break
        try:
            opened_capture = backend.capture(mode="hierarchy")
        except Exception as exc:
            logger.warning("WeChat open-chat settle capture failed: %s", exc)
            break
    if _find_chat_header(opened_capture, chat) is None:
        return ActionResult(
            ok=False,
            action="wechat_open_chat",
            message=f"tap did not open the requested WeChat chat {chat!r}",
            capture=opened_capture,
        )
    if _find_input(opened_capture.elements) is None:
        return ActionResult(
            ok=False,
            action="wechat_open_chat",
            message=f"opened {chat!r}, but the WeChat message input was not found",
            capture=opened_capture,
        )
    return ActionResult(
        ok=True,
        action="wechat_open_chat",
        message=f"opened WeChat chat {chat!r}",
        capture=opened_capture,
    )


def _reply_once(backend: PhoneBackend, chat: str, text: str) -> ActionResult:
    opened = open_chat(backend, chat)
    if not opened.ok or opened.capture is None:
        return ActionResult(
            ok=False, action="wechat_reply", message=opened.message,
            capture=opened.capture,
        )

    focused = _prepare_text_input(backend, opened.capture)
    if not focused.ok or focused.capture is None:
        return ActionResult(
            ok=False, action="wechat_reply", message=focused.message,
            capture=focused.capture,
        )

    typed = _run_and_capture(backend, "set_text", lambda: backend.set_text(text))
    if not typed.ok or typed.capture is None:
        return ActionResult(
            ok=False, action="wechat_reply", message=typed.message,
            capture=typed.capture,
        )

    typed.capture = _settle_capture(
        backend, typed.capture,
        lambda value: _find_send(value) is not None,
    )
    send = _find_send(typed.capture)
    if send is None:
        return ActionResult(
            ok=False, action="wechat_reply",
            message="WeChat send button was not found after entering the reply",
            capture=typed.capture,
        )

    sent = _run_and_capture(backend, "tap", lambda: backend.tap(element=send.index))
    if not sent.ok or sent.capture is None:
        return ActionResult(
            ok=False, action="wechat_reply",
            message=sent.message or "WeChat send action failed",
            capture=sent.capture,
            meta={"delivery_attempted": True, "delivery_status": "uncertain"},
        )

    # ADB reports success when it injects a tap, even if WeChat drops that
    # input while the keyboard or composer is still transitioning. Only retry
    # when fresh captures keep showing the Send button: after a real send the
    # draft clears and that button disappears. Use its current coordinates so
    # a refreshed OCR element index cannot point at a different control.
    if not _contains_reply(sent.capture, text) and _find_send(sent.capture) is not None:
        sent.capture = _settle_capture(
            backend,
            sent.capture,
            lambda value: (
                _contains_reply(value, text) or _find_send(value) is None
            ),
            attempts=2,
        )
        retry_send = _find_send(sent.capture)
        if not _contains_reply(sent.capture, text) and retry_send is not None:
            retry_x, retry_y = retry_send.center()
            logger.warning(
                "WeChat Send remained visible after tap; retrying at (%d, %d)",
                retry_x,
                retry_y,
            )
            sent = _run_and_capture(
                backend,
                "tap",
                lambda: backend.tap(x=retry_x, y=retry_y),
            )
            if not sent.ok or sent.capture is None:
                return ActionResult(
                    ok=False,
                    action="wechat_reply",
                    message=sent.message or "WeChat send retry failed",
                    capture=sent.capture,
                    meta={
                        "delivery_attempted": True,
                        "delivery_status": "uncertain",
                    },
                )

    sent.capture = _settle_capture(
        backend, sent.capture,
        lambda value: _contains_reply(value, text),
    )
    if _contains_reply(sent.capture, text):
        return ActionResult(
            ok=True, action="wechat_reply",
            message=f"sent reply to WeChat chat {chat!r}", capture=sent.capture,
            meta={"delivery_attempted": True, "delivery_status": "confirmed"},
        )

    return ActionResult(
        ok=False, action="wechat_reply",
        message=(
            "WeChat send was attempted, but delivery could not be confirmed; "
            "the reply was not retried to avoid a duplicate"
        ),
        capture=sent.capture,
        meta={"delivery_attempted": True, "delivery_status": "uncertain"},
    )


def reply(backend: PhoneBackend, chat: str, text: str) -> ActionResult:
    """Open a WeChat chat, recover once if needed, and always return Home."""
    result: ActionResult = ActionResult(
        ok=False, action="wechat_reply", message="WeChat reply did not run"
    )
    try:
        for attempt in range(2):
            try:
                result = _reply_once(backend, chat, text)
            except Exception as exc:
                logger.exception("WeChat reply attempt %d failed", attempt + 1)
                result = ActionResult(
                    ok=False, action="wechat_reply", message=str(exc)
                )
            if result.ok:
                return result
            if result.meta.get("delivery_attempted"):
                return result
            if attempt == 0:
                logger.warning(
                    "WeChat reply recovery: retrying chat=%r after: %s",
                    chat, result.message,
                )
                # open_chat() owns recovery and verifies that it reached the
                # conversation list. Avoid an unverified Back here: on a
                # search page it often only dismisses the keyboard, leaving a
                # stale query that the next paste can append to.
        result.message = f"after 2 attempts: {result.message}"
        return result
    except Exception as exc:
        logger.exception("WeChat reply workflow failed")
        result = ActionResult(
            ok=False, action="wechat_reply", message=f"WeChat reply failed: {exc}"
        )
        return result
    finally:
        home = _run_and_capture(
            backend,
            "keyevent",
            lambda: backend.keyevent("HOME"),
        )
        if "result" in locals():
            result.capture = home.capture or result.capture
            if not home.ok and not result.meta.get("delivery_attempted"):
                result.ok = False
                result.message = (
                    f"{result.message}; could not return to Home: {home.message}"
                )
            elif not home.ok:
                result.meta["home_cleanup_failed"] = True
                result.message = (
                    f"{result.message}; delivery was not retried after Home "
                    f"cleanup failed: {home.message}"
                )
        _clear_symbol_chat_hint(chat)


def favorite_message(backend: PhoneBackend, chat: str, original: str) -> ActionResult:
    """Favorite one exact visible text message; never guess a duplicate target."""
    result = ActionResult(ok=False, action='wechat_favorite')
    attempted = False
    try:
        if not isinstance(original, str) or not original.strip() or len(original) > 2000:
            result.message = 'message_text must contain 1–2000 characters'
            return result
        opened = open_chat(backend, chat)
        if not opened.ok:
            result.message = opened.message
            return result
        resolved = opened.meta.get('resolved_chat', chat)
        current = backend.capture(mode='hierarchy')
        entry = _find_input(current.elements)
        if current.current_package != WECHAT_PACKAGE or not _chat_is_open(current, resolved) or entry is None:
            result.message = 'Chat changed before favorite selection'
            return result
        matches = [e for e in current.elements if e.enabled and _label(e) == original.strip()
                   and current.height * .12 <= e.bounds[1] and e.bounds[3] < entry.bounds[1]]
        if len(matches) != 1:
            result.message = 'Original not visible or ambiguous; use a unique exact text anchor'
            return result
        pressed = _run_and_capture(backend, 'long_press',
                                   lambda: backend.long_press(element=matches[0].index, duration_ms=700))
        if not pressed.ok or pressed.capture is None:
            result.message = 'Could not open message menu'
            return result
        current = pressed.capture
        labels = {_label(e) for e in current.elements}
        success_labels = {'已收藏', '收藏成功', 'Added to Favorites', 'Saved to Favorites'}
        choices = [e for e in current.elements if e.enabled and _label(e) in {'收藏', 'Favorite', 'Favorites'}]
        if (current.current_package != WECHAT_PACKAGE or not _chat_is_open(current, resolved)
                or original.strip() not in labels or len(choices) != 1 or labels.intersection(success_labels)):
            result.message = 'Favorite menu/target could not be verified'
            return result
        attempted = True
        tapped = _run_and_capture(backend, 'tap', lambda: backend.tap(element=choices[0].index))
        result.message = 'Favorite attempted; completion uncertain, do not automatically repeat'
        if tapped.ok and tapped.capture is not None:
            current = _settle_capture(backend, tapped.capture, lambda page:
                page.current_package == WECHAT_PACKAGE and any(_label(e) in success_labels for e in page.elements))
            if (current.current_package == WECHAT_PACKAGE and _chat_is_open(current, resolved)
                    and any(_label(e) in success_labels for e in current.elements)):
                result.ok = True
                result.message = 'WeChat confirmed message added to Favorites'
        return result
    except Exception as exc:
        result.message = f'Favorite workflow failed: {exc}'
        return result
    finally:
        result.meta.update(favorite_attempted=attempted,
                           favorite_status='confirmed' if result.ok else 'uncertain' if attempted else 'not_attempted')
        try:
            backend.keyevent('HOME')
        except Exception:
            result.meta['home_cleanup_failed'] = True


def accept_friend_request(backend: PhoneBackend, requester: str) -> ActionResult:
    """Accept one named WeChat friend request and always return Home."""
    requester = requester.strip()
    result = ActionResult(
        ok=False,
        action="wechat_accept_friend",
        message="WeChat friend request action did not run",
        meta={"requester": requester},
    )
    try:
        if not requester:
            result.message = "requester is required"
            return result

        launched = _run_and_capture(
            backend,
            "launch_app",
            lambda: backend.launch_app(WECHAT_PACKAGE),
        )
        if not launched.ok or launched.capture is None:
            result.message = launched.message or "could not launch WeChat"
            result.capture = launched.capture
            return result
        current = launched.capture

        request_row = _find_text(current.elements, requester)
        accept = (
            _find_row_action(current, request_row, _ACCEPT_LABELS)
            if request_row is not None else None
        )
        if accept is None:
            contacts = _find_label(
                current,
                _CONTACT_LABELS,
                min_y=int(current.height * 0.75),
            )
            for _ in range(3):
                if contacts is not None:
                    break
                backed = _run_and_capture(
                    backend, "keyevent", lambda: backend.keyevent("BACK"),
                )
                if not backed.ok or backed.capture is None:
                    result.message = backed.message or "could not reach WeChat navigation"
                    result.capture = backed.capture
                    return result
                current = backed.capture
                contacts = _find_label(
                    current,
                    _CONTACT_LABELS,
                    min_y=int(current.height * 0.75),
                )
            if contacts is None:
                result.message = "WeChat Contacts tab was not found"
                result.capture = current
                return result

            opened_contacts = _run_and_capture(
                backend, "tap", lambda: backend.tap(element=contacts.index),
            )
            if not opened_contacts.ok or opened_contacts.capture is None:
                result.message = opened_contacts.message or "could not open WeChat Contacts"
                result.capture = opened_contacts.capture
                return result
            current = _settle_capture(
                backend,
                opened_contacts.capture,
                lambda capture: _find_label(capture, _NEW_FRIENDS_LABELS) is not None,
            )
            new_friends = _find_label(current, _NEW_FRIENDS_LABELS)
            if new_friends is None:
                result.message = "WeChat New Friends entry was not found"
                result.capture = current
                return result

            opened_requests = _run_and_capture(
                backend, "tap", lambda: backend.tap(element=new_friends.index),
            )
            if not opened_requests.ok or opened_requests.capture is None:
                result.message = opened_requests.message or "could not open New Friends"
                result.capture = opened_requests.capture
                return result
            current = _settle_capture(
                backend,
                opened_requests.capture,
                lambda capture: _find_text(capture.elements, requester) is not None,
            )
            request_row = _find_text(current.elements, requester)
            if request_row is None:
                result.message = f"friend request from {requester!r} was not found"
                result.capture = current
                return result
            accept = _find_row_action(current, request_row, _ACCEPT_LABELS)

        if accept is None or request_row is None:
            result.message = f"Accept button for {requester!r} was not found"
            result.capture = current
            return result

        accepted = _run_and_capture(
            backend, "tap", lambda: backend.tap(element=accept.index),
        )
        if not accepted.ok or accepted.capture is None:
            result.message = accepted.message or "WeChat Accept action failed"
            result.capture = accepted.capture
            return result
        current = _settle_capture(
            backend,
            accepted.capture,
            lambda capture: (
                (row := _find_text(capture.elements, requester)) is not None
                and (
                    _find_row_action(capture, row, _ADDED_LABELS) is not None
                    or _find_row_action(capture, row, _ACCEPT_LABELS) is None
                )
            ),
        )
        request_row = _find_text(current.elements, requester)
        if request_row is None or _find_row_action(current, request_row, _ACCEPT_LABELS):
            result.message = f"accepting friend request from {requester!r} could not be confirmed"
            result.capture = current
            return result

        result = ActionResult(
            ok=True,
            action="wechat_accept_friend",
            message=f"accepted WeChat friend request from {requester!r}",
            capture=current,
            meta={"requester": requester},
        )
        return result
    except Exception as exc:
        logger.exception("WeChat friend request action failed")
        result.message = f"accepting WeChat friend request failed: {exc}"
        return result
    finally:
        home = _run_and_capture(
            backend, "keyevent", lambda: backend.keyevent("HOME"),
        )
        result.capture = home.capture or result.capture
        if not home.ok:
            result.ok = False
            result.message = f"{result.message}; could not return to Home: {home.message}"
