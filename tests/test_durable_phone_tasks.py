"""Task lifetime and disk receipts, without contacting a device or Telegram."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from plugins.phone_use import delivery, tool, wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.policy import PolicyDecision, bind_event_policy, get_event_policy
from plugins.phone_events import adapter
from plugins.phone_events.event_filter import PhoneEvent


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PHONE_BACKEND", "noop")
    monkeypatch.setattr(delivery, "state_directory", lambda: tmp_path / "receipts")
    monkeypatch.setattr(tool, "_resolve_android_serial", lambda: "test-device")
    tool.reset_backend_for_tests()
    yield
    tool.reset_backend_for_tests()


@pytest.mark.parametrize("state, confirmed", [("attempted", False), ("confirmed", True)])
def test_process_death_after_receipt_prevents_replay_with_new_turn_id(tmp_path, monkeypatch, state, confirmed):
    # Kill a separate writer without Python cleanup, then use the real tool
    # entry point in a new task. SQLite commit and OS lock recovery are real.
    program = """
import os, sys
from pathlib import Path
from plugins.phone_use.delivery import DeliveryJournal
with DeliveryJournal(Path(sys.argv[1])).receipt('event-1', 'test-device', 'Example', 'reply') as receipt:
    receipt.record(sys.argv[2])
    os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path / "receipts"), state],
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 17
    calls = []
    monkeypatch.setattr(tool, "reply_to_wechat", lambda *a, **kw: calls.append(a))
    decision = PolicyDecision(behavior="auto", instruction_source=True, delivery_identity="event-1")
    with bind_event_policy(decision):
        payload = json.loads(tool.handle_phone_use(
            {"action": "wechat_reply", "chat": "Example", "text": "reply"}, task_id="new-turn"))
    tool._finish_turn("new-turn", "")
    assert calls == []
    assert payload["ok"] is confirmed
    assert payload["meta"]["duplicate_suppressed"]
    assert payload["meta"]["delivery_status"] == ("confirmed" if confirmed else "uncertain")


def test_durable_reply_distinguishes_quoted_originals(monkeypatch):
    calls = []

    def send(_backend, _chat, _text, *, on_delivery, **options):
        calls.append(options["quote_text"])
        on_delivery("attempted")
        on_delivery("confirmed")
        return ActionResult(ok=True, action="wechat_reply",
                            meta={"delivery_attempted": True, "delivery_status": "confirmed"})

    monkeypatch.setattr(tool, "reply_to_wechat", send)
    decision = PolicyDecision(behavior="auto", instruction_source=True,
                              delivery_identity="same-notification")
    with bind_event_policy(decision):
        for index, quote in enumerate(("first original", "second original", "first original")):
            turn_id = f"quote-turn-{index}"
            response = json.loads(tool.handle_phone_use({
                "action": "wechat_reply", "chat": "Example", "text": "same answer",
                "quote_text": quote,
            }, task_id=turn_id))
            tool._finish_turn(turn_id, "")
            assert response["ok"]
    assert calls == ["first original", "second original"]


def test_pre_send_failure_can_retry_but_confirmed_send_survives_turn_cleanup(monkeypatch):
    calls = []

    def send(_backend, _chat, _text, on_delivery):
        calls.append("prepare")
        if len(calls) == 1:
            return ActionResult(ok=False, action="wechat_reply", message="chat unavailable")
        on_delivery("attempted")
        on_delivery("confirmed")
        return ActionResult(ok=True, action="wechat_reply", meta={"delivery_attempted": True})

    monkeypatch.setattr(tool, "reply_to_wechat", send)
    args = {"action": "wechat_reply", "chat": "Example", "text": "reply"}
    decision = PolicyDecision(behavior="auto", instruction_source=True, delivery_identity="event-1")
    with bind_event_policy(decision):
        assert not json.loads(tool.handle_phone_use(args, task_id="first"))["ok"]
        assert json.loads(tool.handle_phone_use(args, task_id="first"))["ok"]
        tool._finish_turn("first", "")
        assert json.loads(tool.handle_phone_use(args, task_id="after-restart"))["meta"]["duplicate_suppressed"]
        tool._finish_turn("after-restart", "")
    assert calls == ["prepare", "prepare"]


def test_exception_after_send_boundary_does_not_restart_wechat_flow(monkeypatch):
    attempts = []

    def attempt(_backend, _chat, _text, *, on_delivery, **_options):
        attempts.append(1)
        on_delivery("attempted")
        raise RuntimeError("connection lost after send")

    monkeypatch.setattr(wechat, "_reply_once", attempt)
    result = wechat.reply(tool._NoopBackend(), "Example", "reply", on_delivery=lambda state: None)
    assert attempts == [1]
    assert result.meta["delivery_status"] == "uncertain"


def test_actual_send_tap_observes_committed_receipt(tmp_path, monkeypatch):
    journal = delivery.DeliveryJournal(tmp_path / "boundary")
    sends = []
    send_screen = CaptureResult(mode="hierarchy", width=1080, height=2400,
        elements=[UIElement(index=1, class_name="android.widget.Button", text="发送",
                            bounds=(850, 2100, 1050, 2250))])
    empty = CaptureResult(mode="hierarchy", width=1080, height=2400)

    with journal.receipt("event", "device", "Example", "reply") as receipt:
        class Backend(tool._NoopBackend):
            def capture(self, mode="hierarchy"):
                return empty if sends else send_screen

            def tap(self, **kwargs):
                assert receipt.state == "attempted"
                sends.append(kwargs)
                return ActionResult(ok=True, action="tap")

        monkeypatch.setattr(wechat, "open_chat", lambda *a: ActionResult(ok=True, action="open", capture=send_screen))
        monkeypatch.setattr(wechat, "_prepare_text_input", lambda *a: ActionResult(ok=True, action="focus", capture=send_screen))
        monkeypatch.setattr(wechat, "_settle_capture", lambda backend, cap, predicate, **kw: cap)
        result = wechat.reply(Backend(), "Example", "reply", on_delivery=receipt.record)
        assert len(sends) == 1
        assert result.meta["delivery_status"] == "uncertain"
        assert receipt.state == "attempted"


def test_unwritable_receipt_store_never_calls_sender(tmp_path, monkeypatch):
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / "receipts.sqlite3").write_text("invalid database")
    monkeypatch.setattr(tool, "reply_to_wechat", lambda *a, **kw: pytest.fail("sent without durable receipt"))
    with bind_event_policy(PolicyDecision(behavior="auto", instruction_source=True, delivery_identity="event")):
        result = json.loads(tool.handle_phone_use(
            {"action": "wechat_reply", "chat": "Example", "text": "reply"}, task_id="A"))
        tool._finish_turn("A", "")
    assert result["ok"] is False


def test_two_auto_turns_cannot_interleave_even_between_phone_calls(monkeypatch):
    first_call_done, second_waiting, finish_first = (threading.Event() for _ in range(3))
    second_ran = threading.Event()
    operations, errors = [], []
    decision = PolicyDecision(behavior="auto", instruction_source=True)

    def dispatch(_backend, _action, args):
        operations.append(args["label"])
        if args["label"] == "B":
            second_ran.set()
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool, "_dispatch", dispatch)

    def first():
        try:
            with bind_event_policy(decision):
                tool.handle_phone_use({"action": "capture", "label": "A1"}, task_id="A")
                first_call_done.set()
                assert finish_first.wait(3)
                tool.handle_phone_use({"action": "capture", "label": "A2"}, task_id="A")
        except BaseException as exc:
            errors.append(exc)
        finally:
            tool._finish_turn("A", "")

    def second():
        try:
            assert first_call_done.wait(3)
            second_waiting.set()
            with bind_event_policy(decision):
                tool.handle_phone_use({"action": "capture", "label": "B"}, task_id="B")
        except BaseException as exc:
            errors.append(exc)
        finally:
            tool._finish_turn("B", "")

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    assert second_waiting.wait(3)
    assert not second_ran.wait(0.1)
    assert operations == ["A1"]
    finish_first.set()
    a.join(3)
    b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert not errors
    assert operations == ["A1", "A2", "B"]


def test_notification_identity_survives_replay_but_not_a_new_message(monkeypatch):
    decisions = []
    owner = object.__new__(adapter.PhoneEventAdapter)
    owner._serial = "test-device"
    owner._redact_otp = True
    owner._event_filter = SimpleNamespace(should_forward=lambda event: True)
    owner._message_handler = object()
    owner._dispatch_to_gateway = lambda text, event, decision: decisions.append(decision)
    monkeypatch.setattr(adapter, "_load_policy", lambda: SimpleNamespace(evaluate_event=lambda **kw:
        PolicyDecision(behavior="auto", instruction_source=True)))
    for timestamp in (123.0, 123.0, 124.0):
        owner._on_raw_event(PhoneEvent("notification", package="com.tencent.mm", title="Example",
            body="same request", timestamp=timestamp,
            meta={"_transport": "helper_socket", "notification_key": "chat-key"}))
    assert decisions[0].delivery_identity == decisions[1].delivery_identity
    assert decisions[0].delivery_identity != decisions[2].delivery_identity


def test_background_gateway_dispatch_waits_and_keeps_each_events_identity(monkeypatch):
    # Match Hermes' real handle_message contract: spawn background work and
    # return immediately. A second notification must not enter its busy queue.
    session = SimpleNamespace(build_session_key=lambda *a, **kw: "lane")
    monkeypatch.setitem(sys.modules, "gateway.session", session)
    observed = []
    monkeypatch.setattr(adapter, "_return_phone_home", lambda: pytest.fail("premature Home"))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Gateway:
            config = SimpleNamespace(extra={})

            def __init__(self):
                self._session_tasks = {}

            async def handle_message(self, event):
                assert not any(not t.done() for t in self._session_tasks.values())

                async def run():
                    observed.append(get_event_policy().delivery_identity)
                    started.set()
                    await release.wait()

                self._session_tasks["lane"] = asyncio.create_task(run())

        gateway = Gateway()
        first = asyncio.create_task(adapter._dispatch_with_event_policy(gateway,
            SimpleNamespace(channel_prompt=None, source=object()), PolicyDecision(delivery_identity="A")))
        await started.wait()
        second = asyncio.create_task(adapter._dispatch_with_event_policy(gateway,
            SimpleNamespace(channel_prompt=None, source=object()), PolicyDecision(delivery_identity="B")))
        await asyncio.sleep(0)
        assert not first.done()
        assert observed == ["A"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)

    asyncio.run(scenario())
    assert observed == ["A", "B"]
