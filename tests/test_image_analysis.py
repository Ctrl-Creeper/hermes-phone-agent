"""Exercise real Vision decoding and Python handling without touching a phone."""
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from plugins.phone_use.host_ocr import analyze_image, recognize_text
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.wechat_context import collect_context


class ImageWorkflowPhone:
    """Stateful device boundary; element IDs change after returning from preview."""

    def __init__(self, encoded='bm90LXBuZw==', *, back='ok', fault=None):
        self.encoded = encoded
        self.back = back
        self.fault = fault
        self.screen = 'chat'
        self.page = 0
        self.generation = 0
        self.last_elements = []
        self.opened = []
        self.gestures = []
        self.draft = 'unfinished draft'
        self.title = 'Example'
        self.preview_polls = 0

    def launch_app(self, package):
        return ActionResult(ok=True, action='launch_app')

    def capture(self, mode):
        if self.screen == 'preview' and mode == 'screenshot':
            self.preview_polls += 1
            if self.fault == 'delayed' and self.preview_polls == 1:
                return CaptureResult(mode=mode, width=1080, height=2400,
                                     current_package='com.tencent.mm', current_activity='ChatUI')
        if self.fault == 'capture' and self.screen == 'preview' and mode == 'screenshot':
            raise RuntimeError('screenshot failed')
        if self.screen != 'chat':
            return CaptureResult(mode=mode, width=1080, height=2400,
                current_package='com.other' if self.screen == 'external' else 'com.tencent.mm',
                current_activity='ImagePreviewUI' if self.screen == 'preview' else 'OtherUI',
                png_b64=self.encoded)
        elements = [
            UIElement(1, 'android.widget.TextView', text=self.title, bounds=(400, 80, 680, 150)),
            UIElement(99, 'android.widget.EditText' if mode == 'image_hierarchy' else 'host.ocr.MessageInput',
                      text=self.draft, bounds=(100, 2150, 900, 2320)),
            UIElement(2, 'android.widget.TextView', text='recent text' if not self.page else 'older text',
                      bounds=(180, 320, 720, 390)),
            UIElement(3, 'android.widget.ImageView', content_desc='avatar',
                      bounds=(20, 500, 120, 600), clickable=True),
        ]
        if self.page:
            for offset, name in enumerate(('photo-a', 'photo-b')):
                elements.append(UIElement(10 + offset + 100 * self.generation,
                    'android.widget.ImageView', resource_id='image_message', content_desc=name,
                    bounds=(180, 500 + 650 * offset, 700, 1000 + 650 * offset), clickable=True))
            if self.fault == 'overlap':
                elements.append(UIElement(50 + 100 * self.generation, 'host.vision.ImageCandidate',
                    bounds=(185, 505, 695, 995), clickable=True, content_desc='photo-a'))
        if mode != 'screenshot':
            self.last_elements = elements
        return CaptureResult(mode=mode, width=1080, height=2400, elements=elements,
                             current_package='com.tencent.mm', current_activity='ChatUI')

    def tap(self, *, element):
        target = next(e for e in self.last_elements if e.index == element)
        if self.screen != 'chat' or target.content_desc not in {'photo-a', 'photo-b'}:
            raise AssertionError('stale or non-image tap')
        self.opened.append(target.content_desc)
        self.gestures.append('tap')
        self.screen = {'noop': 'chat', 'external': 'external', 'card': 'card'}.get(self.fault, 'preview')
        if self.fault == 'tap_exception':
            raise RuntimeError('tap injected but transport lost')
        return ActionResult(ok=True, action='tap')

    def keyevent(self, keycode):
        self.gestures.append(keycode)
        assert keycode == 'BACK'
        if self.back == 'raise':
            raise RuntimeError('BACK transport failed')
        if self.back == 'rejected':
            return ActionResult(ok=False, action='keyevent')
        if self.back != 'noop':
            self.screen = 'chat'
            self.generation += 1
            if self.back == 'wrong_chat':
                self.title = 'Examples'
        return ActionResult(ok=True, action='keyevent')

    def swipe(self, **kwargs):
        assert self.screen == 'chat', 'history gesture escaped onto preview'
        self.gestures.append('swipe')
        self.page += 1
        return ActionResult(ok=True, action='swipe')

    def wait(self, seconds):
        return ActionResult(ok=True, action='wait')


@pytest.mark.parametrize('back', ['noop', 'rejected', 'raise'])
def test_image_collection_stops_when_return_to_chat_is_unverified(back):
    phone = ImageWorkflowPhone(back=back)
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=2, max_images=2)
    assert not result.ok
    assert result.meta['stop_reason'] == 'image_recovery_failed'
    assert result.meta['chat_restored'] is False
    assert phone.opened == ['photo-a']
    assert 'swipe' not in phone.gestures
    assert phone.draft == 'unfinished draft'


def test_find_open_decode_exit_two_images_then_continue_chat(vision_fixture, monkeypatch):
    from plugins.phone_use import host_ocr
    helper, encoded = vision_fixture
    monkeypatch.setattr(host_ocr, '_DEFAULT_HELPER_PATH', helper)
    phone = ImageWorkflowPhone(encoded)
    result = collect_context(phone, 'Example', max_pages=2, max_images=2)
    assert result.ok
    assert result.meta['chat_restored'] is True
    assert phone.screen == 'chat'
    assert phone.opened == ['photo-a', 'photo-b']
    assert phone.gestures == ['swipe', 'tap', 'BACK', 'tap', 'BACK']
    assert result.meta['screenshots'] == [encoded, encoded]
    assert [i['image_index'] for i in result.meta['image_analysis']] == [1, 2]
    assert all(i['qr_codes'][0]['text'] == 'https://example.org/phone-test?value=42'
               for i in result.meta['image_analysis'])
    assert all('PHONE IMAGE TEST 42' in i['text'] for i in result.meta['image_analysis'])
    # A subsequent public read still uses the chat, with the draft unchanged.
    following = collect_context(phone, 'Example', max_pages=1, include_images=False)
    assert following.ok
    assert 'older text' in following.meta['lines']
    assert phone.draft == 'unfinished draft'
    assert len(phone.opened) == 2


@pytest.mark.parametrize('fault', ['noop', 'card', 'capture', 'tap_exception'])
def test_failed_image_inspection_recovers_without_backing_out_of_chat(fault):
    phone = ImageWorkflowPhone(fault=fault)
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=1, max_images=1)
    assert result.ok
    assert result.meta['image_count'] == 0
    assert result.meta['screenshots'] == []
    assert phone.screen == 'chat'
    assert phone.gestures == (['tap'] if fault == 'noop' else ['tap', 'BACK'])
    assert phone.draft == 'unfinished draft'


def test_external_app_is_not_backed_out_or_read_as_an_image():
    phone = ImageWorkflowPhone(fault='external')
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=2, max_images=2)
    assert not result.ok
    assert result.meta['chat_restored'] is False
    assert phone.gestures == ['tap']


def test_unavailable_recognition_still_restores_chat(tmp_path, monkeypatch):
    from plugins.phone_use import host_ocr
    monkeypatch.setattr(host_ocr, '_DEFAULT_HELPER_PATH', tmp_path / 'absent')
    phone = ImageWorkflowPhone()
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=1, max_images=1)
    assert result.ok
    assert result.meta['image_analysis'][0]['status'] == 'unavailable'
    assert result.meta['chat_restored'] is True
    assert phone.screen == 'chat'


def test_returning_to_similarly_named_chat_stops_instead_of_opening_more_images():
    phone = ImageWorkflowPhone(back='wrong_chat')
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=2, max_images=2)
    assert not result.ok
    assert result.meta['chat_restored'] is False
    assert phone.opened == ['photo-a']


def test_delayed_preview_is_observed_before_returning():
    phone = ImageWorkflowPhone(fault='delayed')
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=1, max_images=1)
    assert result.ok
    assert result.meta['image_count'] == 1
    assert phone.preview_polls == 2
    assert phone.screen == 'chat'


def test_native_and_vision_regions_for_one_photo_do_not_consume_two_attempts():
    phone = ImageWorkflowPhone(fault='overlap')
    phone.page = 1
    result = collect_context(phone, 'Example', max_pages=1, max_images=3)
    assert result.ok
    assert phone.opened == ['photo-a', 'photo-b']
    assert result.meta['image_count'] == 2


def test_phone_tool_preserves_image_results_and_allows_next_read(vision_fixture, monkeypatch, tmp_path):
    from plugins.phone_use import host_ocr, tool
    helper, encoded = vision_fixture
    monkeypatch.setattr(host_ocr, '_DEFAULT_HELPER_PATH', helper)
    config = tmp_path / 'phone-policy.yaml'
    config.write_text('default_behavior: report\n', encoding='utf-8')
    monkeypatch.setenv('PHONE_POLICY_PATH', str(config))
    phone = ImageWorkflowPhone(encoded)
    monkeypatch.setattr(tool, '_backend', phone)
    monkeypatch.setattr(tool, '_approval_callback', lambda *args: 'approve_once')
    response = tool.handle_phone_use({'action': 'wechat_collect_context', 'chat': 'Example',
                                     'max_pages': 2, 'max_images': 1}, task_id='image-test')
    payload = json.loads(response['text_summary'])
    assert payload['ok'] and payload['chat_restored']
    assert payload['image_analysis'][0]['text'] == ['PHONE IMAGE TEST 42']
    assert response['content'][1]['image_url']['url'] == 'data:image/png;base64,' + encoded
    following = json.loads(tool.handle_phone_use({
        'action': 'wechat_collect_context', 'chat': 'Example', 'max_pages': 1, 'include_images': False,
    }, task_id='next-read'))
    assert following['ok']
    assert phone.screen == 'chat'
    assert phone.draft == 'unfinished draft'


def test_app_switch_during_local_recognition_stops_before_history_swipe(vision_fixture, monkeypatch):
    from plugins.phone_use import host_ocr
    helper, encoded = vision_fixture
    phone = ImageWorkflowPhone(encoded)
    phone.page = 1
    run = subprocess.run

    def recognize_then_switch(command, **kwargs):
        result = run(command, **kwargs)
        if command[0] == str(helper):
            phone.screen = 'external'
        return result

    monkeypatch.setattr(host_ocr, '_DEFAULT_HELPER_PATH', helper)
    monkeypatch.setattr(subprocess, 'run', recognize_then_switch)
    result = collect_context(phone, 'Example', max_pages=2, max_images=1)
    assert not result.ok
    assert result.meta['chat_restored'] is False
    assert phone.gestures == ['tap', 'BACK']


def test_failed_image_workflow_does_not_send_home_to_another_app(monkeypatch, tmp_path):
    from plugins.phone_use import tool
    config = tmp_path / 'phone-policy.yaml'
    config.write_text('default_behavior: report\n', encoding='utf-8')
    monkeypatch.setenv('PHONE_POLICY_PATH', str(config))
    phone = ImageWorkflowPhone(fault='external')
    phone.page = 1
    monkeypatch.setattr(tool, '_backend', phone)
    monkeypatch.setattr(tool, '_approval_callback', lambda *args: 'approve_once')
    context = {'task_id': 'interrupted-image-workflow', 'session_id': 'image-session'}
    started = json.loads(tool.handle_phone_use({
        'action': 'begin_workflow', 'goal': 'Read image',
        'allowed_actions': ['wechat_collect_context'],
    }, **context))
    assert started['ok']
    result = json.loads(tool.handle_phone_use({
        'action': 'wechat_collect_context', 'chat': 'Example', 'max_pages': 1, 'max_images': 1,
    }, **context))
    assert not result['ok'] and result['chat_restored'] is False
    assert phone.gestures == ['tap']


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
    screenshot.elements = ImageWorkflowPhone().capture('hierarchy').elements
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
