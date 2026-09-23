import hashlib
import subprocess

import pytest

from plugins.phone_use.adb_backend import AdbBackend, read_attachment
from plugins.phone_use.backend import ActionResult, CaptureResult, UIElement
from plugins.phone_use.wechat import send_attachment


class AttachmentPhone:
    labels = [['More'], ['File'], ['Phone storage'], ['Download'], ['hermes-example.pdf'],
              ['Send to: Example', 'hermes-example.pdf', 'Send']]

    def __init__(self):
        self.stage = -1
        self.sent = 0
        self.home = False
        self.recipient = 'Example'

    def launch_app(self, *_):
        return ActionResult(ok=True, action='launch_app')

    def capture(self, mode):
        labels = ['Example'] if self.stage < 0 else list(self.labels[self.stage])
        if self.stage == 5:
            labels[0] = 'Send to: ' + self.recipient
        elements = [UIElement(index=i + 1, class_name='Text', text=text,
                              bounds=(250, 100 + i * 100, 750, 160 + i * 100))
                    for i, text in enumerate(labels)]
        if self.stage < 0:
            elements.append(UIElement(index=99, class_name='host.ocr.MessageInput',
                                      bounds=(100, 2000, 700, 2100)))
        return CaptureResult(mode=mode, width=1080, height=2400,
                             current_package='com.tencent.mm', elements=elements)

    def stage_attachment(self, path, digest):
        self.stage = 0
        return {'name': 'hermes-example.pdf', 'sha256': digest}

    def tap(self, **_):
        if self.stage == 5:
            self.sent += 1
        else:
            self.stage += 1
        return ActionResult(ok=True, action='tap')

    def keyevent(self, key):
        self.home = key == 'HOME'


def test_matching_recipient_file_summary_gets_one_attempt_and_home():
    phone = AttachmentPhone()
    result = send_attachment(phone, 'Example', '/unused.pdf', 'digest')
    assert phone.sent == 1
    assert phone.home
    assert result.meta['delivery_status'] == 'uncertain'


def test_wrong_recipient_never_sends():
    phone = AttachmentPhone()
    phone.recipient = 'Other'
    result = send_attachment(phone, 'Example', '/unused.pdf', 'digest')
    assert phone.sent == 0
    assert result.meta['delivery_attempted'] is False


def test_exception_after_send_is_uncertain_and_never_retried():
    class BrokenPhone(AttachmentPhone):
        def tap(self, **kwargs):
            result = super().tap(**kwargs)
            if self.sent:
                raise RuntimeError('connection lost')
            return result
    phone = BrokenPhone()
    result = send_attachment(phone, 'Example', '/unused.pdf', 'digest')
    assert phone.sent == 1
    assert result.meta['delivery_status'] == 'uncertain'


def test_changed_file_is_rejected_before_adb(tmp_path):
    path = tmp_path / 'report.pdf'
    path.write_bytes(b'approved')
    metadata, _ = read_attachment(str(path))
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        AdbBackend().stage_attachment(str(path), metadata['sha256'])


def test_adb_push_uses_snapshot_and_verifies_remote_hash(tmp_path, monkeypatch):
    path = tmp_path / 'report.pdf'
    content = b'approved document'
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    backend = AdbBackend()
    calls = []

    def adb(*args, **kwargs):
        from pathlib import Path
        assert args[0] == 'push'
        assert Path(args[1]).read_bytes() == content
        assert args[2].startswith('/sdcard/Download/hermes-')
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(backend, '_adb', adb)
    monkeypatch.setattr(backend, '_adb_shell', lambda *args: subprocess.CompletedProcess(
        args, 0, digest + '  file\n', ''))
    result = backend.stage_attachment(str(path), digest)
    assert result['sha256'] == digest
    assert len(calls) == 1


def test_public_tool_approval_binds_file_contents(tmp_path, monkeypatch):
    from plugins.phone_use import tool
    from plugins.phone_use.policy import PhonePolicy
    path = tmp_path / 'report.pdf'
    path.write_bytes(b'document')
    approvals = []
    monkeypatch.setattr(tool, 'get_policy', lambda: PhonePolicy())
    monkeypatch.setattr(tool, '_request_approval', lambda action, args, **kwargs:
                        approvals.append(tool._summarize_action(action, args)) or 'declined')
    result = tool.handle_phone_use({'action': 'wechat_send_attachment', 'chat': 'Example',
                                    'file_path': str(path)})
    assert result == 'declined'
    assert hashlib.sha256(b'document').hexdigest() in approvals[0]
    assert 'Example' in approvals[0]
