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


def test_recovery_accepts_list_reached_by_its_last_move():
    # Cold starts can spend every move waiting for WeChat to draw; the
    # screen produced by the final move must still be checked.
    loading = CaptureResult(mode='hierarchy', width=1080, height=2400,
                            current_activity='.ui.LauncherUI', elements=[])
    ready = CaptureResult(mode='hierarchy', width=1080, height=2400,
                          current_activity='.ui.LauncherUI',
                          elements=[text(1, 'WeChat', (456, 106, 624, 155))])
    screens = [loading, loading, loading, ready]

    class Backend:
        def tap(self, **_kwargs):
            return ActionResult(ok=True, action='tap')

        def capture(self, mode):
            return screens.pop(0)

    assert wechat._return_to_conversation_list(Backend(), loading).ok


@pytest.mark.parametrize('activity', [
    '.plugin.fts.ui.FTSMainUI',
    '.plugin.webview.ui.tools.fts.MMFTSSOSHomeWebViewUI',
])
def test_search_activity_is_never_an_open_chat(activity):
    # Search box looks like a composer and echoes the title.
    capture = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_activity=activity,
        elements=[
            text(1, 'Example Group', (456, 106, 624, 155)),
            UIElement(index=2, class_name=wechat._MESSAGE_INPUT_CLASS, text='',
                      bounds=(130, 2165, 720, 2325)),
        ],
    )
    assert wechat._find_chat_header(capture, 'Example Group') is not None
    assert not wechat._chat_is_open(capture, 'Example Group')


def test_open_chat_retaps_row_when_cold_start_drops_first_tap():
    # Right after a cold start WeChat draws the list but ignores the first tap.
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
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'Example Group', (320, 104, 704, 157)),
                  UIElement(index=2, class_name='host.ocr.MessageInput',
                            bounds=(110, 2165, 720, 2325))],
    )

    class Backend:
        def launch_app(self, package):
            return ActionResult(ok=True, action='launch_app')

        def capture(self, mode):
            return chat_page if len(calls) >= 2 else list_page

        def tap(self, **kwargs):
            calls.append(kwargs)
            return ActionResult(ok=True, action='tap')

    result = wechat.open_chat(Backend(), 'Example Group')
    assert result.ok, result.message
    assert calls == [{'element': 2}, {'x': 362, 'y': 265}]


def test_open_chat_retaps_search_when_cold_start_drops_first_tap():
    calls = []
    list_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'WeChat', (456, 107, 628, 155)),
                  UIElement(index=2, class_name=wechat._SEARCH_CONTROL_CLASS,
                            text='', bounds=(831, 84, 972, 204), clickable=True),
                  text(3, 'Contacts', (337, 2287, 471, 2318))],
    )
    search_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.plugin.fts.ui.FTSMainUI',
        elements=[text(1, 'Search local or internet results', (130, 123, 700, 196))],
    )

    class Backend:
        def launch_app(self, package):
            return ActionResult(ok=True, action='launch_app')

        def capture(self, mode):
            return search_page if len(calls) >= 2 else list_page

        def tap(self, **kwargs):
            calls.append(kwargs)
            return ActionResult(ok=True, action='tap')

        def set_text(self, value):
            calls.append(('set_text', value))
            return ActionResult(ok=False, action='set_text', message='stop here')

    wechat.open_chat(Backend(), 'Missing Chat')
    assert calls[:3] == [{'element': 2}, {'x': 901, 'y': 144}, ('set_text', 'Missing Chat')]


def test_recovery_leaves_an_open_chat_with_back_not_a_blind_tab_tap():
    # WeChat 8 hosts chats in LauncherUI; the tab-position tap lands on the
    # composer's voice button instead of leaving the chat.
    chat_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'Example Group(164)', (320, 104, 704, 157)),
                  UIElement(index=2, class_name='host.ocr.MessageInput',
                            bounds=(110, 2165, 720, 2325))],
    )
    list_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'WeChat', (456, 107, 628, 155))],
    )
    calls = []

    class Backend:
        def tap(self, **kwargs):
            calls.append(('tap', kwargs))
            return ActionResult(ok=True, action='tap')

        def keyevent(self, keycode):
            calls.append(('keyevent', keycode))
            return ActionResult(ok=True, action='keyevent')

        def capture(self, mode):
            return list_page

    assert wechat._return_to_conversation_list(Backend(), chat_page).ok
    assert calls == [('keyevent', 'BACK')]


def test_recovery_relaunches_wechat_after_backing_out_of_it():
    # A Back captured mid-animation can be repeated and exit WeChat; Back
    # on the Android launcher never returns.
    home = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.google.android.apps.nexuslauncher',
        current_activity='.NexusLauncherActivity', elements=[],
    )
    list_page = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[text(1, 'WeChat', (456, 107, 628, 155))],
    )
    calls = []

    class Backend:
        def launch_app(self, package):
            calls.append(('launch_app', package))
            return ActionResult(ok=True, action='launch_app')

        def keyevent(self, keycode):
            calls.append(('keyevent', keycode))
            return ActionResult(ok=True, action='keyevent')

        def capture(self, mode):
            return list_page if calls[-1][0] == 'launch_app' else home

    assert wechat._return_to_conversation_list(Backend(), home).ok
    assert calls == [('launch_app', 'com.tencent.mm')]
