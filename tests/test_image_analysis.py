"""Exercise real Vision decoding and Python handling without touching a phone."""
import base64
import subprocess
import sys
from pathlib import Path

import pytest

from plugins.phone_use.host_ocr import analyze_image, recognize_text


@pytest.fixture(scope='module')
def vision_fixture(tmp_path_factory):
    if sys.platform != 'darwin':
        pytest.skip('Apple Vision integration requires macOS')
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path_factory.mktemp('image-analysis')
    helper = directory / 'phone-ocr'
    subprocess.run(['swiftc', str(root / 'plugins/phone_use/native/phone_ocr.swift'),
                    '-o', str(helper)], check=True, timeout=120)
    png = directory / 'qr.png'
    subprocess.run(['swift', str(root / 'tests/fixtures/make_qr.swift'), str(png)],
                   check=True, timeout=120)
    return helper, base64.b64encode(png.read_bytes()).decode('ascii')


def test_real_qr_is_data_and_never_a_click_target(vision_fixture):
    helper, encoded = vision_fixture
    result = analyze_image(encoded, helper_path=helper)
    assert result['qr_status'] == 'complete'
    assert result['qr_codes'] == [{'text': 'https://example.org/phone-test?value=42',
                                  'truncated': False}]
    assert result['untrusted_content'] is True
    assert all(e.text != result['qr_codes'][0]['text']
               for e in recognize_text(encoded, helper_path=helper))


def test_missing_helper_is_not_reported_as_no_qr(tmp_path):
    result = analyze_image('aW1hZ2U=', helper_path=tmp_path / 'absent')
    assert result['status'] == 'unavailable'


def test_corrupt_image_is_unavailable(vision_fixture):
    helper, _ = vision_fixture
    assert analyze_image('bm90IGFuIGltYWdl', helper_path=helper)['status'] == 'unavailable'


def test_collection_pairs_decoded_contents_with_returned_screenshot(vision_fixture, monkeypatch):
    from plugins.phone_use import host_ocr, wechat_context
    from plugins.phone_use.backend import ActionResult, CaptureResult
    helper, encoded = vision_fixture
    monkeypatch.setattr(host_ocr, '_DEFAULT_HELPER_PATH', helper)
    screenshot = CaptureResult(mode='som', width=1080, height=2400,
                               current_package='com.tencent.mm', png_b64=encoded)
    monkeypatch.setattr(wechat_context, 'open_chat', lambda *_: ActionResult(
        ok=True, action='wechat_open_chat', capture=screenshot))

    class Phone:
        def capture(self, mode):
            return screenshot

    result = wechat_context.collect_context(Phone(), 'Example', max_pages=1,
                                            include_images=True, open_images=False)
    assert result.ok
    assert result.meta['screenshots'] == [encoded]
    observation = result.meta['image_analysis'][0]
    assert observation['image_index'] == 1
    assert observation['source'] == 'chat_screenshot'
    assert observation['qr_codes'][0]['text'] == 'https://example.org/phone-test?value=42'
