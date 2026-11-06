"""Opening the WeChat chat while the model decides, without a device."""
from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from plugins.phone_events import adapter
from plugins.phone_events.event_filter import PhoneEvent
from plugins.phone_events.participation import ConversationBatch
from plugins.phone_use import tool, wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.policy import PolicyDecision

# Captured before the autouse conftest stub replaces it.
_REAL_PREOPEN = tool.preopen_wechat_chat

CHAT = "Example Group"
# The turn decision a participation batch gets: trusted automatic reply.
TRUSTED = PolicyDecision(behavior="auto", instruction_source=True,
                         allowed_actions=frozenset({"wechat_reply", "wechat_collect_context"}))


def _open_chat_page() -> CaptureResult:
    return CaptureResult(
        mode="hierarchy", width=1080, height=2400, current_package="com.tencent.mm",
        elements=[UIElement(index=1, class_name="host.ocr.Text", text=CHAT, bounds=(320, 104, 704, 157)),
                  UIElement(index=2, class_name="host.ocr.MessageInput", bounds=(110, 2165, 720, 2325))],
    )


class Backend:
    def __init__(self):
        self.keys = []

    def keyevent(self, key):
        self.keys.append(key)
        return ActionResult(ok=True, action="keyevent")

    def capture(self, mode="hierarchy"):
        return _open_chat_page()


@pytest.fixture
def phone(monkeypatch):
    backend = Backend()
    opened = []

    def open_chat(_backend, chat):
        opened.append(chat)
        return ActionResult(ok=True, action="wechat_open_chat", message="opened")

    full = []

    def full_navigation(_backend, chat):
        full.append(chat)
        return ActionResult(ok=False, action="wechat_open_chat", message="full navigation failed")

    monkeypatch.setattr(tool, "_get_backend", lambda: backend)
    monkeypatch.setattr(tool, "open_visible_wechat_chat", open_chat)
    monkeypatch.setattr(tool, "open_wechat_chat", full_navigation)
    monkeypatch.setattr(wechat, "_open_reply_chat", None)
    yield SimpleNamespace(backend=backend, opened=opened, full=full)
    wechat._open_reply_chat = None


def _preopen_and_wait(chat=CHAT):
    preopen = _REAL_PREOPEN(chat, TRUSTED)
    preopen._thread.join(5)
    return preopen


def test_reply_reuses_the_preopened_chat_and_leaves_cleanup_to_the_turn(phone):
    preopen = _preopen_and_wait()

    reused = wechat._reusable_open_chat(phone.backend, CHAT)  # what wechat_reply does first
    preopen.finish()

    assert phone.opened == [CHAT]
    assert reused is not None and reused.ok and reused.message == "reused open chat"
    assert phone.backend.keys == []  # the reply turn owns its own Home cleanup


def test_unused_preopen_returns_home_when_the_model_stays_silent(phone):
    preopen = _preopen_and_wait()

    preopen.finish()

    assert phone.backend.keys == ["HOME"]
    assert wechat._open_reply_chat is None  # no stale hint for a later reply


def test_preopen_never_starts_once_the_turn_has_ended(phone):
    # Another task owns the phone, so the pre-open waits in the device queue.
    tool._device_operation_queue._owner = "other-task"
    try:
        preopen = _REAL_PREOPEN(CHAT, TRUSTED)
        preopen.finish()  # the silent turn ends before the phone is free
    finally:
        tool._device_operation_queue.release("other-task")
    preopen._thread.join(5)

    assert not preopen._thread.is_alive()
    assert phone.opened == [] and phone.backend.keys == []


def test_cleanup_does_not_undo_a_newer_reply_or_preopen(phone):
    preopen = _preopen_and_wait()
    newer = wechat.remember_open_chat("Another Chat")  # e.g. a reply to another chat in the same turn

    preopen.finish()

    assert phone.backend.keys == []
    assert wechat._open_reply_chat is newer


def test_preopen_respects_app_policy_blocking_replies(phone, monkeypatch):
    blocked = PolicyDecision(blocked_actions=frozenset({"wechat_reply"}))
    monkeypatch.setattr(tool, "get_policy", lambda: SimpleNamespace(check_action=lambda *a: blocked))

    _preopen_and_wait().finish()

    assert phone.opened == [] and phone.backend.keys == []


def test_failed_open_leaves_no_hint_and_returns_home(phone, monkeypatch):
    monkeypatch.setattr(tool, "open_visible_wechat_chat",
                        lambda b, chat: ActionResult(ok=False, action="wechat_open_chat", message="not found"))

    _preopen_and_wait().finish()

    assert wechat._open_reply_chat is None
    assert phone.backend.keys == ["HOME"]  # no half-navigated WeChat left behind


# ── adapter gating ───────────────────────────────────────────────────────

def _batch(conversation_type, invited):
    event = PhoneEvent("notification", "com.tencent.mm", title=CHAT, meta={"conversation_title": CHAT})
    return ConversationBatch(lane="lane", events=(event,), decisions=(), invited=invited,
                             metrics={}, conversation_type=conversation_type)


@pytest.mark.parametrize("conversation_type, invited, expected", [
    ("group", True, True),     # replied to 95% of the time
    ("private", False, True),  # ~66%
    ("group", False, False),   # 8%: not worth marking chatter read
])
def test_only_likely_replies_are_preopened_and_finished_after_the_turn(monkeypatch, conversation_type, invited, expected):
    monkeypatch.setitem(sys.modules, "gateway.session",
                        SimpleNamespace(build_session_key=lambda *a, **kw: "lane"))
    events = []

    class Preopen:
        def finish(self):
            events.append("finish")

    def preopen(chat, decision):
        events.append(("preopen", chat))
        assert decision is TRUSTED  # the turn's own policy reaches the gate
        return Preopen()

    monkeypatch.setattr(tool, "preopen_wechat_chat", preopen)
    batch = _batch(conversation_type, invited)

    async def scenario():
        class Gateway:
            config = SimpleNamespace(extra={})
            _session_tasks = {}

            async def handle_message(self, message):
                async def turn():
                    events.append("turn")
                self._session_tasks["lane"] = asyncio.create_task(turn())

        await adapter._dispatch_with_event_policy(
            Gateway(), SimpleNamespace(channel_prompt=None, source=object()), TRUSTED,
            phone_event=PhoneEvent("notification", meta={"_participation_batch": batch}))

    asyncio.run(scenario())
    assert events == ([("preopen", CHAT), "turn", "finish"] if expected else ["turn"])


def test_unused_preopen_does_not_press_home_on_a_paused_phone(phone):
    from plugins.phone_use.pause import pause_gate

    preopen = _preopen_and_wait()
    pause_gate.pause()  # the owner took over the phone
    try:
        preopen.finish()
    finally:
        pause_gate.resume()

    assert phone.backend.keys == []
    assert wechat._open_reply_chat is None


@pytest.mark.parametrize("decision", [
    None,
    PolicyDecision(behavior="report", instruction_source=True),
    PolicyDecision(behavior="auto"),  # automatic, but no trusted instruction source
    PolicyDecision(behavior="auto", instruction_source=True,
                   allowed_actions=frozenset({"wechat_collect_context"})),
    PolicyDecision(behavior="auto", instruction_source=True,
                   blocked_actions=frozenset({"wechat_reply"})),
])
def test_preopen_requires_the_turn_to_be_allowed_to_reply(phone, decision):
    assert _REAL_PREOPEN(CHAT, decision) is None
    assert phone.opened == []


def test_open_finishing_after_finish_gave_up_cleans_up_itself(phone, monkeypatch):
    release = threading.Event()

    def slow_open(_backend, chat):
        release.wait(5)
        phone.opened.append(chat)
        return ActionResult(ok=True, action="wechat_open_chat", message="opened")

    monkeypatch.setattr(tool, "open_visible_wechat_chat", slow_open)
    preopen = _REAL_PREOPEN(CHAT, TRUSTED)
    while not preopen._started:
        threading.Event().wait(0.01)

    preopen.finish(timeout=0.05)  # the turn stops waiting for a slow open
    release.set()
    preopen._thread.join(5)

    assert phone.opened == [CHAT]
    assert wechat._open_reply_chat is None  # no hint a later reply could trust
    assert phone.backend.keys == ["HOME"]


def _fast_path_fails(monkeypatch):
    monkeypatch.setattr(tool, "open_visible_wechat_chat",
                        lambda b, chat: ActionResult(ok=False, action="wechat_open_chat", message="not on the list"))


def test_failed_fast_path_navigates_fully_before_the_model_replies(phone, monkeypatch):
    _fast_path_fails(monkeypatch)
    monkeypatch.setattr(tool, "open_wechat_chat",
                        lambda b, chat: (phone.full.append(chat), ActionResult(ok=True, action="wechat_open_chat"))[1])
    preopen = _preopen_and_wait()

    reused = wechat._reusable_open_chat(phone.backend, CHAT)  # the reply's first step
    preopen.finish()

    assert phone.full == [CHAT]
    assert reused is not None and reused.ok and phone.backend.keys == []


def test_full_navigation_failure_returns_home(phone, monkeypatch):
    _fast_path_fails(monkeypatch)

    _preopen_and_wait().finish()

    assert phone.full == [CHAT]
    assert wechat._open_reply_chat is None and phone.backend.keys == ["HOME"]


def test_no_full_navigation_once_the_turn_has_ended(phone, monkeypatch):
    # The model already stayed silent while the fast path ran: do not navigate for nothing.
    release = threading.Event()
    started = threading.Event()

    def slow_fail(b, chat):
        started.set(); release.wait(5)
        return ActionResult(ok=False, action="wechat_open_chat", message="not on the list")

    monkeypatch.setattr(tool, "open_visible_wechat_chat", slow_fail)
    preopen = _REAL_PREOPEN(CHAT, TRUSTED)
    started.wait(5)
    finisher = threading.Thread(target=preopen.finish)
    finisher.start()
    while not preopen._finished:
        threading.Event().wait(0.01)
    release.set(); preopen._thread.join(5); finisher.join(5)

    assert phone.full == []
    assert phone.backend.keys == ["HOME"]
