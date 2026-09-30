"""Owner-controlled pause behavior, without touching an Android device."""

import json
import threading
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.phone_use.pause import PhonePauseGate, PhonePaused
from plugins.phone_use import tool as phone_tool
from plugins.phone_use import control
from plugins.phone_events import adapter as phone_events_adapter


@pytest.fixture(autouse=True)
def isolate_pause_state(tmp_path, monkeypatch):
    """Never let a phone-control test write the user's live Hermes home."""
    gate = PhonePauseGate(tmp_path / "isolated-phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_events_adapter, "_phone_pause_gate", lambda: gate)


def test_pause_persists_and_resume_invalidates_old_turn(tmp_path):
    path = tmp_path / "phone-pause.json"
    gate = PhonePauseGate(path)
    assert gate.snapshot() == (False, 0)
    assert gate.pause() == (True, 1)
    assert PhonePauseGate(path).snapshot() == (True, 1)
    assert gate.pause() == (True, 1)
    with pytest.raises(PhonePaused):
        gate.ensure_active()
    assert gate.resume() == (False, 2)
    assert gate.resume() == (False, 2)
    with pytest.raises(PhonePaused):
        gate.ensure_active(0)
    gate.ensure_active(2)
    assert path.stat().st_mode & 0o777 == 0o600


def test_broken_pause_state_fails_closed_but_can_be_recovered(tmp_path):
    path = tmp_path / "phone-pause.json"
    path.write_text("bad json")
    gate = PhonePauseGate(path)
    assert gate.snapshot()[0] is True
    with pytest.raises(PhonePaused):
        gate.ensure_active()
    assert gate.resume()[0] is False
    assert json.loads(path.read_text())["paused"] is False


def test_paused_phone_tool_never_asks_approval_or_opens_backend(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    gate.pause()
    monkeypatch.setattr(phone_tool, "_get_backend", lambda: pytest.fail("backend opened"))
    phone_tool.set_approval_callback(lambda *_args: pytest.fail("approval asked"))
    try:
        result = json.loads(phone_tool.handle_phone_use({"action": "tap", "x": 10, "y": 10}))
    finally:
        phone_tool.set_approval_callback(None)
    assert result["error"] == "phone paused"


def test_pause_wakes_queued_phone_action_without_running_it(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    queue = phone_tool.DeviceOperationQueue()
    monkeypatch.setattr(phone_tool, "_device_operation_queue", queue)
    entered = threading.Event()
    returned = []

    def waiting():
        entered.set()
        try:
            with queue.turn("tap", "waiting", generation=gate.snapshot()[1]):
                returned.append("ran")
        except PhonePaused:
            returned.append("paused")

    with queue.turn("tap", "active", generation=gate.snapshot()[1]):
        worker = threading.Thread(target=waiting)
        worker.start()
        assert entered.wait(timeout=1)
        gate.pause()
        queue.abort_waiters()
        worker.join(timeout=1)
    assert returned == ["paused"]


def test_resuming_does_not_revive_a_cancelled_composite_call(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    original_generation = gate.snapshot()[1]
    calls = []

    class Backend:
        def capture(self):
            calls.append("capture")
            gate.pause()
            gate.resume()

        def tap(self):
            calls.append("tap")

    backend = phone_tool._GuardedBackend(Backend(), original_generation)
    backend.capture()
    with pytest.raises(PhonePaused):
        backend.tap()
    assert calls == ["capture"]


def test_only_authorized_main_telegram_message_can_pause(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    extras = {"telegram_user_id": "owner", "telegram_chat_id": "main"}
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"),
                             user_id="owner", chat_id="main", thread_id=None,
                             profile=None)
    event = SimpleNamespace(text="暂停手机操控", source=source, internal=False,
                            raw_message=None)
    runner = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra=extras)}),
        _is_user_authorized=lambda src: src.user_id == "owner",
        adapters={},
    )

    async def exercise():
        assert control.pre_gateway_dispatch(event=event, gateway=runner)["action"] == "skip"
        await asyncio.sleep(0)

    asyncio.run(exercise())
    assert gate.snapshot()[0] is True
    gate.resume()
    event.source = SimpleNamespace(**{**vars(source), "user_id": "other"})
    assert control.pre_gateway_dispatch(event=event, gateway=runner) is None
    event.source = source
    event.text = "请不要暂停手机操控，引用：暂停手机操控"
    assert control.pre_gateway_dispatch(event=event, gateway=runner) is None
    assert gate.snapshot()[0] is False


def test_paused_event_adapter_never_reads_or_forwards_phone_events(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    gate.pause()
    monkeypatch.setattr(phone_events_adapter, "_phone_pause_gate", lambda: gate)
    adapter = object.__new__(phone_events_adapter.PhoneEventAdapter)
    adapter._target_chat_id = "main"
    adapter._mark_connected = lambda: None
    adapter._on_raw_event(SimpleNamespace(event_type="notification", package="com.tencent.mm",
                                          title="Example", body="@Bot hello", meta={}))

    async def exercise():
        assert await adapter.connect() is True

    asyncio.run(exercise())
    assert adapter.is_connected is True
    assert adapter._monitors_running is False


def test_owner_pause_cancels_active_phone_turn_and_stops_monitors(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    queue = phone_tool.DeviceOperationQueue()
    monkeypatch.setattr(phone_tool, "_device_operation_queue", queue)
    calls = []
    monkeypatch.setattr(phone_tool, "return_phone_home", lambda task: calls.append(("home", task)))

    class Events:
        def abort_phone_dispatches(self):
            calls.append("discard queued phone events")
            return {}

        async def suspend_monitors(self):
            calls.append("disconnect helper")

    class Telegram:
        async def send(self, chat_id, content, metadata=None):
            calls.append(("ack", chat_id, content))

    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"),
                             user_id="owner", chat_id="main", thread_id=None, profile=None)
    runner = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra={
            "telegram_user_id": "owner", "telegram_chat_id": "main",
        })}),
        _is_user_authorized=lambda _source: True,
        adapters={"phone_events": Events()},
        _get_cached_session_source=lambda _key: source,
        _adapter_for_source=lambda _source: Telegram(),
        _interrupt_and_clear_session=None,
    )

    async def interrupt(key, _source, **_kwargs):
        calls.append(("cancel", key))

    runner._interrupt_and_clear_session = interrupt
    event = SimpleNamespace(source=source, text="暂停手机操控", internal=False,
                            raw_message=None)

    async def exercise():
        with queue.turn("tap", "active-phone-turn", generation=gate.snapshot()[1]):
            assert control.pre_gateway_dispatch(event=event, gateway=runner)["action"] == "skip"
            await asyncio.sleep(0.03)

    asyncio.run(exercise())
    assert gate.snapshot()[0]
    assert ("cancel", "active-phone-turn") in calls
    assert "disconnect helper" in calls
    assert any(c[0] == "ack" and c[1] == "main" for c in calls if isinstance(c, tuple))


def test_cancelled_nonretained_turn_gets_one_home_cleanup(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    queue = phone_tool.DeviceOperationQueue()
    monkeypatch.setattr(phone_tool, "_device_operation_queue", queue)
    homes = []
    monkeypatch.setattr(phone_tool, "return_phone_home", lambda task: homes.append(task))
    with queue.turn("wechat_reply", "turn-one", generation=0):
        assert "turn-one" in phone_tool.pause_phone_operations()
    phone_tool._finish_turn("turn-one", "session-one")
    phone_tool._finish_turn("turn-one", "session-one")
    assert homes == ["turn-one"]


def test_resume_keeps_gate_closed_when_listener_reconnect_fails(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    gate.pause()
    sent = []

    class Events:
        async def resume_monitors(self):
            raise RuntimeError("device offline")

    class Telegram:
        async def send(self, _chat_id, content, metadata=None):
            sent.append(content)

    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                             chat_id="main", thread_id=None, profile=None)
    runner = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra={
            "telegram_user_id": "owner", "telegram_chat_id": "main",
        })}), _is_user_authorized=lambda _source: True,
        adapters={"phone_events": Events()}, _adapter_for_source=lambda _source: Telegram(),
    )
    event = SimpleNamespace(source=source, text="恢复手机操控", internal=False,
                            raw_message=None)

    async def exercise():
        assert control.pre_gateway_dispatch(event=event, gateway=runner)["action"] == "skip"
        await asyncio.sleep(0.03)

    asyncio.run(exercise())
    assert gate.snapshot()[0] is True
    assert "仍暂停" in sent[-1]


def test_forwarded_telegram_phrase_cannot_toggle_pause(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                             chat_id="main", thread_id=None, profile=None)
    event = SimpleNamespace(source=source, text="暂停手机操控", internal=False,
                            raw_message=SimpleNamespace(forward_origin=object()))
    gateway = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra={
            "telegram_user_id": "owner", "telegram_chat_id": "main",
        })}), _is_user_authorized=lambda _source: True,
    )
    assert control.pre_gateway_dispatch(event=event, gateway=gateway) is None
    assert gate.snapshot()[0] is False


def test_approved_friend_action_cannot_bypass_pause(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    gate.pause()
    monkeypatch.setattr(phone_tool, "_get_backend", lambda: pytest.fail("backend opened"))
    result = json.loads(phone_tool.accept_approved_wechat_friend_request("Example"))
    assert result["error"] == "phone paused"


def test_friend_approval_that_finishes_after_pause_does_not_touch_device(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_events_adapter, "_phone_pause_gate", lambda: gate)
    monkeypatch.setattr(phone_events_adapter, "_await_friend_request_approval",
                        lambda *_args: (gate.pause(), "once")[1])
    monkeypatch.setattr(phone_events_adapter, "_accept_approved_friend_request",
                        lambda *_args: pytest.fail("device action started"))
    adapter = object.__new__(phone_events_adapter.PhoneEventAdapter)
    adapter._process_friend_request(SimpleNamespace(), "Example")


def test_queued_phone_event_dropped_after_previous_turn_on_pause(tmp_path, monkeypatch):
    hermes_source = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_source.is_dir():
        pytest.skip("Hermes source is required for platform integration")
    monkeypatch.syspath_prepend(str(hermes_source))
    from gateway.session import SessionSource, build_session_key
    from gateway.config import Platform
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(phone_events_adapter, "_phone_pause_gate", lambda: gate)
    started = asyncio.Event()
    release = asyncio.Event()

    class Telegram:
        config = SimpleNamespace(extra={})
        _session_tasks = {}

        async def handle_message(self, message):
            pytest.fail("paused queued event dispatched")

    adapter = Telegram()
    message = SimpleNamespace(source=SessionSource(platform=Platform.TELEGRAM,
                              chat_id="main", chat_type="group", user_id="phone"), channel_prompt="")

    async def previous():
        started.set()
        await release.wait()

    async def exercise():
        key = build_session_key(message.source)
        adapter._session_tasks[key] = asyncio.create_task(previous())
        await started.wait()
        queued = asyncio.create_task(phone_events_adapter._dispatch_with_event_policy(
            adapter, message, None,
        ))
        await asyncio.sleep(0)
        gate.pause()
        release.set()
        await queued

    asyncio.run(exercise())


def test_cleanup_in_progress_prevents_resume(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    gate.pause()
    queue = phone_tool.DeviceOperationQueue()
    monkeypatch.setattr(phone_tool, "_device_operation_queue", queue)
    queue._cleanup_needed.add("old-turn")
    sent = []
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                             chat_id="main", thread_id=None, profile=None)
    class Events:
        async def resume_monitors(self):
            pytest.fail("reconnected during cleanup")
    class Telegram:
        async def send(self, _chat_id, content, metadata=None):
            sent.append(content)
    runner = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra={
            "telegram_user_id": "owner", "telegram_chat_id": "main",
        })}), _is_user_authorized=lambda _source: True,
        adapters={"phone_events": Events()}, _adapter_for_source=lambda _source: Telegram(),
    )
    event = SimpleNamespace(source=source, text="恢复手机操控", internal=False, raw_message=None)

    async def exercise():
        assert control.pre_gateway_dispatch(event=event, gateway=runner)["action"] == "skip"
        await asyncio.sleep(0.03)

    asyncio.run(exercise())
    assert gate.snapshot()[0] is True
    assert "清理" in sent[-1]


def test_failed_home_cleanup_is_visible_and_blocks_resume(tmp_path, monkeypatch):
    gate = PhonePauseGate(tmp_path / "phone-pause.json")
    monkeypatch.setattr(control, "pause_gate", gate)
    monkeypatch.setattr(phone_tool, "pause_gate", gate)
    queue = phone_tool.DeviceOperationQueue()
    monkeypatch.setattr(phone_tool, "_device_operation_queue", queue)
    queue._cleanup_needed.add("old-turn")
    monkeypatch.setattr(phone_tool, "return_phone_home", lambda _task: False)
    phone_tool._finish_turn("old-turn", "")
    assert queue.cleanup_failed()
    gate.pause()
    sent = []
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="owner",
                             chat_id="main", thread_id=None, profile=None)
    class Telegram:
        async def send(self, _chat_id, content, metadata=None):
            sent.append(content)
    gateway = SimpleNamespace(adapters={"phone_events": SimpleNamespace()},
                              _adapter_for_source=lambda _source: Telegram())
    asyncio.run(control._apply(gateway, source, "resume", set()))
    assert gate.snapshot()[0]
    assert "清理失败" in sent[-1]


def test_real_hermes_source_registers_pre_dispatch_hook(tmp_path, monkeypatch):
    """Exercise the plugin edge with real gateway types, no network or ADB."""
    hermes_source = Path.home() / ".hermes" / "hermes-agent"
    if not hermes_source.is_dir():
        pytest.skip("Hermes source is required for platform integration")
    monkeypatch.syspath_prepend(str(hermes_source))
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.platforms.base import MessageEvent, MessageType
    from plugins import phone_use

    hooks = {}
    class Context:
        def register_tool(self, **_kwargs):
            pass
        def register_hook(self, name, callback):
            hooks[name] = callback

    phone_use.register(Context())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="main",
                           chat_type="dm", user_id="owner")
    event = MessageEvent(text="暂停手机操控", message_type=MessageType.TEXT,
                         source=source)
    runner = SimpleNamespace(
        config=SimpleNamespace(platforms={"phone_events": SimpleNamespace(extra={
            "telegram_user_id": "owner", "telegram_chat_id": "main",
        })}), _is_user_authorized=lambda _source: True, adapters={},
    )
    async def exercise():
        assert hooks["pre_gateway_dispatch"](event=event, gateway=runner)["action"] == "skip"
        await asyncio.sleep(0)
    asyncio.run(exercise())
    assert phone_tool.pause_gate.snapshot()[0]
