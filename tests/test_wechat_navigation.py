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


def test_search_prefers_group_chat_over_search_results_with_same_title():
    def cap(elements, activity='.plugin.fts.ui.FTSMainUI'):
        return CaptureResult(mode='hierarchy', width=1080, height=2400,
                             current_package='com.tencent.mm',
                             current_activity=activity, elements=elements)

    search_page = cap([text(1, 'Search local or internet results', (184, 135, 823, 178))])
    result_page = cap([
        text(1, 'Group Chats', (41, 291, 255, 333)),
        text(2, '图• 老查留学吴宸丞2028Fall美本申..（9）', (44, 402, 918, 483)),
        text(3, '六 Search Results', (48, 589, 384, 632)),
        text(4, 'Q 老查留学吴宸丞2028Fall美本申请规划〉', (45, 697, 1035, 754)),
    ])
    chat_page = cap([
        text(1, '老查留学吴宸本申请规划（9）', (202, 107, 816, 164)),
        UIElement(index=2, class_name=wechat._MESSAGE_INPUT_CLASS,
                  bounds=(110, 2165, 720, 2325)),
    ], activity='.ui.LauncherUI')
    captures = iter([search_page, result_page, chat_page])
    taps = []

    class Backend:
        def tap(self, **kwargs):
            taps.append(kwargs['element'])
            return ActionResult(ok=True, action='tap')

        def set_text(self, value):
            return ActionResult(ok=True, action='set_text')

        def capture(self, mode):
            return next(captures)

    result = wechat._search_for_chat(
        Backend(),
        cap([UIElement(index=9, class_name=wechat._SEARCH_CONTROL_CLASS,
                       bounds=(831, 84, 972, 204))], activity='.ui.LauncherUI'),
        '老查留学吴宸丞2028Fall美本申请规划',
    )
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


def test_long_chat_header_allows_ocr_missing_middle_fragment():
    # Host OCR can drop a middle run from a long WeChat title while retaining
    # the distinctive prefix and suffix. The composer proves this is a chat,
    # but the old global 0.80 ratio rejected the opened destination.
    capture = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[
            text(1, '老查留学吴宸本申请规划（9）', (202, 107, 816, 164)),
            UIElement(index=2, class_name=wechat._MESSAGE_INPUT_CLASS,
                      bounds=(110, 2165, 720, 2325)),
        ],
    )
    assert wechat._chat_is_open(capture, '老查留学吴宸丞2028Fall美本申请规划')


def test_long_chat_header_ignores_navigation_chrome_in_ocr_label():
    capture = CaptureResult(
        mode='hierarchy', width=1080, height=2400,
        current_package='com.tencent.mm', current_activity='.ui.LauncherUI',
        elements=[
            text(1, '< 31老查留学吴宸丞2. 美本申请规划（9）込', (41, 100, 927, 161)),
            UIElement(index=2, class_name=wechat._MESSAGE_INPUT_CLASS,
                      bounds=(110, 2165, 720, 2325)),
        ],
    )
    assert wechat._chat_is_open(capture, '老查留学吴宸丞2028Fall美本申请规划')


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


def _search_cap(elements, activity='.plugin.fts.ui.FTSMainUI'):
    return CaptureResult(mode='hierarchy', width=1080, height=2400,
                         current_package='com.tencent.mm',
                         current_activity=activity, elements=elements)


class _SearchBackend:
    """Serves captures keyed by the last typed query."""

    def __init__(self, pages, chat_page=None):
        self.pages, self.chat_page = pages, chat_page
        self.queries, self.taps, self.query = [], [], None

    def tap(self, **kwargs):
        self.taps.append(kwargs.get('element'))
        return ActionResult(ok=True, action='tap')

    def set_text(self, value):
        self.queries.append(value)
        self.query = value
        return ActionResult(ok=True, action='set_text')

    def capture(self, mode):
        if self.query is None:
            return _search_cap([text(1, 'Search local or internet results', (184, 135, 823, 178))])
        if self.taps[-1] != 9 and self.chat_page is not None:
            return self.chat_page
        return self.pages.get(self.query, _search_cap([]))


_LIST = _search_cap([UIElement(index=9, class_name='host.ocr.WeChatSearch',
                               bounds=(831, 84, 972, 204))], '.ui.LauncherUI')


def test_search_falls_back_to_a_keyword_and_matches_the_full_name():
    # The name came from OCR ("Al" for "AI"); WeChat finds nothing for the
    # whole string but finds the chat for one of its words.
    keyword_page = _search_cap([text(1, 'Top Hits', (41, 292, 185, 333)),
                                text(2, 'Al Ethos 社团（26）', (200, 420, 700, 480))])
    chat_page = _search_cap([text(1, 'Al Ethos 社团(26)', (320, 104, 704, 157)),
                             UIElement(index=3, class_name='host.ocr.MessageInput',
                                       bounds=(110, 2165, 720, 2325))], '.ui.LauncherUI')
    backend = _SearchBackend({'Ethos': keyword_page}, chat_page)
    result = wechat._search_for_chat(backend, _LIST, 'Al Ethos 社团')
    assert result.ok, result.message
    assert backend.queries == ['Al Ethos 社团', 'Ethos']
    assert backend.taps == [9, 2]


def test_keyword_search_never_opens_a_chat_with_a_different_full_name():
    keyword_page = _search_cap([text(1, 'Group Chats', (41, 292, 185, 333)),
                                text(2, 'AI Ethos 社团（26）', (200, 420, 700, 480)),
                                text(3, 'Members: Ethos fan', (200, 490, 700, 530))])
    backend = _SearchBackend({'Ethos': keyword_page})
    result = wechat._search_for_chat(backend, _LIST, 'AI Ethos 社团core members')
    assert not result.ok
    assert backend.taps == [9]
    assert "WeChat showed: ['AI Ethos 社团']" in result.message


def test_search_never_taps_a_web_search_suggestion():
    web_page = _search_cap([text(1, 'Search Results', (114, 313, 384, 349)),
                            text(2, 'Example Group', (128, 425, 415, 468))])
    backend = _SearchBackend({'Example Group': web_page})
    result = wechat._search_for_chat(backend, _LIST, 'Example Group')
    assert not result.ok
    assert backend.taps == [9]


def test_search_never_matches_a_group_member_line():
    page = _search_cap([text(1, 'Group Chats', (41, 292, 185, 333)),
                        text(2, 'Study Group（9）', (200, 420, 700, 480)),
                        text(3, 'Members: Alexandra Example', (200, 490, 700, 530))])
    backend = _SearchBackend({'Alexandra Example': page, 'Alexandra': page, 'Example': page})
    result = wechat._search_for_chat(backend, _LIST, 'Alexandra Example')
    assert not result.ok
    assert backend.taps == [9]


def test_chat_header_is_the_title_bar_not_the_first_message_sender():
    # A message sender just below the title bar must not count as the title.
    chat_page = _search_cap([text(1, 'Other Group(17)', (320, 104, 704, 157)),
                             text(2, 'Void_DRS', (201, 239, 408, 298)),
                             UIElement(index=3, class_name='host.ocr.MessageInput',
                                       bounds=(110, 2165, 720, 2325))], '.ui.LauncherUI')
    assert wechat._find_chat_header(chat_page, 'Void_DRS') is None
    assert wechat._find_chat_header(chat_page, 'Other Group') is not None


_LAUNCHER_NO_BANNER = CaptureResult(mode='screenshot', width=1080, height=2400,
                                   current_package='com.google.android.apps.nexuslauncher')

@pytest.fixture
def emulator_clock(monkeypatch):
    """Real wait limits on a virtual clock; each capture costs 0.6 s as measured live."""
    clock = {"now": 0.0}
    monkeypatch.setattr(wechat, "time", type("Clock", (), {
        "monotonic": staticmethod(lambda: clock["now"]),
        "sleep": staticmethod(lambda seconds: clock.__setitem__("now", clock["now"] + seconds)),
    }))
    monkeypatch.setattr(wechat, "_WECHAT_DRAW_SECONDS", 8.0)
    monkeypatch.setattr(wechat, "_LIST_TAP_REACTION_SECONDS", 5.0)

    def timed(frames):
        frames = iter(frames)

        def capture(mode='hierarchy'):
            clock["now"] += 0.6
            if mode == 'screenshot':  # the banner check: a launcher with no notification
                return _LAUNCHER_NO_BANNER
            return next(frames)
        return capture
    return timed


def _visible_chat_backend(screens):
    """Serves *screens* in order; records every input so recovery is visible."""
    inputs = []

    class Backend:
        def __init__(self):
            self.screen = 0

        def launch_app(self, package):
            inputs.append("launch")
            return ActionResult(ok=True, action='launch_app')

        def capture(self, mode='hierarchy'):
            if mode == 'screenshot':  # the banner check: a launcher with no notification
                return _LAUNCHER_NO_BANNER
            capture = screens[min(self.screen, len(screens) - 1)]
            return capture

        def tap(self, *, element=None, x=None, y=None):
            inputs.append(("tap", element))
            self.screen += 1
            return ActionResult(ok=True, action='tap')

        def keyevent(self, key):
            inputs.append(("key", key))
            return ActionResult(ok=True, action='keyevent')

        def swipe(self, **kwargs):
            inputs.append("swipe")
            return ActionResult(ok=True, action='swipe')

        def set_text(self, text, element=None):
            inputs.append("type")
            return ActionResult(ok=True, action='set_text')

    return Backend(), inputs


def _page(*elements, activity='.ui.LauncherUI'):
    return CaptureResult(mode='hierarchy', width=1080, height=2400, current_package='com.tencent.mm',
                         current_activity=activity, elements=list(elements))


_VISIBLE_LIST = _page(text(1, 'WeChat', (456, 107, 628, 155)), text(2, 'Example Group', (202, 244, 523, 287)),
              text(3, 'WeChat', (100, 2287, 200, 2318)), text(4, 'Contacts', (337, 2287, 471, 2318)))
_VISIBLE_CHAT = _page(text(1, 'Example Group', (320, 104, 704, 157)),
              UIElement(index=2, class_name='host.ocr.MessageInput', bounds=(110, 2165, 720, 2325)))
_VISIBLE_OTHER = _page(text(1, 'Someone Else', (320, 104, 704, 157)),
                    UIElement(index=2, class_name='host.ocr.MessageInput', bounds=(110, 2165, 720, 2325)))


def test_visible_chat_reuses_an_already_open_chat_without_input():
    backend, inputs = _visible_chat_backend([_VISIBLE_CHAT])

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok and inputs == ["launch"]


def test_visible_chat_waits_out_a_stale_launch_frame_without_input(monkeypatch):
    monkeypatch.setattr(wechat.time, "sleep", lambda s: None)
    stale = _page(text(1, '1:29', (41, 16, 115, 53)))  # launcher still drawn, WeChat reported
    backend, inputs = _visible_chat_backend([stale])
    frames = iter([stale, stale, _VISIBLE_CHAT])
    backend.capture = lambda mode='hierarchy': next(frames)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok and inputs == ["launch"]


def test_visible_chat_taps_its_chat_list_row_once():
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST, _VISIBLE_CHAT])

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok and inputs == ["launch", ("tap", 2)]


def test_visible_chat_waits_for_a_slow_cold_open_instead_of_tapping_again(emulator_clock):
    # A cold WeChat keeps showing the list for seconds after a row tap that
    # did land; a second tap would then hit the chat as it opens.
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST])
    # ~3 s of list frames after a tap that did land, as recorded on a cold WeChat.
    frames = [_VISIBLE_LIST] * 7 + [_VISIBLE_CHAT, _VISIBLE_CHAT]
    backend.capture = emulator_clock(frames)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert [i for i in inputs if i != "launch"] == [("tap", 2)]


def test_visible_chat_repeats_a_dropped_list_tap_once():
    # The list is still there after the whole reaction window: the tap was dropped.
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST, _VISIBLE_LIST, _VISIBLE_CHAT])

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert [i for i in inputs if i != "launch"] == [("tap", 2), ("tap", None)]


@pytest.mark.parametrize("screens", [
    [_page(text(1, 'Settings', (420, 104, 660, 157)))],  # some unrelated page
    [_VISIBLE_LIST.__class__(**{**_VISIBLE_LIST.__dict__, 'elements': _VISIBLE_LIST.elements[:1] + _VISIBLE_LIST.elements[2:]})],  # row not visible
    [_VISIBLE_LIST, _VISIBLE_LIST],                    # WeChat ignored the row tap
])
def test_visible_chat_gives_up_without_any_recovery_gesture(screens, monkeypatch):
    monkeypatch.setattr(wechat.time, "sleep", lambda s: None)
    backend, inputs = _visible_chat_backend(screens)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert not result.ok
    assert all(i == "launch" or i[0] == "tap" for i in inputs)  # no Back, swipe, search or typing
    assert sum(1 for i in inputs if i != "launch") <= 2  # at most a row tap and its single retap


def test_visible_chat_waits_through_the_slide_in_frames(emulator_clock):
    # Between the list and the open chat, WeChat slides the chat in over the
    # list: the title is still off-centre, so the frame is neither.
    slide = _page(text(1, 'WeChat (61)', (0, 107, 160, 155)), text(2, 'Example Group', (860, 104, 1080, 157)),
                  UIElement(index=3, class_name='host.ocr.MessageInput', bounds=(110, 2165, 720, 2325)))
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST])
    frames = [_VISIBLE_LIST] * 3 + [slide, slide, _VISIBLE_CHAT, _VISIBLE_CHAT]
    backend.capture = emulator_clock(frames)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert [i for i in inputs if i != "launch"] == [("tap", 2)]


def test_open_chat_cold_start_uses_one_tap_and_no_recovery(emulator_clock):
    # Recorded on the emulator: splash for ~2 s after launch, the list, a row
    # tap that takes ~3 s to open the chat, then a slide-in transition.
    splash = _page(text(1, '1:43', (41, 16, 115, 53)))
    slide = _page(text(1, 'WeChat (61)', (0, 107, 160, 155)), text(2, 'Example Group', (860, 104, 1080, 157)),
                  UIElement(index=3, class_name='host.ocr.MessageInput', bounds=(110, 2165, 720, 2325)))
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST])
    frames = [splash] * 4 + [_VISIBLE_LIST] * 7 + [slide, _VISIBLE_CHAT, _VISIBLE_CHAT, _VISIBLE_CHAT]
    backend.capture = emulator_clock(frames)

    result = wechat.open_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == ["launch", ("tap", 2)]  # no Back, no retap, no search


def test_launcher_frame_reported_as_wechat_is_not_a_drawn_wechat_ui():
    # Recorded: Android reports WeChat in front while the launcher is still on
    # screen. Its status-bar icons must not become a chat title and invent a composer.
    launcher = [text(1, '•lg', (794, 10, 910, 56)), text(2, '2:31', (41, 17, 115, 49)),
                text(3, 'Fri, Nov 6', (69, 201, 262, 256)), text(4, 'Play Store', (73, 1757, 244, 1801)),
                text(5, 'Gmail', (362, 1757, 464, 1794))]
    enriched = host_ocr.add_semantic_regions(launcher, package='com.tencent.mm', width=1080, height=2400)
    frame = _page(*enriched)

    assert not any(e.class_name == 'host.ocr.MessageInput' for e in enriched)
    assert not wechat._wechat_ui_drawn(frame, 'Example Group')


def test_visible_chat_leaves_the_previous_chat_with_one_back(emulator_clock):
    # WeChat resumes on the chat of the previous reply; Back reaches the list.
    backend, inputs = _visible_chat_backend([_VISIBLE_OTHER])
    frames = [_VISIBLE_OTHER, _VISIBLE_LIST, _VISIBLE_CHAT, _VISIBLE_CHAT]
    backend.capture = emulator_clock(frames)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == ["launch", ("key", "BACK"), ("tap", 2)]


def test_visible_chat_never_presses_back_past_the_chat_list(emulator_clock):
    # Back that only closes the keyboard gets one more; Back on the list
    # itself would leave WeChat, so it never happens.
    backend, inputs = _visible_chat_backend([_VISIBLE_OTHER])
    frames = [_VISIBLE_OTHER, _VISIBLE_OTHER, _VISIBLE_LIST, _VISIBLE_CHAT, _VISIBLE_CHAT]
    backend.capture = emulator_clock(frames)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == ["launch", ("key", "BACK"), ("key", "BACK"), ("tap", 2)]


def test_visible_chat_gives_up_on_a_chat_that_back_does_not_leave(emulator_clock):
    backend, inputs = _visible_chat_backend([_VISIBLE_OTHER])
    backend.capture = emulator_clock([_VISIBLE_OTHER] * 6)

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert not result.ok
    assert inputs == ["launch", ("key", "BACK"), ("key", "BACK")]  # bounded; no taps, no search


def _banner_screen(title_line):
    """Recorded launcher frame with WeChat's heads-up banner (OCR boxes as captured live)."""
    capture = CaptureResult(mode='screenshot', width=1080, height=2400, png_b64='cG5n',
                            current_package='com.google.android.apps.nexuslauncher')
    return capture, [text(1, '3:32', (41, 17, 115, 49)), text(2, title_line, (229, 139, 579, 175)),
                     text(3, '［7］闫：连着上了好几天了', (226, 184, 624, 231)), text(4, 'Play Store', (73, 1757, 244, 1801))]


def _banner_backend(monkeypatch, emulator_clock, title_line, after_tap):
    screen, ocr = _banner_screen(title_line)
    monkeypatch.setattr(wechat, 'recognize_text', lambda png: ocr)
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST])
    frames = iter(after_tap)
    timed = emulator_clock(after_tap)

    def capture(mode='hierarchy'):
        return screen if mode == 'screenshot' else timed(mode)
    backend.capture = capture
    return backend, inputs


def test_tapping_the_notification_banner_jumps_straight_into_the_chat(monkeypatch, emulator_clock):
    backend, inputs = _banner_backend(monkeypatch, emulator_clock, 'Example Group•now',
                                      [_VISIBLE_CHAT, _VISIBLE_CHAT])

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == [("tap", None)]  # the banner itself: no launch, no list, no Back


@pytest.mark.parametrize("title_line", [
    'Someone Else•now',       # another chat's notification
    'Example Gro…•now',       # truncated name: never guess
    'Example Group',          # no "• now": not a notification title line
])
def test_banner_is_only_tapped_when_it_is_the_target_chats(monkeypatch, emulator_clock, title_line):
    backend, inputs = _banner_backend(monkeypatch, emulator_clock, title_line,
                                      [_VISIBLE_LIST, _VISIBLE_CHAT, _VISIBLE_CHAT])

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == ["launch", ("tap", 2)]  # normal list path; the banner was left alone


def test_banner_that_opens_another_chat_falls_back_to_navigation(monkeypatch, emulator_clock):
    # A newer notification replaced the banner between screenshot and tap, so
    # the tap opened another chat. It stays there until Back.
    screen, ocr = _banner_screen('Example Group•now')
    monkeypatch.setattr(wechat, 'recognize_text', lambda png: ocr)
    backend, inputs = _visible_chat_backend([_VISIBLE_LIST])
    state = {"page": "other"}
    pages = {"other": _VISIBLE_OTHER, "list": _VISIBLE_LIST, "chat": _VISIBLE_CHAT}
    timed = emulator_clock(iter(lambda: pages[state["page"]], None))
    backend.capture = lambda mode='hierarchy': screen if mode == 'screenshot' else timed(mode)
    real_key, real_tap = backend.keyevent, backend.tap
    backend.keyevent = lambda key: (state.update(page="list") if key == "BACK" else None, real_key(key))[1]
    backend.tap = lambda **kw: (state.update(page="chat") if kw.get("element") == 2 else None, real_tap(**kw))[1]

    result = wechat.open_visible_chat(backend, 'Example Group')

    assert result.ok
    assert inputs == [("tap", None), "launch", ("key", "BACK"), ("tap", 2)]
