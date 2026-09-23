"""Quoted replies through the public composite workflow; no live messages."""
import json

import pytest

from plugins.phone_use import wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement


def node(index, text, y, *, cls='host.ocr.Text', **kwargs):
    return UIElement(index=index, class_name=cls, text=text,
                     bounds=(180, y, 800, y + 60), clickable=True, **kwargs)


class QuotePhone:
    def __init__(self, original='明天下午三点开会', preview=None):
        self.original = original
        self.preview = preview if preview is not None else 'Alice：' + original
        self.state = 'chat'
        self.calls = []
        self.text = ''
        self.duplicate = False
        self.menu_label = '引用'
        self.changed_preview = None

    def launch_app(self, package):
        return ActionResult(ok=True, action='launch_app')

    def capture(self, mode='hierarchy'):
        elements = [node(1, 'Example Chat', 100),
                    node(2, self.original, 450, attributes={'sender': 'Alice'}),
                    node(8, '', 1800, cls='host.ocr.MessageInput')]
        if self.duplicate:
            elements.append(node(4, self.original, 850, attributes={'sender': 'Bob'}))
        if self.state == 'menu':
            elements.append(node(3, self.menu_label, 1100))
        if self.state in {'quote', 'typed'}:
            text = self.changed_preview if self.state == 'typed' and self.changed_preview is not None else self.preview
            elements.append(node(5, text, 1660))
        if self.state == 'typed':
            elements.append(UIElement(index=6, class_name='host.ocr.Text', text='Send',
                                     bounds=(930, 1800, 1040, 1860), clickable=True))
        if self.state == 'sent':
            elements.append(node(7, self.text, 1000))
        return CaptureResult(mode=mode, width=1080, height=2400,
                             current_package='com.tencent.mm', elements=elements)

    def long_press(self, *, element, **kwargs):
        self.calls.append(('hold', element))
        self.state = 'menu'
        return ActionResult(ok=True, action='long_press')

    def tap(self, *, element=None, **kwargs):
        self.calls.append(('tap', element))
        if element == 3:
            self.state = 'quote'
        if element == 6:
            self.state = 'sent'
        return ActionResult(ok=True, action='tap')

    def set_text(self, text):
        self.calls.append(('type', text))
        self.text = text
        self.state = 'typed'
        return ActionResult(ok=True, action='set_text')

    def keyevent(self, key):
        self.calls.append(('key', key))
        if key == 'BACK' and self.state == 'menu':
            self.state = 'chat'
        return ActionResult(ok=True, action='keyevent')

    def swipe(self, **kwargs):
        self.calls.append(('swipe',))
        return ActionResult(ok=True, action='swipe')

    def wait(self, seconds):
        return ActionResult(ok=True, action='wait')


def test_reply_quotes_original_and_verifies_preview_before_send():
    phone = QuotePhone()
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original,
                          quote_sender='Alice', quote_max_pages=1)
    assert result.ok, result.message
    assert result.meta['quote_verified'] is True
    assert phone.calls.index(('hold', 2)) < phone.calls.index(('tap', 3)) < phone.calls.index(('type', '好的'))
    assert phone.calls.count(('tap', 6)) == 1
    assert phone.calls[-1] == ('key', 'HOME')


@pytest.mark.parametrize('preview', ['Alice：明天下午不开会', 'Bob：明天下午三点开会', '明天下午三点开会'])
def test_wrong_or_unidentified_preview_never_sends(preview):
    phone = QuotePhone(preview=preview)
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original,
                          quote_sender='Alice', quote_max_pages=1)
    assert not result.ok
    assert ('type', '好的') not in phone.calls
    assert ('tap', 6) not in phone.calls
    assert phone.calls.count(('hold', 2)) == 1


def test_preview_is_rechecked_after_typing():
    phone = QuotePhone()
    phone.changed_preview = 'Alice：另外一条消息'
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original)
    assert not result.ok
    assert result.meta['quote_status'] == 'quote_preview_changed_before_send'
    assert ('tap', 6) not in phone.calls


def test_duplicate_original_needs_sender_or_context():
    phone = QuotePhone()
    phone.duplicate = True
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original)
    assert not result.ok
    assert result.meta['quote_status'] == 'quote_ambiguous'
    assert not any(call[0] == 'hold' for call in phone.calls)


def test_explicit_sender_disambiguates_duplicate_original():
    phone = QuotePhone()
    phone.duplicate = True
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original, quote_sender='Alice')
    assert result.ok
    assert ('hold', 2) in phone.calls
    assert ('hold', 4) not in phone.calls


@pytest.mark.parametrize('original, requested', [
    ('明天下午三点开会，请带好材料', '明天下午四点开会，请带好材料'),
    ('明天下午不开会，请准备好材料', '明天下午开会，请准备好材料'),
    ('Meeting at 15:00 in the main room', 'Meeting at 16:00 in the main room'),
])
def test_numbers_and_negation_are_not_ocr_tolerance(original, requested):
    phone = QuotePhone(original=original)
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=requested, quote_max_pages=1)
    assert not result.ok
    assert not any(call[0] == 'hold' for call in phone.calls)


def test_unavailable_quote_menu_does_not_fall_back_to_plain_reply():
    phone = QuotePhone()
    phone.menu_label = 'Delete'
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original)
    assert not result.ok
    assert ('key', 'BACK') in phone.calls
    assert ('type', '好的') not in phone.calls


def test_quote_options_are_forwarded_by_tool_dispatch():
    from plugins.phone_use import tool
    phone = QuotePhone()
    result = json.loads(tool._dispatch(phone, 'wechat_reply', {
        'chat': 'Example Chat', 'text': '好的', 'quote_text': phone.original,
        'quote_sender': 'Alice', 'quote_max_pages': 1,
    }))
    assert result['ok']
    assert result['meta']['quote_verified']
    assert ('hold', 2) in phone.calls


def test_fuzzy_original_requires_observed_nearby_context():
    class ContextPhone(QuotePhone):
        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            result.elements.append(node(9, '请看活动安排', 350))
            return result

    phone = ContextPhone(original='明天下午大家记得带好会议材科')
    wanted = '明天下午大家记得带好会议材料'
    rejected = wechat.reply(phone, 'Example Chat', '好的', quote_text=wanted, quote_max_pages=1)
    assert not rejected.ok
    phone = ContextPhone(original='明天下午大家记得带好会议材科')
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=wanted,
                          quote_context='请看活动安排', quote_max_pages=1)
    assert result.ok, result.message
    assert result.meta['quote_text'] == phone.original


def test_quote_scroll_reacquires_current_element_index():
    class OlderPhone(QuotePhone):
        older = False

        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            if self.state == 'chat':
                result.elements[1].text = self.original if self.older else '最新的另一句话'
                result.elements[1].index = 22 if self.older else 2
            return result

        def swipe(self, **kwargs):
            self.older = True
            return super().swipe(**kwargs)

    phone = OlderPhone()
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original)
    assert result.ok, result.message
    assert ('hold', 22) in phone.calls
    assert ('hold', 2) not in phone.calls


def test_quoted_send_is_never_retapped_when_observation_is_uncertain():
    class UncertainPhone(QuotePhone):
        def tap(self, *, element=None, **kwargs):
            result = super().tap(element=element, **kwargs)
            if element == 6:
                self.state = 'typed'
            return result

    phone = UncertainPhone()
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original)
    assert not result.ok
    assert result.meta['delivery_attempted']
    assert phone.calls.count(('tap', 6)) == 1
    assert phone.calls.count(('hold', 2)) == 1


def test_context_returns_quote_anchors_through_public_collection(monkeypatch):
    from plugins.phone_use.wechat_context import collect_context
    phone = QuotePhone()
    result = collect_context(phone, 'Example Chat', include_images=False, max_pages=1)
    assert result.ok
    original = next(m for m in result.meta['message_candidates'] if m['text'] == phone.original)
    assert original['sender'] == 'Alice'
    assert original['page'] == 1
    assert original['bounds'] == [180, 450, 800, 510]


def test_quote_approval_describes_original_sender_and_context():
    from plugins.phone_use.tool import _summarize_action
    description = _summarize_action('wechat_reply', {
        'chat': 'Example Chat', 'text': '好的', 'quote_text': '原话',
        'quote_sender': 'Alice', 'quote_context': '相邻消息',
    })
    assert all(value in description for value in ['原话', 'Alice', '相邻消息', '好的'])


def test_existing_original_cannot_confirm_identical_reply_delivery():
    phone = QuotePhone()
    result = wechat.reply(phone, 'Example Chat', phone.original, quote_text=phone.original)
    assert not result.ok
    assert result.meta['delivery_status'] == 'uncertain'
    assert result.meta['quote_verified']
    assert phone.calls.count(('tap', 6)) == 1


def test_quote_target_is_part_of_automatic_reply_deduplication(monkeypatch):
    from plugins.phone_use import tool
    from plugins.phone_use.policy import PolicyDecision
    tool.reset_backend_for_tests()
    dispatched = []
    monkeypatch.setattr(tool, '_get_backend', lambda: object())
    monkeypatch.setattr(tool, '_dispatch', lambda *args: dispatched.append(args) or json.dumps({
        'ok': True, 'meta': {'delivery_attempted': True, 'delivery_status': 'confirmed'},
    }))
    decision = PolicyDecision(behavior='auto', allowed_actions=frozenset({'wechat_reply'}),
                              instruction_source=True)
    args = {'action': 'wechat_reply', 'chat': 'Example Chat', 'text': '好的'}
    try:
        with tool.bind_event_policy(decision):
            for original in ['第一条原话', '第一条原话', '第二条原话']:
                tool.handle_phone_use({**args, 'quote_text': original},
                                      task_id='quote-turn', session_id='telegram:example')
        assert len(dispatched) == 2
    finally:
        tool.reset_backend_for_tests()


def test_wrapped_original_and_preview_are_reassembled():
    class WrappedPhone(QuotePhone):
        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            result.elements[1].text = '明天下午三点开会'
            result.elements.append(node(12, '请带上会议材料', 518, attributes={'sender': 'Alice'}))
            if self.state in {'quote', 'typed'}:
                preview = next(e for e in result.elements if e.index == 5)
                preview.text = 'Alice：明天下午三点开会'
                result.elements.append(node(13, '请带上会议材料', 1728))
            return result

    phone = WrappedPhone(original='明天下午三点开会请带上会议材料')
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original, quote_sender='Alice')
    assert result.ok, result.message


def test_sender_can_be_confirmed_by_preview_when_ocr_has_no_sender_metadata():
    class OCRPhone(QuotePhone):
        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            result.elements[1].attributes.clear()
            return result

    phone = OCRPhone()
    result = wechat.reply(phone, 'Example Chat', '好的', quote_text=phone.original, quote_sender='Alice')
    assert result.ok, result.message
