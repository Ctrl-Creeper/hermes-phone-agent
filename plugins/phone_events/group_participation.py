"""Host-only notification batching and turn scope for WeChat group participation.

All scheduler methods run on the gateway loop. Receiving a notification never
reads the phone or calls a model. Started batches run to completion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from .event_filter import PhoneEvent
from .redact import redact_sensitive, truncate_notification_body

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParticipationSettings:
    enabled: bool = False
    mention_names: tuple[str, ...] = ()
    quiet_seconds: float = 2.0
    max_wait_seconds: float = 8.0
    min_interval_seconds: float = 30.0
    max_messages: int = 64

    @classmethod
    def from_config(cls, raw: Any) -> ParticipationSettings:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError('group_participation must be a mapping')
        if raw.get('enabled', False) is not True:
            return cls()
        names = raw.get('mention_names', [])
        if (not isinstance(names, list) or not names
                or any(not isinstance(n, str) or not n.strip().lstrip('@') for n in names)):
            raise ValueError('group_participation.mention_names must name your WeChat account')
        values = {}
        for key in ('quiet_seconds', 'max_wait_seconds', 'min_interval_seconds'):
            value = raw.get(key, getattr(cls(), key))
            if isinstance(value, bool):
                raise ValueError(f'group_participation.{key} must be a finite number')
            try:
                value = float(value)
            except (ValueError, TypeError):
                raise ValueError(f'group_participation.{key} must be a finite number') from None
            if not math.isfinite(value) or value < 0 or (key != 'min_interval_seconds' and value == 0):
                raise ValueError(f'group_participation.{key} must be positive (interval may be zero)')
            values[key] = value
        count = raw.get('max_messages', cls().max_messages)
        if type(count) is not int or not 1 <= count <= 256:
            raise ValueError('group_participation.max_messages must be between 1 and 256')
        if values['max_wait_seconds'] < values['quiet_seconds']:
            raise ValueError('group_participation.max_wait_seconds must be at least quiet_seconds')
        return cls(enabled=True, mention_names=tuple(n.strip().lstrip('@') for n in names),
                   max_messages=count, **values)

    def is_invited(self, text: str) -> bool:
        return any(re.search(r'@' + re.escape(name) + r'(?![\w])', text, re.IGNORECASE)
                   for name in self.mention_names)


@dataclass
class GroupBatch:
    lane: str
    events: tuple[PhoneEvent, ...]
    decisions: tuple[Any, ...]
    invited: bool
    metrics: dict[str, Any]
    replied: bool = False
    # Kept only for host policy revalidation, never formatted into model input.
    policy_inputs: tuple[dict[str, str], ...] = field(default=(), repr=False)

    @property
    def destination(self) -> str:
        return str(self.events[-1].meta.get('conversation_title') or self.events[-1].title).strip()


@dataclass(frozen=True)
class _Observation:
    event: PhoneEvent
    decision: Any
    invited: bool
    policy_input: dict[str, str] = field(repr=False)


@dataclass
class _Lane:
    title: str
    pending: list[_Observation] = field(default_factory=list)
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    previous_lines: list[str] = field(default_factory=list)
    arrivals: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    replies: deque[float] = field(default_factory=lambda: deque(maxlen=128))
    first_at: float = 0
    last_at: float = 0
    last_completed: float = float('-inf')
    omitted: int = 0
    timer: asyncio.TimerHandle | None = None
    running: asyncio.Task | None = None


class GroupParticipation:
    def __init__(self, settings: ParticipationSettings,
                 dispatch: Callable[[GroupBatch], Awaitable[None]], *, redact_otp: bool = True) -> None:
        self._redact_otp = redact_otp
        self.settings = settings
        self._dispatch = dispatch
        self._lanes: dict[str, _Lane] = {}
        self._closed = False

    def observe(self, lane: str, event: PhoneEvent, decision: Any,
                policy_input: dict[str, str] | None = None) -> bool:
        if self._closed:
            return False
        loop = asyncio.get_running_loop()
        title = str(event.meta.get('conversation_title') or event.title).strip()
        state = self._lanes.get(lane)
        if state is None:
            if len(self._lanes) >= 128:
                # Evict only idle lanes, never running or pending work.
                idle = next((k for k, s in self._lanes.items()
                             if not s.pending and s.running is None), None)
                if idle is None:
                    logger.warning('Group participation capacity reached; observation not admitted')
                    return False
                self._lanes.pop(idle)
            state = self._lanes[lane] = _Lane(title=title)
        if state.title != title:
            logger.warning('Conflicting conversation titles for one notification identity; not merging')
            return False
        policy_input = dict(policy_input) if policy_input is not None else {
            'package': event.package, 'event_type': event.event_type,
            'title': event.title, 'body': event.body, 'conversation_type': 'group',
        }
        identity = hashlib.sha256(json.dumps([
            event.meta.get('notification_key'), event.timestamp,
            policy_input['title'], policy_input['body'],
        ], ensure_ascii=False).encode()).hexdigest()
        if identity in state.seen:
            return True
        state.seen[identity] = None
        if len(state.seen) > 512:
            state.seen.popitem(last=False)

        # Android can replace one notification with overlapping message lines.
        lines = policy_input['body'].splitlines()
        delta = lines
        if len(lines) > 1:
            overlap = 0
            for size in range(1, min(len(lines), len(state.previous_lines)) + 1):
                if state.previous_lines[-size:] == lines[:size]:
                    overlap = size
            delta = lines[overlap:]
        state.previous_lines = lines
        if not delta:
            return True
        new_body = '\n'.join(delta)
        invited = self.settings.is_invited(new_body)
        observed = replace(event, body=truncate_notification_body(
            redact_sensitive(new_body, enable_otp=self._redact_otp)), meta=dict(event.meta))
        now = loop.time()
        state.arrivals.append(now)
        if not state.pending:
            state.first_at = now
        state.last_at = now
        state.pending.append(_Observation(observed, decision, invited, policy_input))
        if len(state.pending) > self.settings.max_messages:
            index = next((i for i, observation in enumerate(state.pending) if not observation.invited), 0)
            state.pending.pop(index)
            state.omitted += 1
        self._schedule(lane, state)
        return True

    def _schedule(self, lane: str, state: _Lane) -> None:
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if self._closed or state.running is not None or not state.pending:
            return
        loop = asyncio.get_running_loop()
        invited = any(observation.invited for observation in state.pending)
        if invited:
            due = min(state.first_at + .25, state.last_at + self.settings.quiet_seconds)
        else:
            due = max(min(state.first_at + self.settings.max_wait_seconds,
                          state.last_at + self.settings.quiet_seconds),
                      state.last_completed + self.settings.min_interval_seconds)
        state.timer = loop.call_later(max(0, due - loop.time()), self._start, lane)

    def _start(self, lane: str) -> None:
        state = self._lanes[lane]
        state.timer = None
        if self._closed or state.running is not None or not state.pending:
            return
        now = asyncio.get_running_loop().time()
        for times in (state.arrivals, state.replies):
            while times and times[0] < now - 300:
                times.popleft()
        pending, state.pending = state.pending, []
        batch = GroupBatch(
            lane=lane, events=tuple(o.event for o in pending),
            decisions=tuple(o.decision for o in pending),
            invited=any(o.invited for o in pending),
            policy_inputs=tuple(o.policy_input for o in pending),
            metrics={
                'observed_notifications': len(pending),
                'notifications_last_minute': sum(t >= now - 60 for t in state.arrivals),
                'seconds_since_last_notification': round(now - state.last_at, 2),
                'replies_last_five_minutes': len(state.replies),
                'omitted_notifications': state.omitted,
            },
        )
        state.omitted = 0
        state.running = asyncio.create_task(self._run(lane, state, batch))

    async def _run(self, lane: str, state: _Lane, batch: GroupBatch) -> None:
        try:
            await self._dispatch(batch)
        except Exception:
            # A failed batch must not strand another group or cause a retry loop.
            logger.exception('Group participation batch failed')
        finally:
            now = asyncio.get_running_loop().time()
            state.last_completed = now
            if batch.replied:
                state.replies.append(now)
            state.running = None
            self._schedule(lane, state)

    def close(self) -> None:
        """Discard unsent observations and timers; never cancel a started turn."""
        self._closed = True
        for state in self._lanes.values():
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
            state.pending.clear()


_group_turn: ContextVar[GroupBatch | None] = ContextVar('phone_group_turn', default=None)


@contextmanager
def bind_group_turn(batch: GroupBatch | None):
    token = _group_turn.set(batch)
    try:
        yield
    finally:
        _group_turn.reset(token)


def guard_group_tool(tool_name: str, args: dict | None = None, **kwargs):
    batch = _group_turn.get()
    if batch is None:
        return None
    args = args if isinstance(args, dict) else {}
    if tool_name in {'web_search', 'web_extract'}:
        if batch.invited:
            return None
    elif tool_name == 'phone_use':
        action = args.get('action')
        allowed = action == 'wechat_reply' or (batch.invited and action == 'wechat_collect_context')
        target = args.get('chat')
        if allowed and isinstance(target, str) and target.strip() == batch.destination:
            return None
    return {'action': 'block', 'message': (
        'This group turn may reply only to its bound conversation. '
        'Only explicitly invited turns may read that conversation or research; '
        'ordinary participation must use the observed notification context or remain silent.'
    )}


def record_group_reply(tool_name: str, args: dict | None = None, result: Any = None, **kwargs):
    batch = _group_turn.get()
    if batch is None or tool_name != 'phone_use' or not isinstance(args, dict):
        return
    target = args.get('chat')
    if (args.get('action') != 'wechat_reply' or not isinstance(target, str)
            or target.strip() != batch.destination):
        return
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return
    meta = result.get('meta') if isinstance(result, dict) else None
    if isinstance(meta, dict) and meta.get('delivery_attempted') and not meta.get('duplicate_suppressed'):
        batch.replied = True
