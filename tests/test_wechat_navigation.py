"""Navigation must distinguish WeChat's top-level tabs from chat composers."""
import pytest

from plugins.phone_use import host_ocr, wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement


def text(index, label, bounds):
    return UIElement(index=index, class_name='host.ocr.Text', text=label,
                     bounds=bounds, clickable=True)


def test_open_first_chat_row_without_search_or_bottom_tab_taps():
    calls = []
    list_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'WeChat', (456, 107, 628, 155)),
                  text(2, 'Example Group', (202, 244, 523, 287)),
                  text(3, 'Contacts', (337, 2287, 471, 2318))],
    )
    chat_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm',
        elements=[text(1, 'Example Group', (320, 104, 704, 157)),
                  UIElement(index=2, class_name='host.ocr.MessageInput',
                            bounds=(110, 2165, 720, 2325))],
    )

    class Backend:
        def launch_app(self, package):
            return ActionResult(ok=True, action='launch_app')

        def capture(self, mode):
            return chat_page if calls else list_page

        def tap(self, **kwargs):
            calls.append(kwargs)
            assert kwargs == {'element': 2}
            return ActionResult(ok=True, action='tap')

    result = wechat.open_chat(Backend(), 'Example Group')
    assert result.ok, result.message
    assert calls == [{'element': 2}]


@pytest.mark.parametrize('title', ['Contacts', '通讯录', 'Discover', 'Me'])
def test_top_level_tabs_never_gain_a_synthetic_composer(title):
    elements = [text(1, title, (430, 106, 650, 155)),
                text(2, 'Example Group', (180, 320, 520, 390)),
                text(3, 'WeChat', (76, 2284, 196, 2317)),
                text(4, 'Contacts', (337, 2287, 471, 2318))]
    enriched = host_ocr.add_semantic_regions(
        elements, package='com.tencent.mm', width=1080, height=2400,
    )
    assert not any(e.class_name == 'host.ocr.MessageInput' for e in enriched)


def test_recovery_ignores_synthetic_composer_on_contacts_page():
    contacts = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'Contacts', (430, 106, 650, 155)),
                  text(2, 'WeChat', (76, 2284, 196, 2317)),
                  text(3, 'Contacts', (337, 2287, 471, 2318)),
                  UIElement(index=4, class_name='host.ocr.MessageInput',
                            bounds=(110, 2165, 720, 2325))],
    )
    assert not wechat._chat_is_open(contacts, 'Contacts')


def test_search_waits_for_result_instead_of_tapping_keyboard_suggestion():
    def cap(elements):
        return CaptureResult(mode='hierarchy', width=1080, height=2400,
                             current_package='com.tencent.mm', elements=elements)

    search_page = cap([text(1, 'Search local or internet results', (184, 135, 823, 178))])
    suggestion = cap([text(1, 'Example Group', (200, 1550, 800, 1600))])
    result_page = cap([text(1, 'Top Hits', (41, 292, 185, 333)),
                       text(2, 'Example Group', (200, 420, 700, 480))])
    chat_page = cap([text(1, 'Example Group', (320, 104, 704, 157)),
                     UIElement(index=3, class_name='host.ocr.MessageInput',
                               bounds=(110, 2165, 720, 2325))])
    list_page = cap([UIElement(index=9, class_name='host.ocr.WeChatSearch',
                               bounds=(831, 84, 972, 204))])
    captures = iter([search_page, suggestion, result_page, chat_page])
    taps = []

    class Backend:
        def tap(self, **kwargs):
            taps.append(kwargs['element'])
            return ActionResult(ok=True, action='tap')

        def set_text(self, value):
            return ActionResult(ok=True, action='set_text')

        def capture(self, mode):
            return next(captures)

    result = wechat._search_for_chat(Backend(), list_page, 'Example Group')
    assert result.ok, result.message
    assert taps == [9, 2]
