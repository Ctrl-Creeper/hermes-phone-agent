from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.wechat import search_history


class SearchPhone:
    def __init__(self):
        self.stage = 0
        self.query = ''
        self.home = False
        self.scrolls = 0
        self.change_query = False
        self.empty = False

    def launch_app(self, *_):
        return ActionResult(ok=True, action='launch_app')

    def capture(self, mode):
        labels = ['Example', 'Chat Info'] if self.stage == 0 else ['Search Chat History']
        elements = [UIElement(index=i + 1, class_name='Text', text=text,
                              bounds=(250, 100 + i * 300, 750, 160 + i * 300))
                    for i, text in enumerate(labels)]
        if self.stage == 0:
            elements.append(UIElement(index=9, class_name='host.ocr.MessageInput',
                                      bounds=(100, 2000, 700, 2100)))
        if self.stage >= 2:
            elements.append(UIElement(index=8, class_name='android.widget.EditText',
                                      text='other' if self.change_query else self.query,
                                      bounds=(200, 100, 800, 200)))
        if self.stage == 3:
            elements.append(UIElement(index=10, class_name='Text',
                                      text='No results' if self.empty else 'meeting tomorrow',
                                      bounds=(200, 600, 800, 700)))
        return CaptureResult(mode=mode, width=1080, height=2400,
                             current_package='com.tencent.mm', elements=elements)

    def tap(self, *, element):
        if element != 8:
            self.stage += 1
        return ActionResult(ok=True, action='tap')

    def set_text(self, text):
        self.query = text
        return ActionResult(ok=True, action='set_text')

    def keyevent(self, key):
        if key == 'HOME':
            self.home = True
        if key == 'ENTER':
            self.stage = 3
        return ActionResult(ok=True, action='keyevent')

    def swipe(self, **_):
        self.scrolls += 1
        return ActionResult(ok=True, action='swipe')

    def wait(self, _):
        return ActionResult(ok=True, action='wait')


def test_history_returns_observed_snippet_and_stops_at_repeat():
    phone = SearchPhone()
    result = search_history(phone, 'Example', 'meeting', max_pages=8)
    assert result.ok, result.message
    assert result.meta['results'] == [{'text': 'meeting tomorrow', 'page': 1}]
    assert result.meta['coverage'] == 'partial'
    assert result.meta['stop_reason'] == 'repeated_page'
    assert phone.scrolls == 1
    assert phone.home


def test_changed_query_discards_results():
    phone = SearchPhone()
    phone.change_query = True
    result = search_history(phone, 'Example', 'meeting')
    assert not result.ok
    assert 'results' not in result.meta
    assert phone.home


def test_no_results_is_explicit():
    phone = SearchPhone()
    phone.empty = True
    result = search_history(phone, 'Example', 'meeting')
    assert result.ok
    assert result.meta['results'] == []
    assert result.meta['stop_reason'] == 'no_results'


def test_public_dispatch_routes_history_request():
    import json
    from plugins.phone_use.tool import _dispatch
    result = json.loads(_dispatch(SearchPhone(), 'wechat_search_history', {
        'chat': 'Example', 'query': 'meeting', 'max_pages': 1}))
    assert result['ok']
    assert result['meta']['query'] == 'meeting'
