from plugins.phone_use import wechat_context as context
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement


def node(index, text, bounds, *, desc=''):
    return UIElement(index=index, class_name='android.widget.TextView', text=text,
                     content_desc=desc, bounds=bounds, clickable=True)


def capture(*nodes):
    return CaptureResult(mode='hierarchy', width=1080, height=2400,
                         current_package='com.tencent.mm', elements=[
                             node(1, 'Example Chat', (300, 100, 760, 160)), *nodes])


class VoiceBackend:
    def __init__(self, menu='Convert to Text', transcript='明天下午三点开会'):
        self.state = 'chat'
        self.menu = menu
        self.transcript = transcript
        self.calls = []

    def capture(self, mode='hierarchy'):
        voice = node(2, '', (150, 450, 650, 540), desc='Voice message, 5 seconds')
        if self.state == 'menu':
            return capture(voice, node(3, self.menu, (300, 900, 760, 990)))
        if self.state == 'converted' and self.transcript:
            return capture(voice, node(4, self.transcript, (150, 550, 700, 640)))
        return capture(voice)

    def long_press(self, **kwargs):
        self.calls.append(('long_press', kwargs['element']))
        self.state = 'menu'
        return ActionResult(ok=True, action='long_press')

    def tap(self, **kwargs):
        self.calls.append(('tap', kwargs['element']))
        self.state = 'converted'
        return ActionResult(ok=True, action='tap')

    def keyevent(self, key):
        self.calls.append(('key', key))
        self.state = 'chat'
        return ActionResult(ok=True, action='keyevent')

    def wait(self, seconds):
        return ActionResult(ok=True, action='wait')


def collect(monkeypatch, backend, **kwargs):
    monkeypatch.setattr(context, 'open_chat', lambda *a: ActionResult(
        ok=True, action='wechat_open_chat', capture=backend.capture()))
    return context.collect_context(backend, 'Example Chat', include_images=False,
                                   max_pages=1, transcribe_voice=True, **kwargs)


def test_context_collects_wechat_voice_transcript(monkeypatch):
    backend = VoiceBackend()
    result = collect(monkeypatch, backend)
    assert result.ok
    assert result.meta['voice_transcripts'][0]['text'] == '明天下午三点开会'
    assert backend.calls == [('long_press', 2), ('tap', 3)]
    assert '明天下午三点开会' in result.meta['lines']


def test_missing_conversion_menu_is_reported_and_dismissed(monkeypatch):
    backend = VoiceBackend(menu='Delete')
    result = collect(monkeypatch, backend)
    assert result.meta['voice_transcripts'][0]['status'] == 'conversion_unavailable'
    assert backend.calls == [('long_press', 2), ('key', 'BACK')]


def test_transcription_timeout_does_not_invent_text(monkeypatch):
    result = collect(monkeypatch, VoiceBackend(transcript=''))
    assert result.meta['voice_transcripts'][0]['status'] == 'unconfirmed'
    assert 'text' not in result.meta['voice_transcripts'][0]


def test_voice_budget_zero_never_touches_message(monkeypatch):
    backend = VoiceBackend()
    result = collect(monkeypatch, backend, max_voice=0)
    assert result.meta['voice_transcripts'] == []
    assert backend.calls == []


def test_duration_text_alone_is_not_a_voice_message():
    assert context._voice_bubbles(capture(node(2, '5"', (150, 450, 650, 540)))) == []


def test_chinese_conversion_menu(monkeypatch):
    backend = VoiceBackend(menu='转文字')
    assert collect(monkeypatch, backend).meta['voice_count'] == 1


def test_changed_chat_stops_context_collection(monkeypatch):
    class ChangedChat(VoiceBackend):
        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            if self.state == 'converted':
                result.elements[0].text = 'Unrelated Chat'
            return result

    result = collect(monkeypatch, ChangedChat())
    assert not result.ok
    assert result.meta['stop_reason'] == 'chat_changed'


def test_voice_layout_shift_does_not_attribute_nearby_text(monkeypatch):
    class ShiftedVoice(VoiceBackend):
        def capture(self, mode='hierarchy'):
            result = super().capture(mode)
            if self.state == 'converted':
                result.elements[1].bounds = (150, 250, 650, 340)
            return result

    result = collect(monkeypatch, ShiftedVoice())
    assert result.meta['voice_count'] == 0
    assert result.meta['voice_transcripts'][0]['status'] == 'unconfirmed'


def test_menu_recovery_failure_stops_collection(monkeypatch):
    class FailedBack(VoiceBackend):
        def keyevent(self, key):
            raise RuntimeError('device disconnected')

    result = collect(monkeypatch, FailedBack(menu='Delete'))
    assert not result.ok
    assert result.meta['stop_reason'] == 'recovery_failed'


def test_voice_options_reach_real_context_dispatch(monkeypatch):
    import json
    from plugins.phone_use import tool
    backend = VoiceBackend(menu='转文字')
    monkeypatch.setattr(context, 'open_chat', lambda *a: ActionResult(
        ok=True, action='wechat_open_chat', capture=backend.capture()))
    result = json.loads(tool._dispatch(backend, 'wechat_collect_context', {
        'chat': 'Example Chat', 'transcribe_voice': True, 'max_voice': 1,
        'include_images': False, 'max_pages': 1,
    }))
    assert result['voice_count'] == 1
    assert result['voice_transcripts'][0]['text'] == '明天下午三点开会'


def test_changing_partial_transcription_is_not_accepted(monkeypatch):
    class ChangingVoice(VoiceBackend):
        counter = 0

        def capture(self, mode='hierarchy'):
            if self.state == 'converted':
                self.counter += 1
                self.transcript = '识别中的内容' + str(self.counter)
            return super().capture(mode)

    result = collect(monkeypatch, ChangingVoice())
    assert result.meta['voice_count'] == 0
    assert 'text' not in result.meta['voice_transcripts'][0]


def test_builtin_conversion_failure_has_explicit_status(monkeypatch):
    result = collect(monkeypatch, VoiceBackend(transcript='转换失败'))
    assert result.meta['voice_transcripts'][0]['status'] == 'conversion_failed'
    assert result.meta['voice_count'] == 0


def test_other_package_with_same_title_never_gets_long_press(monkeypatch):
    class OtherApp(VoiceBackend):
        def capture(self, mode='hierarchy'):
            page = super().capture(mode)
            page.current_package = 'com.example.other'
            return page
    backend = OtherApp()
    result = collect(monkeypatch, backend)
    assert not result.ok
    assert backend.calls == []
