"""Keep emoji identities intact through Android text input."""

import base64
import subprocess

import pytest

from plugins.phone_use import adb_backend
from plugins.phone_use.adb_backend import AdbBackend
from plugins.phone_use import wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement


@pytest.mark.parametrize('name', ['阿明😀', 'Example 👩🏽‍💻', 'Family 👨‍👩‍👧‍👦', '旗帜🇨🇳', '心❤️'])
def test_clipboard_preserves_emoji_sequences(name, monkeypatch):
    calls = []
    backend = AdbBackend(serial='emulator-5554')

    def shell(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(backend, '_adb_shell', shell)
    monkeypatch.setattr(adb_backend.time, 'sleep', lambda _: None)
    assert backend.type_text(name).ok
    clipboard = calls[0]
    encoded = clipboard[clipboard.index('text_b64') + 1]
    assert base64.b64decode(encoded).decode('utf-8') == name
    assert calls[-1] == ('input', 'keyevent', '279')


def screen(*texts):
    return CaptureResult(mode='hierarchy', width=1080, height=2400,
                         current_package='com.tencent.mm', elements=[
        UIElement(index=i+1, class_name='host.ocr.Text', text=text,
                  bounds=(200, 400+i*150, 700, 460+i*150))
        for i, text in enumerate(texts)])


class SearchBackend:
    def __init__(self, names, header):
        self.state = 'initial'
        self.query = None
        self.taps = []
        self.names = names
        self.header = header

    def tap(self, *, element):
        self.taps.append((self.state, element))
        self.state = 'search' if self.state == 'initial' else 'chat'
        return ActionResult(ok=True, action='tap')

    def set_text(self, text):
        self.query = text
        self.state = 'results'
        return ActionResult(ok=True, action='set_text')

    def capture(self, mode):
        if self.state == 'search':
            return screen('Search local or internet results')
        if self.state == 'results':
            # Literal placeholders are not indexed as the original emoji.
            return screen(*self.names) if self.query == '阿明' else screen()
        result = screen(self.header)
        result.elements[0].bounds = (320, 100, 760, 170)
        result.elements.append(UIElement(index=8, class_name='host.ocr.MessageInput',
                                        bounds=(110, 2165, 720, 2325)))
        return result


def search(backend, name):
    initial = screen()
    initial.elements.append(UIElement(index=9, class_name='host.ocr.WeChatSearch',
                                     bounds=(840, 80, 960, 190)))
    return wechat._search_for_chat(backend, initial, name)


@pytest.mark.parametrize('placeholder', ['[Sticker]', '[emoji]', '[表情]'])
def test_placeholder_search_uses_text_anchor_then_verifies_emoji_title(placeholder):
    backend = SearchBackend(['阿明😀'], '阿明😀')
    result = search(backend, '阿明' + placeholder)
    assert result.ok, result.message
    assert backend.query == '阿明'


def test_placeholder_search_does_not_choose_between_different_emoji_names():
    backend = SearchBackend(['阿明😀', '阿明😎'], '阿明😀')
    assert not search(backend, '阿明[Sticker]').ok
    assert backend.taps == [('initial', 9)]


def test_placeholder_cannot_match_another_text_suffix_or_missing_symbol():
    for name in ['阿明同学', '阿明']:
        assert wechat._find_text(screen(name).elements, '阿明[Sticker]') is None


def test_placeholder_without_text_does_not_search_or_select_top_hit():
    backend = SearchBackend(['😀'], '😀')
    assert not search(backend, '[emoji]').ok
    assert backend.query is None
    assert backend.taps == []


def test_placeholder_result_header_must_keep_selected_emoji():
    backend = SearchBackend(['阿明😀'], '阿明😎')
    assert not search(backend, '阿明[Sticker]').ok


@pytest.mark.parametrize('emoji', ['😀', '👩🏽‍💻', '👨‍👩‍👧‍👦', '🇨🇳', '❤️'])
def test_placeholder_matches_complete_emoji_sequences(emoji):
    assert wechat._matches_placeholder_title('阿明' + emoji, '阿明[Sticker]')
    assert not wechat._matches_placeholder_title('阿明' + emoji + '同学', '阿明[Sticker]')


def test_known_emoji_name_does_not_fuzzy_match_different_emoji():
    elements = screen('Example User😎').elements
    assert wechat._find_text(elements, 'Example User😀') is None
    header = screen('Example User😎')
    header.elements[0].bounds = (320, 100, 760, 170)
    assert wechat._find_chat_header(header, 'Example User😀') is None


def test_placeholder_header_alone_cannot_authorize_current_conversation():
    header = screen('阿明😀')
    header.elements[0].bounds = (320, 100, 760, 170)
    assert wechat._find_chat_header(header, '阿明[Sticker]') is None


def test_literal_bracketed_name_is_not_an_emoji_placeholder():
    assert not wechat._EMOJI_PLACEHOLDER.search('Example [Team]')
