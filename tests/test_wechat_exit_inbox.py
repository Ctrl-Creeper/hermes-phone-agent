"""Public behavior for the conservative end-of-chat WeChat inbox scan."""

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


def test_exit_inbox_group_requires_exact_mention_and_never_accepts_quote_summary():
    baseline = capture_exit_inbox_baseline(capture(message(1, "older", 700)))
    found = find_exit_inbox_messages(baseline, capture(
        message(1, "older", 500),
        message(2, "Alice: @Void_DRSAI old quoted request", 800,
                side="incoming"),
        message(3, "please @Void_DRSAI check this", 1200),
    ), conversation_type="group")
    assert [item["text"] for item in found] == ["please @Void_DRSAI check this"]


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
    adapter._on_raw_event = lambda event: received.append(event)
    adapter._register_exit_inbox_callback()
    try:
        wechat._exit_inbox_callback("Example Group", "group", [
            {"text": "@Void_DRSAI please review"},
        ])
    finally:
        wechat.set_exit_inbox_callback(None)

    assert len(received) == 1
    event = received[0]
    assert event.meta["_transport"] == "exit_inbox"
    assert event.meta["conversation_type"] == "group"
    assert event.title == "Example Group"


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
