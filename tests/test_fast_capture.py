"""Fast WeChat capture: resident OCR helper and raw-frame screenshots."""
from __future__ import annotations

import struct
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from plugins.phone_use import adb_backend as adb_module
from plugins.phone_use import host_ocr
from plugins.phone_use.adb_backend import AdbBackend
from plugins.phone_use.backend import DeviceInfo, UIElement

# Imported before the autouse fixture stubs it out.
_REAL_RAW_SCREEN = AdbBackend._recognize_raw_screen

# Speaks the --serve protocol of phone_ocr.swift and reports which process and
# request it served.
_SERVER = f"""#!{sys.executable}
import json, os, sys
log = open(os.environ["OCR_LOG"], "a")
log.write(" ".join(sys.argv[1:]) + "\\n"); log.flush()
if sys.argv[1:] != ["--serve"]:
    sys.exit(1)
stdin = sys.stdin.buffer
while True:
    line = stdin.readline()
    if not line:
        break
    header = json.loads(line)
    data = stdin.read(header["length"])
    mode = os.environ.get("OCR_MODE", "ok")
    if mode == "hang":
        import time; time.sleep(30)
    if mode == "error":
        print(json.dumps({{"error": "unreadable image"}}), flush=True)
        continue
    print(json.dumps({{"width": 4, "height": 6, "pid": os.getpid(), "format": header["format"],
                       "size": len(data), "items": [{{"text": "hi", "confidence": 0.9, "bounds": [1, 2, 30, 40]}}]}}),
          flush=True)
"""

# A helper built before --serve existed: usage error unless given an image path.
_ONE_SHOT_ONLY = f"""#!{sys.executable}
import json, os, sys
open(os.environ["OCR_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--serve"] or len(sys.argv) != 2:
    sys.exit(1)
print(json.dumps({{"width": 4, "height": 6, "items": [{{"text": "legacy", "confidence": 0.9, "bounds": [1, 2, 30, 40]}}]}}))
"""


@pytest.fixture
def ocr(monkeypatch, tmp_path):
    """Install a fake helper as the default one with a fresh resident server."""
    log = tmp_path / "ocr.log"
    monkeypatch.setenv("OCR_LOG", str(log))
    server = host_ocr._OcrServer()
    monkeypatch.setattr(host_ocr, "_server", server)

    def install(script: str = _SERVER) -> Path:
        helper = tmp_path / "phone-ocr"
        helper.write_text(script, encoding="utf-8")
        helper.chmod(0o755)
        monkeypatch.setattr(host_ocr, "_DEFAULT_HELPER_PATH", helper)
        return helper

    yield types.SimpleNamespace(install=install, server=server, log=log)
    server.close()



def _png() -> str:
    import base64
    return base64.b64encode(b"png-bytes").decode()


def test_resident_helper_serves_png_and_raw_frames_from_one_process(ocr):
    ocr.install()

    first = host_ocr.recognize_text(_png())
    raw = host_ocr.recognize_rgba(b"\x00" * 24, 2, 3)
    host_ocr.recognize_text(_png())

    assert [e.text for e in first] == ["hi"] and first[0].bounds == (1, 2, 30, 40)
    assert [e.text for e in raw] == ["hi"]
    # One spawn for three requests: no per-call Vision cold start.
    assert ocr.log.read_text().splitlines() == ["--serve"]


def test_raw_frame_reaches_helper_intact(ocr):
    ocr.install()
    helper = host_ocr._DEFAULT_HELPER_PATH

    payload = host_ocr._server.request(helper, "rgba", memoryview(b"\x01" * 24)[4:], width=2, height=3)

    assert (payload["format"], payload["size"]) == ("rgba", 20)


def test_helper_without_serve_falls_back_to_one_shot_and_is_not_retried(ocr):
    ocr.install(_ONE_SHOT_ONLY)

    texts = [[e.text for e in host_ocr.recognize_text(_png())] for _ in range(2)]

    assert texts == [["legacy"], ["legacy"]]
    assert ocr.server._unsupported
    assert host_ocr.recognize_rgba(b"\x00" * 24, 2, 3) is None  # caller takes the PNG route
    # --serve was tried exactly once; every other invocation was one-shot.
    assert ocr.log.read_text().splitlines().count("--serve") == 1


def test_helper_crash_is_recovered_with_a_new_process(ocr):
    ocr.install()
    pid = host_ocr._server.request(host_ocr._DEFAULT_HELPER_PATH, "png", b"x")["pid"]
    ocr.server._proc.kill()
    ocr.server._proc.wait()

    again = host_ocr._server.request(host_ocr._DEFAULT_HELPER_PATH, "png", b"x")

    assert again is not None and again["pid"] != pid


def test_hung_helper_times_out_to_one_shot_without_blocking(ocr, monkeypatch):
    ocr.install()
    monkeypatch.setenv("OCR_MODE", "hang")
    started = time.monotonic()

    payload = ocr.server.request(host_ocr._DEFAULT_HELPER_PATH, "png", b"x", timeout=0.5)

    assert payload is None and time.monotonic() - started < 5
    assert ocr.server._proc is None and not ocr.server._unsupported


def test_error_reply_drops_the_helper_so_no_request_bytes_linger(ocr, monkeypatch):
    ocr.install()
    monkeypatch.setenv("OCR_MODE", "error")

    assert ocr.server.request(host_ocr._DEFAULT_HELPER_PATH, "png", b"x") is None
    assert ocr.server._proc is None


def test_resident_helper_exits_after_idle_period(ocr, monkeypatch):
    ocr.install()
    monkeypatch.setattr(host_ocr, "_SERVER_IDLE_SECONDS", 0.1)
    ocr.server.request(host_ocr._DEFAULT_HELPER_PATH, "png", b"x")
    proc = ocr.server._proc

    time.sleep(0.6)

    assert proc.poll() is not None and ocr.server._proc is None


def test_explicit_helper_path_stays_one_shot(ocr, tmp_path):
    ocr.install()
    other = tmp_path / "other"
    other.write_text(_ONE_SHOT_ONLY, encoding="utf-8")
    other.chmod(0o755)

    elements = host_ocr.recognize_text(_png(), helper_path=other)

    assert [e.text for e in elements] == ["legacy"]
    assert ocr.server._proc is None  # the resident helper is only for the default binary


# ── adb raw-frame capture ───────────────────────────────────────────────

def _frame(width: int, height: int, header: int = 16, pixel_format: int = 1) -> bytes:
    head = struct.pack("<III", width, height, pixel_format) + b"\x00" * (header - 12)
    return head + b"\x07" * (width * height * 4)


def _wechat_backend(monkeypatch) -> AdbBackend:
    backend = AdbBackend(serial="emulator-5554")
    monkeypatch.setattr(backend, "device_info", lambda: DeviceInfo(
        serial="emulator-5554", model="", android_version="", sdk_version=0,
        screen_width=1080, screen_height=2400, density=420, is_emulator=True))
    monkeypatch.setattr(backend, "_get_foreground_app",
                        lambda: {"package": "com.tencent.mm", "activity": ".ui.LauncherUI"})
    monkeypatch.setattr(backend, "_wait_for_window_focus", lambda package: None)
    monkeypatch.setattr(backend, "_dump_ui_hierarchy", lambda: [])
    monkeypatch.setattr(AdbBackend, "_recognize_raw_screen", _REAL_RAW_SCREEN)
    return backend


def _adb_returns(monkeypatch, stdout: bytes = b"", returncode: int = 0, error: Exception = None):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if error:
            raise error
        return subprocess.CompletedProcess(command, returncode, stdout, b"")

    monkeypatch.setattr(adb_module.subprocess, "run", run)
    return commands


@pytest.mark.parametrize("header", [12, 16])  # Android <13 / >=13 screencap headers
def test_text_only_capture_ocrs_the_raw_frame_without_png_round_trip(monkeypatch, header):
    backend = _wechat_backend(monkeypatch)
    commands = _adb_returns(monkeypatch, _frame(4, 6, header))
    seen = []
    ocr_element = UIElement(1, "host.ocr.Text", text="Chat", bounds=(1, 2, 3, 4), clickable=True)

    def recognize(rgba, width, height):
        seen.append((width, height, len(rgba), bytes(rgba[:1])))
        return [ocr_element]

    monkeypatch.setattr(adb_module, "recognize_rgba", recognize)
    monkeypatch.setattr(backend, "_take_screenshot",
                        lambda: pytest.fail("PNG screenshot must not be taken"))

    capture = backend.capture(mode="hierarchy")

    assert commands == [["adb", "-s", "emulator-5554", "exec-out", "screencap"]]
    assert seen == [(4, 6, 4 * 6 * 4, b"\x07")]  # header stripped exactly
    assert [e.text for e in capture.elements][0] == "Chat"
    assert capture.png_b64 is None


@pytest.mark.parametrize("scenario", ["adb_failed", "adb_missing", "other_pixel_format", "truncated", "unknown_header"])
def test_unusable_raw_frame_falls_back_to_png_ocr(monkeypatch, scenario):
    backend = _wechat_backend(monkeypatch)
    if scenario == "adb_failed":
        _adb_returns(monkeypatch, b"", returncode=1)
    elif scenario == "adb_missing":
        _adb_returns(monkeypatch, error=FileNotFoundError("adb"))
    elif scenario == "other_pixel_format":
        _adb_returns(monkeypatch, _frame(4, 6, pixel_format=22))  # e.g. wide-gamut RGBA_F16
    elif scenario == "truncated":
        _adb_returns(monkeypatch, _frame(4, 6)[:-5])
    else:
        _adb_returns(monkeypatch, _frame(4, 6, header=16) + b"\x00" * 3)
    monkeypatch.setattr(adb_module, "recognize_rgba", lambda *a: pytest.fail("must not OCR a bad frame"))
    monkeypatch.setattr(backend, "_take_screenshot", lambda: "ZmFrZS1wbmc=")
    ocr_element = UIElement(1, "host.ocr.Text", text="Chat", bounds=(1, 2, 3, 4), clickable=True)
    monkeypatch.setattr(adb_module, "recognize_text", lambda png_b64: [ocr_element])

    capture = backend.capture(mode="hierarchy")

    assert [e.text for e in capture.elements][0] == "Chat"


def test_helper_without_raw_support_falls_back_to_png_ocr(monkeypatch):
    backend = _wechat_backend(monkeypatch)
    _adb_returns(monkeypatch, _frame(4, 6))
    monkeypatch.setattr(adb_module, "recognize_rgba", lambda *a: None)
    monkeypatch.setattr(backend, "_take_screenshot", lambda: "ZmFrZS1wbmc=")
    ocr_element = UIElement(1, "host.ocr.Text", text="Legacy", bounds=(1, 2, 3, 4), clickable=True)
    monkeypatch.setattr(adb_module, "recognize_text", lambda png_b64: [ocr_element])

    assert backend.capture(mode="hierarchy").elements[0].text == "Legacy"


@pytest.mark.parametrize("mode", ["som", "image_hierarchy"])
def test_modes_that_return_or_inspect_the_image_keep_the_png_route(monkeypatch, mode):
    backend = _wechat_backend(monkeypatch)
    monkeypatch.setattr(AdbBackend, "_recognize_raw_screen",
                        lambda self: pytest.fail("raw frames are only for text-only captures"))
    monkeypatch.setattr(backend, "_take_screenshot", lambda: "ZmFrZS1wbmc=")
    ocr_element = UIElement(1, "host.ocr.Text", text="Chat", bounds=(1, 2, 3, 4), clickable=True)
    monkeypatch.setattr(adb_module, "recognize_text", lambda png_b64: [ocr_element])

    capture = backend.capture(mode=mode)

    assert capture.png_b64 == "ZmFrZS1wbmc=" and capture.elements[0].text == "Chat"
