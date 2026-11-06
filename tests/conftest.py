import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    # Plugin state (receipts, known groups) must never touch the real ~/.hermes.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


@pytest.fixture(autouse=True)
def _no_device_frames(monkeypatch):
    # Capture tests stub their own screenshot seams; the raw-frame fast path
    # must never reach a real adb/emulator that happens to be installed.
    from plugins.phone_use import adb_backend

    monkeypatch.setattr(adb_backend.AdbBackend, "_recognize_raw_screen", lambda self: None)


@pytest.fixture(autouse=True)
def _no_background_preopen(monkeypatch):
    # Dispatch tests must never open a chat on a real emulator behind their back.
    from plugins.phone_use import tool

    monkeypatch.setattr(tool, "preopen_wechat_chat", lambda chat: None)
