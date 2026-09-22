from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.wechat import favorite_message


class FavoritePhone:
    def __init__(self):
        self.state = 'chat'
        self.attempts = 0
        self.home = False
        self.duplicate = False
        self.success = True

    def launch_app(self, *_):
        return ActionResult(ok=True, action='launch_app')

    def capture(self, mode):
        labels = [(1, 'Example', 100), (2, 'Keep this note', 500)]
        if self.duplicate:
            labels.append((3, 'Keep this note', 700))
        if self.state == 'menu':
            labels.append((4, '收藏', 900))
        if self.state == 'done' and self.success:
            labels.append((5, '已收藏', 1100))
        elements = [UIElement(index=i, class_name='Text', text=t, bounds=(250, y, 750, y+60))
                    for i, t, y in labels]
        elements.append(UIElement(index=9, class_name='host.ocr.MessageInput',
                                  bounds=(100, 2000, 700, 2100)))
        return CaptureResult(mode=mode, width=1080, height=2400,
                             current_package='com.tencent.mm', elements=elements)

    def long_press(self, **_):
        self.state = 'menu'
        return ActionResult(ok=True, action='long_press')

    def tap(self, **_):
        self.attempts += 1
        self.state = 'done'
        return ActionResult(ok=True, action='tap')

    def keyevent(self, key):
        self.home = key == 'HOME'
        return ActionResult(ok=True, action='keyevent')


def test_exact_unique_message_is_favorited_once_with_confirmation():
    phone = FavoritePhone()
    result = favorite_message(phone, 'Example', 'Keep this note')
    assert result.ok, result.message
    assert result.meta['favorite_status'] == 'confirmed'
    assert phone.attempts == 1
    assert phone.home


def test_duplicate_original_is_rejected():
    phone = FavoritePhone()
    phone.duplicate = True
    result = favorite_message(phone, 'Example', 'Keep this note')
    assert not result.ok
    assert phone.attempts == 0


def test_missing_notice_is_uncertain_without_repeat():
    phone = FavoritePhone()
    phone.success = False
    result = favorite_message(phone, 'Example', 'Keep this note')
    assert not result.ok
    assert result.meta['favorite_status'] == 'uncertain'
    assert phone.attempts == 1


def test_exception_after_injection_is_uncertain():
    class BrokenPhone(FavoritePhone):
        def tap(self, **kwargs):
            super().tap(**kwargs)
            raise RuntimeError('connection lost')
    phone = BrokenPhone()
    result = favorite_message(phone, 'Example', 'Keep this note')
    assert result.meta['favorite_status'] == 'uncertain'
    assert phone.attempts == 1


def test_approval_contains_original_and_chat():
    from plugins.phone_use.tool import _summarize_action
    summary = _summarize_action('wechat_favorite', {'chat': 'Example', 'message_text': 'Keep this note'})
    assert 'Example' in summary and 'Keep this note' in summary
