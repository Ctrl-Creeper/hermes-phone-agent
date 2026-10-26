import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    # Plugin state (receipts, known groups) must never touch the real ~/.hermes.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
