"""Owner-controlled pause behavior, without touching an Android device."""

import json
import threading

import pytest

from plugins.phone_use.pause import PhonePauseGate, PhonePaused
from plugins.phone_use import tool as phone_tool


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
