"""Offline integration: real Hermes task lanes, notification buffers and receipts.

No model, Telegram, ADB or real WeChat messages. Run with this repo and an
installed Hermes on PYTHONPATH, using the Hermes Python environment.
"""
import asyncio
import hashlib
import os
import sys
import tempfile
import types
from pathlib import Path


async def check():
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.session import build_session_key

    namespace = types.ModuleType('hermes_plugins')
    namespace.__path__ = [str(Path(__file__).resolve().parents[1] / 'plugins')]
    sys.modules['hermes_plugins'] = namespace
    from hermes_plugins.phone_events.adapter import PhoneEventAdapter, _prepend_phone_report_context
    from hermes_plugins.phone_events.event_filter import EventFilter, PhoneEvent
    from hermes_plugins.phone_events.participation import guard_conversation_tool, record_conversation_reply
    from hermes_plugins.phone_use import _cleanup_workflow_at_turn_end, tool
    from hermes_plugins.phone_use.backend import ActionResult
    from hermes_plugins.phone_use.policy import get_event_policy

    class Transport(BasePlatformAdapter):
        async def connect(self): return True
        async def disconnect(self): pass
        async def send(self, *args, **kwargs): return SendResult(success=True)
        async def get_chat_info(self, chat_id): return {}

    transport = Transport(PlatformConfig(enabled=True, extra={}), Platform.TELEGRAM)
    owner = PhoneEventAdapter(PlatformConfig(enabled=True, extra={
        'telegram_chat_id': 'offline-report',
        'private_participation': {
            'enabled': True, 'quiet_seconds': .01, 'max_wait_seconds': .03,
        },
        'group_participation': {
            'enabled': True, 'mention_names': ['agent'], 'quiet_seconds': .01,
            'max_wait_seconds': .03, 'min_interval_seconds': .01,
        },
    }))
    owner._connected = True
    owner._participation_admission_open = True
    owner._event_filter = EventFilter(max_events_per_second=1)
    owner._message_handler = object()
    owner.gateway_runner = types.SimpleNamespace(
        config=types.SimpleNamespace(group_sessions_per_user=True),
        _gateway_loop=asyncio.get_running_loop(), adapters={Platform.TELEGRAM: transport},
        _session_key_for_source=build_session_key,
    )
    started_a, release_a, finished_b, finished_next = (asyncio.Event() for _ in range(4))
    seen, sends, task_ids = [], [], []
    private_done, private_silent = asyncio.Event(), asyncio.Event()

    def send(_backend, chat, text, *, on_delivery, **kwargs):
        sends.append((chat, text))
        on_delivery('attempted')
        on_delivery('confirmed')
        return ActionResult(ok=True, action='wechat_reply',
                            meta={'delivery_attempted': True, 'delivery_status': 'confirmed'})

    async def handler(message):
        batch = message.raw_message.meta['_participation_batch']
        seen.append((message.source.user_id, batch.destination, [e.body for e in batch.events]))
        if batch.conversation_type == 'private':
            assert 'private WeChat conversation' in message.channel_prompt
        turn_id = get_event_policy().delivery_identity
        task_ids.append(turn_id)
        try:
            if batch.events[0].body == '@agent first':
                started_a.set()
                await release_a.wait()
            if batch.conversation_type == 'private' and batch.events[0].body == 'thanks':
                assert _prepend_phone_report_context('NO_REPLY', platform='telegram') is None
                private_silent.set()
                return 'NO_REPLY'
            if not batch.invited:
                assert _prepend_phone_report_context('NO_REPLY', platform='telegram') is None
                finished_next.set()
                return 'NO_REPLY'
            args = {'action': 'wechat_reply', 'chat': batch.destination, 'text': 'offline answer'}
            assert guard_conversation_tool('phone_use', {**args, 'chat': 'unrelated chat'})['action'] == 'block'
            assert guard_conversation_tool('phone_use', args) is None
            result = await asyncio.to_thread(tool.handle_phone_use, args,
                                            task_id=turn_id, session_id=message.source.user_id)
            record_conversation_reply('phone_use', args, result)
            assert batch.replied
            if batch.conversation_type == 'private':
                private_done.set()
            if batch.destination == 'Group B':
                finished_b.set()
        finally:
            await asyncio.to_thread(_cleanup_workflow_at_turn_end,
                                    task_id=turn_id, session_id=message.source.user_id)

    transport.set_message_handler(handler)
    original_sender = tool.reply_to_wechat
    tool.reply_to_wechat = send
    def notify(chat, text, timestamp, kind='group'):
        owner._on_raw_event(PhoneEvent('notification', package='com.tencent.mm',
            title=chat, body=text, timestamp=timestamp,
            meta={'conversation_type': kind, 'conversation_title': chat,
                  # Android sbn.key: WeChat posts one notification id per conversation.
                  'notification_key': f"0|com.tencent.mm|{int(hashlib.sha256(f'{kind}:{chat}'.encode()).hexdigest()[:7], 16)}|null|10224",
                  '_transport': 'helper_socket'}))
    try:
        notify('Group A', '@agent first', 1)
        await asyncio.wait_for(started_a.wait(), 5)
        notify('Group A', 'new observation', 2)
        notify('Group B', '@agent second', 3)
        await asyncio.wait_for(finished_b.wait(), 5)
        assert not release_a.is_set()
        assert seen[0][2] == ['@agent first']
        release_a.set()
        await asyncio.wait_for(finished_next.wait(), 5)
        for _ in range(50):
            if not owner._phone_dispatch_futures:
                break
            await asyncio.sleep(.01)
        assert not owner._phone_dispatch_futures
        assert seen[0][0] != seen[1][0]
        assert seen[0][0] == seen[2][0]
        assert sends == [('Group B', 'offline answer'), ('Group A', 'offline answer')]
        # Same title as Group A, but a different conversation and notification id.
        notify('Group A', 'private request', 4, kind='private')
        notify('Group A', 'extra detail', 5, kind='private')
        await asyncio.wait_for(private_done.wait(), 5)
        private_lane = seen[-1][0]
        assert private_lane != seen[0][0]
        assert seen[-1][2] == ['private request', 'extra detail']
        notify('Group A', 'thanks', 6, kind='private')
        await asyncio.wait_for(private_silent.wait(), 5)
        assert seen[-1][0] == private_lane
        for _ in range(50):
            if not owner._phone_dispatch_futures:
                break
            await asyncio.sleep(.01)
        assert not owner._phone_dispatch_futures
        assert len(sends) == 3
        assert all(not tool._device_operation_queue.owns(task_id) for task_id in task_ids)
    finally:
        owner._close_participation_buffers()
        tool.reply_to_wechat = original_sender
    print('real Hermes notification-only private/group participation: passed')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='conversation-participation-') as directory:
        os.environ['HERMES_HOME'] = directory
        os.environ['HERMES_PHONE_BACKEND'] = 'noop'
        policy = Path(directory) / 'phone-policy.yaml'
        policy.write_text('''default_behavior: ignore
app_profiles: {}
event_rules:
  - match:
      package: com.tencent.mm
      event: notification
    behavior: auto
    instruction_source: true
    allowed_actions: [wechat_reply, wechat_collect_context]
''')
        os.environ['PHONE_POLICY_PATH'] = str(policy)
        asyncio.run(check())
