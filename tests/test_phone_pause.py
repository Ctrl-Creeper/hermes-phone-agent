"""Owner-controlled pause behavior, without touching an Android device."""

import json

import pytest

from plugins.phone_use.pause import PhonePauseGate, PhonePaused


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
