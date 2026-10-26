"""Notification-only participation; no device, model or Telegram traffic."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plugins.phone_events.event_filter import PhoneEvent
from plugins.phone_events.group_participation import GroupParticipation, ParticipationSettings


def event(body, *, chat='Group A', timestamp=1):
    return PhoneEvent('notification', package='com.tencent.mm', title=chat,
                      body=body, timestamp=timestamp, meta={
                          'conversation_title': chat, 'conversation_type': 'group',
                          'notification_key': chat, '_transport': 'helper_socket',
                      })


def settings(**kwargs):
    return ParticipationSettings(enabled=True, mention_names=('agent',),
                                 quiet_seconds=.02, max_wait_seconds=.06,
                                 min_interval_seconds=0, **kwargs)


def test_quiet_window_batches_notifications_without_running_work_on_admission():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(settings(), dispatch)
        scheduler.observe('A', event('first'), SimpleNamespace())
        scheduler.observe('A', event('second', timestamp=2), SimpleNamespace())
        assert batches == []
        await asyncio.wait_for(ready.wait(), 1)
        scheduler.close()
        assert [e.body for e in batches[0].events] == ['first', 'second']
        assert batches[0].metrics['observed_notifications'] == 2
    asyncio.run(scenario())


def test_an_invitation_bypasses_ordinary_evaluation_cooldown():
    async def scenario():
        batches, first, second = [], asyncio.Event(), asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            (first if len(batches) == 1 else second).set()
        config = replace(settings(), min_interval_seconds=60)
        scheduler = GroupParticipation(config, dispatch)
        scheduler.observe('A', event('ordinary'), None)
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.sleep(0)
        scheduler.observe('A', event('@agent question', timestamp=2), None)
        await asyncio.wait_for(second.wait(), 1)
        scheduler.close()
        assert batches[1].invited is True
    asyncio.run(scenario())


def test_waiting_group_does_not_block_another_group_and_running_batch_is_fixed():
    async def scenario():
        batches, entered, release, other = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            if batch.lane == 'A' and len(batches) == 1:
                entered.set()
                await release.wait()
            if batch.lane == 'B':
                other.set()
        scheduler = GroupParticipation(settings(), dispatch)
        scheduler.observe('A', event('@agent first'), None)
        await asyncio.wait_for(entered.wait(), 1)
        scheduler.observe('A', event('next', timestamp=2), None)
        scheduler.observe('B', event('@agent B', chat='Group B'), None)
        await asyncio.wait_for(other.wait(), 1)
        assert [e.body for e in batches[0].events] == ['@agent first']
        assert len([b for b in batches if b.lane == 'A']) == 1
        release.set()
        for _ in range(20):
            if len(batches) == 3:
                break
            await asyncio.sleep(.01)
        scheduler.close()
        assert [e.body for e in batches[2].events] == ['next']
    asyncio.run(scenario())


def test_replayed_notification_is_ignored_but_identical_later_message_survives():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(settings(), dispatch)
        original = event('same')
        scheduler.observe('A', original, None)
        scheduler.observe('A', event('same'), None)
        scheduler.observe('A', event('same', timestamp=2), None)
        await asyncio.wait_for(ready.wait(), 1)
        scheduler.close()
        assert [e.timestamp for e in batches[0].events] == [1, 2]
    asyncio.run(scenario())


def test_cumulative_notification_updates_contribute_only_new_lines():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(settings(), dispatch)
        scheduler.observe('A', event('Alice: first'), None)
        scheduler.observe('A', event('Alice: first\nBob: second', timestamp=2), None)
        scheduler.observe('A', event('Bob: second\nCarol: third', timestamp=3), None)
        await asyncio.wait_for(ready.wait(), 1)
        scheduler.close()
        assert [e.body for e in batches[0].events] == ['Alice: first', 'Bob: second', 'Carol: third']
    asyncio.run(scenario())


def test_ordinary_evaluations_are_rate_limited_without_periodic_empty_calls():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(replace(settings(), min_interval_seconds=.1), dispatch)
        scheduler.observe('A', event('first'), None)
        await asyncio.wait_for(ready.wait(), 1)
        await asyncio.sleep(0)
        scheduler.observe('A', event('second', timestamp=2), None)
        await asyncio.sleep(.04)
        assert len(batches) == 1
        await asyncio.sleep(.1)
        assert len(batches) == 2
        await asyncio.sleep(.1)
        scheduler.close()
        assert len(batches) == 2
    asyncio.run(scenario())


def test_bounded_pending_context_keeps_invitation_and_latest_chatter():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(settings(max_messages=3), dispatch)
        scheduler.observe('A', event('@agent question'), None)
        for n in range(2, 7):
            scheduler.observe('A', event(str(n), timestamp=n), None)
        await asyncio.wait_for(ready.wait(), 1)
        scheduler.close()
        assert [e.body for e in batches[0].events] == ['@agent question', '5', '6']
        assert batches[0].metrics['omitted_notifications'] == 3
    asyncio.run(scenario())


def test_close_discards_pending_only_and_does_not_cancel_running_batch():
    async def scenario():
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def dispatch(batch):
            entered.set()
            await release.wait()
            completed.set()
        scheduler = GroupParticipation(settings(), dispatch)
        scheduler.observe('A', event('@agent hello'), None)
        await asyncio.wait_for(entered.wait(), 1)
        scheduler.observe('A', event('pending', timestamp=2), None)
        scheduler.close()
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
    asyncio.run(scenario())


def test_same_notification_key_with_conflicting_chat_title_is_not_merged():
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            ready.set()
        scheduler = GroupParticipation(settings(), dispatch)
        scheduler.observe('shared-key', event('one'), None)
        assert scheduler.observe('shared-key', event('two', chat='Group B'), None) is False
        await asyncio.wait_for(ready.wait(), 1)
        scheduler.close()
        assert [e.body for e in batches[0].events] == ['one']
    asyncio.run(scenario())


@pytest.mark.parametrize('config', [
    {'enabled': True},
    {'enabled': True, 'mention_names': ['agent'], 'quiet_seconds': float('nan')},
    {'enabled': True, 'mention_names': ['agent'], 'max_messages': 0},
])
def test_invalid_enabled_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        ParticipationSettings.from_config(config)


def authorized_policy(behavior='auto'):
    from plugins.phone_use.policy import PhonePolicy, EventRule
    return PhonePolicy(event_rules=[EventRule(
        package='com.tencent.mm', event_type='notification', conversation_type='group',
        behavior=behavior, instruction_source=True,
        allowed_actions=frozenset({'wechat_reply', 'wechat_collect_context'}),
    )])


def make_owner(monkeypatch, dispatch, *, policy=None):
    from plugins.phone_events import adapter
    from plugins.phone_events.event_filter import EventFilter
    owner = object.__new__(adapter.PhoneEventAdapter)
    owner._group_settings = settings()
    owner._group_participation = None
    owner._event_filter = EventFilter(max_events_per_second=1)
    owner._redact_otp = True
    owner._serial = 'offline-device'
    owner._message_handler = object()
    owner._wechat_assume_unmentioned_private = True
    owner._telegram_dispatch_target = lambda: (asyncio.get_running_loop(), object())
    owner._dispatch_group_batch = dispatch
    owner._dispatch_report_to_telegram = lambda text: None
    monkeypatch.setattr(adapter, '_phone_is_paused', lambda: False)
    monkeypatch.setattr(adapter, '_load_policy', lambda: policy or authorized_policy())
    return owner


def test_authorized_notifications_buffer_without_phone_and_ignore_global_rate(monkeypatch):
    async def scenario():
        batches, ready = [], asyncio.Event()
        async def dispatch(batch):
            batches.append(batch)
            if len(batches) == 2:
                ready.set()
        owner = make_owner(monkeypatch, dispatch)
        owner._on_raw_event(event('a1'))
        owner._on_raw_event(event('a2', timestamp=2))
        owner._on_raw_event(event('b1', chat='Group B'))
        assert batches == []
        await asyncio.wait_for(ready.wait(), 1)
        owner._group_participation.close()
        assert sorted([e.body for e in b.events] for b in batches) == [['a1', 'a2'], ['b1']]
    asyncio.run(scenario())


@pytest.mark.parametrize('behavior', ['ignore', 'report'])
def test_group_buffer_does_not_override_phone_policy(monkeypatch, behavior):
    async def scenario():
        async def dispatch(batch):
            pytest.fail('unauthorized group dispatched')
        owner = make_owner(monkeypatch, dispatch, policy=authorized_policy(behavior))
        owner._on_raw_event(event('chatter'))
        assert owner._group_participation is None
    asyncio.run(scenario())


def test_unknown_notification_is_not_inferred_private_when_participation_enabled(monkeypatch):
    from plugins.phone_events import adapter
    from plugins.phone_use.policy import PolicyDecision
    observed = []
    policy = SimpleNamespace(evaluate_event=lambda **kw: (
        observed.append(kw['conversation_type']) or PolicyDecision(behavior='ignore')))
    async def scenario():
        owner = make_owner(monkeypatch, None, policy=policy)
        unknown = event('chatter')
        unknown.meta['conversation_type'] = 'unknown'
        owner._on_raw_event(unknown)
    asyncio.run(scenario())
    assert observed == ['unknown']


def test_unauthenticated_group_notification_cannot_enter_buffer(monkeypatch):
    async def scenario():
        owner = make_owner(monkeypatch, None)
        untrusted = event('chatter')
        untrusted.meta['_transport'] = 'logcat'
        owner._on_raw_event(untrusted)
        assert owner._group_participation is None
    asyncio.run(scenario())


def test_group_tool_scope_binds_target_and_counts_real_send_attempts():
    from plugins.phone_events.group_participation import (
        GroupBatch, bind_group_turn, guard_group_tool, record_group_reply,
    )
    batch = GroupBatch('A', (event('hello'),), (), False, {})
    with bind_group_turn(batch):
        assert guard_group_tool('phone_use', {'action': 'wechat_reply', 'chat': 'Group A'}) is None
        assert guard_group_tool('phone_use', {'action': 'wechat_reply', 'chat': 'Group B'})['action'] == 'block'
        assert guard_group_tool('phone_use', {'action': 'capture'})['action'] == 'block'
        assert guard_group_tool('web_search', {})['action'] == 'block'
        record_group_reply('phone_use', {'action': 'wechat_reply', 'chat': 'Group A'},
                           '{"ok": false}')
        assert batch.replied is False
        record_group_reply('phone_use', {'action': 'wechat_reply', 'chat': 'Group A'},
                           '{"meta": {"delivery_attempted": true, "delivery_status": "uncertain"}}')
        assert batch.replied is True
    assert guard_group_tool('phone_use', {'action': 'capture'}) is None


def test_participation_rejects_shared_report_topic_instead_of_merging_group_context(monkeypatch):
    from pathlib import Path
    from plugins.phone_events.adapter import PhoneEventAdapter
    source = Path.home() / '.hermes/hermes-agent'
    if not source.exists():
        pytest.skip('installed Hermes required')
    monkeypatch.syspath_prepend(str(source))
    from gateway.session import build_session_key
    owner = object.__new__(PhoneEventAdapter)
    owner._target_chat_id = 'report-chat'
    owner._target_thread_id = 'shared-topic'
    owner._target_user_id = 'owner'
    owner._target_user_name = 'phone'
    owner._target_chat_type = 'dm'
    owner._target_profile = None
    owner._group_settings = settings()
    with pytest.raises(ValueError, match='shared topic'):
        owner._validate_group_routing()


def test_silence_marker_survives_phone_report_formatting():
    from plugins.phone_events.adapter import _bind_report_origin, _prepend_phone_report_context
    from plugins.phone_use.policy import PolicyDecision
    with _bind_report_origin(event('chatter'), PolicyDecision(behavior='auto', instruction_source=True)):
        assert _prepend_phone_report_context('NO_REPLY', platform='telegram') is None


def test_group_tool_guard_rejects_malformed_targets_without_raising():
    from plugins.phone_events.group_participation import GroupBatch, bind_group_turn, guard_group_tool
    batch = GroupBatch('A', (event('hello'),), (), False, {})
    with bind_group_turn(batch):
        assert guard_group_tool('phone_use', {'action': 'wechat_reply', 'chat': 123})['action'] == 'block'


def test_continuous_chatter_reaches_maximum_batch_wait():
    async def scenario():
        batches = []
        async def dispatch(batch):
            batches.append(batch)
        scheduler = GroupParticipation(settings(), dispatch)
        for n in range(12):
            scheduler.observe('A', event(str(n), timestamp=n), None)
            await asyncio.sleep(.01)
        scheduler.close()
        assert batches
        assert len(batches[0].events) < 12
    asyncio.run(scenario())


def test_group_routing_checks_the_actual_secondary_profile_adapter(monkeypatch):
    from pathlib import Path
    from plugins.phone_events.adapter import PhoneEventAdapter
    source = Path.home() / '.hermes/hermes-agent'
    if not source.exists():
        pytest.skip('installed Hermes required')
    monkeypatch.syspath_prepend(str(source))
    from gateway.config import Platform
    owner = object.__new__(PhoneEventAdapter)
    owner._group_settings = settings()
    owner._target_thread_id = None
    owner._target_profile = 'phone-profile'
    owner.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(group_sessions_per_user=True),
        _gateway_loop=object(),
        adapters={Platform.TELEGRAM: SimpleNamespace(config=SimpleNamespace(extra={}))},
        _profile_adapters={'phone-profile': {Platform.TELEGRAM: SimpleNamespace(
            config=SimpleNamespace(extra={'group_sessions_per_user': False}))}},
    )
    with pytest.raises(ValueError, match='isolation'):
        owner._validate_group_routing()


def test_stopping_monitors_prevents_queued_observations_reopening_a_buffer(monkeypatch):
    async def scenario():
        from plugins.phone_events.adapter import PhoneEventAdapter
        stopped = asyncio.Event()
        async def dispatch(batch):
            pytest.fail('stopped observation dispatched')
        owner = make_owner(monkeypatch, dispatch)
        owner._group_admission_open = True
        owner._logcat_monitor = SimpleNamespace(stop=lambda: stopped.set())
        owner._socket_listener = None
        # This callback runs while suspend_monitors awaits its stopping thread.
        asyncio.get_running_loop().call_soon(owner._observe_group, event('late'), authorized_policy().evaluate_event(
            package='com.tencent.mm', event_type='notification', conversation_type='group'))
        await owner.suspend_monitors()
        assert owner._group_participation is None
        assert owner._group_admission_open is False
    asyncio.run(scenario())


@pytest.mark.parametrize('bodies, pattern', [
    (['@agent question', '@agent question\nBob: followup'], '@agent'),
    (['x' * 220 + ' authorized suffix'], 'authorized suffix'),
    (['OTP 123456'], '123456'),
])
def test_batch_policy_revalidation_uses_original_payload_not_display_delta(monkeypatch, bodies, pattern):
    import concurrent.futures
    import re
    from plugins.phone_events import adapter
    from plugins.phone_use.policy import PhonePolicy, EventRule
    policy = PhonePolicy(default_behavior='ignore', event_rules=[EventRule(
        package='com.tencent.mm', event_type='notification', conversation_type='group',
        body_regex=re.compile(pattern), behavior='auto', instruction_source=True,
        allowed_actions=frozenset({'wechat_reply'}), notes='User configured group instructions',
    )])
    async def scenario():
        sent, ready = [], asyncio.Event()
        owner = make_owner(monkeypatch, None, policy=policy)
        owner._dispatch_group_batch = lambda batch: adapter.PhoneEventAdapter._dispatch_group_batch(owner, batch)
        def capture(text, phone_event, decision):
            sent.append(text)
            done = concurrent.futures.Future()
            done.set_result(None)
            ready.set()
            return done
        owner._dispatch_to_gateway = capture
        for n, body in enumerate(bodies):
            owner._on_raw_event(event(body, timestamp=n + 1))
        await asyncio.wait_for(ready.wait(), 1)
        owner._group_participation.close()
        assert len(sent) == 1
        assert 'User configured group instructions' in sent[0]
        if pattern == '123456':
            assert '123456' not in sent[0]
        if len(bodies) > 1:
            assert sent[0].count('@agent question') == 1
    asyncio.run(scenario())
