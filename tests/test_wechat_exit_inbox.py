"""Public behavior for the conservative end-of-chat WeChat inbox scan."""

import re
import sys
import types

from plugins.phone_use.backend import CaptureResult, UIElement
from plugins.phone_use.wechat import capture_exit_inbox_baseline, find_exit_inbox_messages


def message(index, text, y, *, side="incoming"):
    left, right = (80, 700) if side == "incoming" else (480, 1030)
    return UIElement(index=index, class_name="host.ocr.Text", text=text,
                     bounds=(left, y, right, y + 70), clickable=True,
                     attributes={"message_direction": side})


def capture(*items):
    return CaptureResult(mode="hierarchy", width=1080, height=2400,
                         current_package="com.tencent.mm", elements=[
                             UIElement(index=90, class_name="host.ocr.MessageInput",
                                       bounds=(100, 2050, 720, 2190)),
                             *items,
                         ])


def test_exit_inbox_only_returns_new_incoming_private_messages():
    baseline = capture_exit_inbox_baseline(capture(
        message(1, "earlier", 700), message(2, "our reply", 1000, side="outgoing"),
    ))
    found = find_exit_inbox_messages(baseline, capture(
        message(1, "earlier", 500), message(2, "our reply", 800, side="outgoing"),
        message(3, "new private request", 1200),
    ), conversation_type="private")
    assert [item["text"] for item in found] == ["new private request"]


def test_exit_inbox_group_defers_new_bodies_for_configured_policy_and_skips_quote_summary():
    baseline = capture_exit_inbox_baseline(capture(message(1, "older", 700)))
    found = find_exit_inbox_messages(baseline, capture(
        message(1, "older", 500),
        message(2, "Alice: @ExampleBot old quoted request", 800,
                side="incoming"),
        message(3, "please @ExampleBot check this", 1200),
        message(4, "another group message", 1400),
    ), conversation_type="group")
    assert [item["text"] for item in found] == [
        "please @ExampleBot check this", "another group message",
    ]


def test_exit_inbox_group_trigger_comes_from_event_policy():
    from plugins.phone_use.policy import EventRule, PhonePolicy

    policy = PhonePolicy(event_rules=[
        EventRule(package="com.tencent.mm", event_type="notification",
                  conversation_type="group", body_regex=re.compile(r"@ExampleBot\b", re.I),
                  behavior="auto", instruction_source=True, priority=2),
        EventRule(package="com.tencent.mm", event_type="notification",
                  conversation_type="group", behavior="ignore", priority=1),
    ])
    baseline = capture_exit_inbox_baseline(capture())
    messages = find_exit_inbox_messages(baseline, capture(
        message(1, "plain group message", 700),
        message(2, "@ExampleBot please check", 900),
    ), conversation_type="group")
    decisions = [policy.evaluate_event(package="com.tencent.mm", event_type="notification",
                                       title="Example Group", body=item["text"],
                                       conversation_type="group") for item in messages]
    assert [item["text"] for item, decision in zip(messages, decisions)
            if decision.is_auto and decision.instruction_source] == ["@ExampleBot please check"]


def test_exit_inbox_fails_closed_without_direction_or_known_conversation_type():
    baseline = capture_exit_inbox_baseline(capture())
    unknown_side = UIElement(index=1, class_name="host.ocr.Text", text="hello",
                             bounds=(250, 900, 800, 970), clickable=True)
    final = capture(unknown_side)
    assert find_exit_inbox_messages(baseline, final, conversation_type="private") == []
    assert find_exit_inbox_messages(baseline, final, conversation_type="unknown") == []


def test_exit_inbox_callback_reenters_the_normal_phone_event_policy(monkeypatch):
    from plugins.phone_events.adapter import PhoneEventAdapter
    from plugins.phone_use import wechat

    received = []
    adapter = object.__new__(PhoneEventAdapter)
    adapter._target_thread_id = ""
    adapter._target_chat_type = "private"
    adapter._target_chat_id = "123456789"
    adapter._target_user_name = "Example"
    adapter._target_user_id = "123456789"
    adapter._on_raw_event = lambda event: received.append(event)
    adapter._register_exit_inbox_callback()
    try:
        wechat._exit_inbox_callback("Example Group", "group", [
            {"text": "@ExampleBot please review"},
        ])
    finally:
        wechat.set_exit_inbox_callback(None)

    assert len(received) == 1
    event = received[0]
    assert event.meta["_transport"] == "exit_inbox"
    assert event.meta["conversation_type"] == "group"
    assert event.title == "Example Group"
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    config = types.ModuleType("gateway.config")
    config.Platform = types.SimpleNamespace(TELEGRAM="telegram")
    session = types.ModuleType("gateway.session")
    session.SessionSource = types.SimpleNamespace
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", config)
    monkeypatch.setitem(sys.modules, "gateway.session", session)
    assert adapter._source_for_event(event).role_authorized


def test_reply_scans_once_before_home_and_defers_new_private_message(monkeypatch):
    from plugins.phone_use import wechat
    from plugins.phone_use.backend import ActionResult

    baseline = capture_exit_inbox_baseline(capture(message(1, "older", 700)))
    deferred = []
    monkeypatch.setattr(
        wechat, "_reply_once",
        lambda backend, chat, text, **_kwargs: ActionResult(
            ok=True, action="wechat_reply", message="sent",
            meta={"_exit_inbox_baseline": baseline},
        ),
    )
    wechat.set_exit_inbox_callback(lambda chat, kind, messages: deferred.extend(messages))

    class Backend:
        def capture(self, mode):
            return capture(message(1, "older", 500), message(2, "later", 1000))

        def keyevent(self, key):
            return ActionResult(ok=True, action="keyevent")

    try:
        result = wechat.reply(Backend(), "Example Chat", "reply",
                              exit_inbox_conversation_type="private")
    finally:
        wechat.set_exit_inbox_callback(None)

    assert result.ok
    assert result.meta["exit_inbox_count"] == 1
    assert deferred == [{"text": "later", "direction": "incoming"}]
