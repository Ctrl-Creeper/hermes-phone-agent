"""Anonymized screen transitions for the deterministic friend workflow."""

import pytest

from plugins.phone_use import wechat
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement


def element(index, text, bounds):
    return UIElement(index=index, class_name="host.ocr.Text", text=text,
                     bounds=bounds, clickable=True)


def page(*elements):
    return CaptureResult(mode="hierarchy", width=1080, height=2400,
                         current_package="com.tencent.mm", elements=list(elements))


class Screens:
    def __init__(self, screens, transitions):
        self.screens = screens
        self.transitions = transitions
        self.state = "start"
        self.taps = []

    def launch_app(self, package):
        return ActionResult(ok=True, action="launch_app")

    def capture(self, mode="hierarchy"):
        return self.screens[self.state]

    def tap(self, *, element):
        self.taps.append((self.state, element))
        self.state = self.transitions[(self.state, element)]
        return ActionResult(ok=True, action="tap")

    def keyevent(self, key):
        if key == "HOME":
            self.state = "home"
        return ActionResult(ok=True, action="keyevent")


def recommended_screens():
    return {
        "start": page(element(1, "WeChat", (450, 100, 630, 155)),
                      element(2, "Contacts", (337, 2287, 471, 2321))),
        # Observed on the emulator: New Friends is replaced by the latest
        # request's name and greeting below Recommended, above Group Chats.
        "contacts": page(element(1, "Contacts", (450, 100, 630, 155)),
                         element(2, "Recommended", (40, 200, 300, 245)),
                         element(3, "Example Applicant", (195, 280, 650, 330)),
                         element(4, "A greeting", (195, 350, 700, 390)),
                         element(5, "Group Chats", (195, 460, 500, 520))),
        "requests": page(element(1, "New Friends", (390, 106, 690, 155)),
                         element(2, "Example Applicant", (160, 420, 650, 485)),
                         element(3, "Accept", (830, 415, 1015, 490))),
        "added": page(element(1, "New Friends", (390, 106, 690, 155)),
                      element(2, "Example Applicant", (160, 420, 650, 485)),
                      element(3, "Added", (830, 415, 1015, 490))),
        "home": page(),
    }


def test_recommended_request_replaces_new_friends_entry():
    backend = Screens(recommended_screens(), {
        ("start", 2): "contacts", ("contacts", 3): "requests",
        ("requests", 3): "added",
    })
    result = wechat.accept_friend_request(backend, "Example Applicant")
    assert result.ok, result.message
    assert backend.taps == [("start", 2), ("contacts", 3), ("requests", 3)]
    assert backend.state == "home"


def test_view_opens_confirmation_then_done_without_editing_alias():
    screens = recommended_screens()
    screens['start'] = screens['requests']
    screens['start'].elements[2].text = 'View'
    screens['confirm'] = page(
        element(1, 'Confirm Friend Request', (250, 100, 850, 160)),
        element(2, 'New alias', (130, 350, 800, 460)),
        element(3, 'Done', (290, 2140, 790, 2270)),
    )
    backend = Screens(screens, {('start', 3): 'confirm', ('confirm', 3): 'added'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert result.ok, result.message
    assert backend.taps == [('start', 3), ('confirm', 3)]


def test_missing_accept_button_is_not_success():
    screens = recommended_screens()
    screens['start'] = screens['requests']
    screens['unknown'] = page(element(2, 'Example Applicant', (160, 420, 650, 485)))
    backend = Screens(screens, {('start', 3): 'unknown'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert not result.ok
    assert backend.taps == [('start', 3)]


def test_duplicate_requester_names_are_not_accepted():
    screens = recommended_screens()
    screens['start'] = screens['requests']
    screens['start'].elements += [
        element(4, 'Example Applicant', (160, 620, 650, 685)),
        element(5, 'Accept', (830, 615, 1015, 690)),
    ]
    backend = Screens(screens, {('start', 3): 'added'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert not result.ok
    assert backend.taps == []


@pytest.mark.parametrize('scale', [0.6666667, 1.0, 1.3333333])
@pytest.mark.parametrize('chinese', [False, True])
def test_confirmation_flow_scales_and_supports_chinese(scale, chinese):
    screens = recommended_screens()
    screens['requests'].elements[2].text = 'View'
    screens['confirm'] = page(
        element(1, 'Confirm Friend Request', (250, 100, 850, 160)),
        element(3, 'Done', (290, 2140, 790, 2270)),
    )
    translations = {'WeChat': '微信', 'Contacts': '通讯录', 'Recommended': '推荐',
                    'Group Chats': '群聊', 'New Friends': '新的朋友', 'View': '查看',
                    'Confirm Friend Request': '通过朋友验证', 'Done': '完成', 'Added': '已添加'}
    for screen in screens.values():
        screen.width = round(screen.width * scale)
        screen.height = round(screen.height * scale)
        for e in screen.elements:
            e.bounds = tuple(round(v * scale) for v in e.bounds)
            if chinese:
                e.text = translations.get(e.text, e.text)
    backend = Screens(screens, {('start', 2): 'contacts', ('contacts', 3): 'requests',
                                ('requests', 3): 'confirm', ('confirm', 3): 'added'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert result.ok, result.message
    assert backend.taps[-1] == ('confirm', 3)


def test_confirmation_without_done_does_not_submit_other_controls():
    screens = recommended_screens()
    screens['start'] = screens['requests']
    screens['start'].elements[2].text = 'View'
    screens['confirm'] = page(
        element(1, 'Confirm Friend Request', (250, 100, 850, 160)),
        element(2, 'Add Tag', (130, 1000, 800, 1150)),
    )
    backend = Screens(screens, {('start', 3): 'confirm'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert not result.ok
    assert result.meta['stage'] == 'confirmation'
    assert result.meta['acceptance_attempted'] is False
    assert backend.taps == [('start', 3)]


def test_already_added_is_success_without_another_tap():
    screens = recommended_screens()
    screens['start'] = screens['added']
    backend = Screens(screens, {})
    assert wechat.accept_friend_request(backend, 'Example Applicant').ok
    assert backend.taps == []


def test_unrelated_contacts_are_not_used_as_request_navigation():
    screens = recommended_screens()
    screens['contacts'].elements = [e for e in screens['contacts'].elements if e.text != 'Recommended']
    backend = Screens(screens, {('start', 2): 'contacts'})
    result = wechat.accept_friend_request(backend, 'Example Applicant')
    assert not result.ok
    assert backend.taps == [('start', 2)]


def test_next_request_action_is_not_used_for_target():
    screens = recommended_screens()
    screens['start'] = screens['requests']
    screens['start'].elements[2].bounds = (830, 550, 1015, 610)
    backend = Screens(screens, {})
    assert not wechat.accept_friend_request(backend, 'Example Applicant').ok
    assert backend.taps == []
