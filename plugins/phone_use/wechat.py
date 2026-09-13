"""Deterministic WeChat flows built on the phone backend."""

from __future__ import annotations

import logging
import re
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


def _label(element: UIElement) -> str:
    return (element.text or element.content_desc or "").strip()


def _find_text(elements: list[UIElement], text: str) -> Optional[UIElement]:
    wanted = _normalize_chat_title(text)
    exact = [
        element for element in elements
        if _normalize_chat_title(_label(element)) == wanted
    ]
    if exact:
        return exact[0]

    # Short names are too collision-prone for fuzzy matching. For longer chat
    # titles, accept OCR substitutions only when one candidate is clearly best.
    if len(wanted) < 5:
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


def _find_chat_header(capture: CaptureResult, chat: str) -> Optional[UIElement]:
    """Find the conversation title, as opposed to a list row or message body."""
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
            and SequenceMatcher(None, wanted, observed).ratio()
            >= _MIN_TITLE_SIMILARITY
        ):
            return element
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
    return (
        "search local or internet results" in labels
        or "搜索本地或互联网结果" in labels
        or "group chats" in labels
        or "群聊" in labels
    )


def _settle_capture(
    backend: PhoneBackend,
    capture: CaptureResult,
    predicate: Callable[[CaptureResult], bool],
    attempts: int = 3,
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


def _chat_is_open(capture: CaptureResult, chat: str) -> bool:
    return (
        _find_chat_header(capture, chat) is not None
        and _find_input(capture.elements) is not None
    )


def _search_for_chat(
    backend: PhoneBackend,
    capture: CaptureResult,
    chat: str,
) -> ActionResult:
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
        backend, "set_text", lambda: backend.set_text(chat),
    )
    if not typed.ok or typed.capture is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=typed.message or "could not enter the WeChat search query",
            capture=typed.capture,
        )

    def find_result(value: CaptureResult) -> Optional[UIElement]:
        candidates = [
            element for element in value.elements
            if element.bounds[1] >= max(220, int(value.height * 0.12))
            and element.bounds[3] < value.height - 180
        ]
        return _find_text(candidates, chat)

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

    selected = _run_and_capture(
        backend, "tap", lambda: backend.tap(element=target.index),
    )
    if not selected.ok or selected.capture is None:
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=selected.message or f"could not open search result {chat!r}",
            capture=selected.capture,
        )
    selected_capture = _settle_capture(
        backend, selected.capture, lambda value: _chat_is_open(value, chat),
    )
    if not _chat_is_open(selected_capture, chat):
        return ActionResult(
            ok=False, action="wechat_open_chat",
            message=f"search result did not open the requested WeChat chat {chat!r}",
            capture=selected_capture,
        )
    return ActionResult(
        ok=True, action="wechat_open_chat",
        message=f"opened WeChat chat {chat!r} via search",
        capture=selected_capture,
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

    return _run_and_capture(
        backend,
        "tap",
        lambda: backend.tap(element=message_input.index),
    )


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

    if input_element is not None:
        back = _run_and_capture(
            backend,
            "keyevent",
            lambda: backend.keyevent("BACK"),
        )
        if not back.ok or back.capture is None:
            return ActionResult(
                ok=False,
                action="wechat_open_chat",
                message=back.message or "could not return to WeChat conversation list",
                capture=back.capture,
            )
        capture = back.capture
        capture = _settle_capture(backend, capture, _is_conversation_list)
        if not _is_conversation_list(capture):
            return ActionResult(
                ok=False,
                action="wechat_open_chat",
                message="WeChat conversation list did not become ready after Back",
                capture=capture,
            )

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
                # Reset transient search/chat state before the second lookup.
                try:
                    backend.keyevent("BACK")
                except Exception:
                    logger.debug("WeChat recovery Back failed", exc_info=True)
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
