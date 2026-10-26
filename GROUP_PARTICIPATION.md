# Experimental notification-only WeChat group participation

This Hermes-only feature buffers authorized group notifications without
operating the phone or calling a model. A normal agent turn evaluates a batch
and either returns `NO_REPLY` or composes and sends a response. There is no
separate classifier and no cancellation/regeneration when new messages arrive.
It is disabled by default and has not been validated against real WeChat mute,
foreground and cumulative-notification behavior.

## Opt in

In `~/.hermes/config.yaml` (preserve your other platform settings):

```yaml
group_sessions_per_user: true
platforms:
  phone_events:
    enabled: true
    extra:
      telegram_chat_id: "YOUR_PRIVATE_TELEGRAM_CHAT"
      group_participation:
        enabled: true
        mention_names: ["YourBot"]  # Your actual WeChat @mention name(s), without @
        quiet_seconds: 2
        max_wait_seconds: 8
        min_interval_seconds: 30
        max_messages: 64
```

Use a Telegram destination without `telegram_thread_id`. Shared topics and
`group_sessions_per_user: false` are rejected because the current Hermes source
mapping would combine multiple phone conversations into one model history.
Per-profile Telegram destinations must also preserve per-user session isolation.

The phone policy remains authoritative. In `~/.hermes/phone-policy.yaml`,
**replace** the existing catch-all WeChat group `ignore` rule with this rule;
do not simply append it below an existing higher-priority group ignore rule:

```yaml
event_rules:
  # Preserve your other rules, including higher-priority @mention rules.
  - match:
      package: "com.tencent.mm"
      event: notification
      conversation_type: group
    behavior: auto
    instruction_source: true
    allowed_actions: [wechat_reply, wechat_collect_context]
    notes: "Allow notification-only participation in this group conversation."
    priority: 39
```

Retain specific ignore/report rules with higher priority for any conversations
or notification categories you do not want treated as an automatic inbox.
Global restrictions, app action restrictions and authentication still apply.
An ignore/report decision never becomes auto merely because batching is enabled.
Restart the gateway after changing `config.yaml`; phone policy rules retain their
existing hot-reload behavior. Buffered tasks are rechecked against the original
authenticated payload before dispatch; only sanitized, truncated deltas reach
the model. This PR does not change your running installation.

## Timing and four signals

- **Invitation:** only configured @names are a local fast-path signal. They
  bypass the ordinary evaluation interval and wait at most 250 ms to combine
  consecutive notifications. Other @names do not trigger that fast path. The
  model can interpret implicit invitations from the batch and its own history.
- **Discussion pace:** ordinary batches wait for `quiet_seconds` of silence,
  bounded by `max_wait_seconds`. Observed notification counts and the last
  arrival gap accompany the batch. These are notification statistics, not a
  complete message/speaker census.
- **Saturation:** ordinary evaluations are spaced by `min_interval_seconds`
  after the previous turn finishes, including turns that chose silence. Recent
  reply-turn counts also accompany the batch. Attempted but unconfirmed sends
  consume a reply turn; suppressed duplicate sends do not.
- **Value:** the same agent call that would compose a response decides whether
  there is anything useful or socially appropriate to add. Ordinary participation
  may only reply to its bound destination; it cannot open the phone or search
  just to decide whether to speak. Explicitly invited turns may collect that
  chat's context and perform web research under the existing task policy.

Each group has its own pending buffer, timer and in-flight batch. While one
batch runs, subsequent notifications enter the next batch. The running batch's
observations and target stay fixed. An idle group never causes periodic model
calls. Notifications do not consume the shared per-second event quota; package
allowlisting still applies. Per-group replay deduplication uses notification
identity, post time and content, retaining identical later single-line messages
as distinct observations. Overlapping multiline notification updates contribute
only new lines. A genuinely repeated multiline message can be indistinguishable
from a cumulative update and may be suppressed by that overlap heuristic. Buffers retain at most `max_messages` notification fragments, favoring
invitations over older ordinary chatter, and disclose omitted fragment counts
to the model. If all fragments are invitations, the oldest fragment is omitted.

The existing global phone queue is unchanged: model work and timers are
independent per group, but an automatic task that has acquired the phone keeps
it until turn cleanup. This avoids switching screens between dependent actions;
notification batching alone does not remove that device-level wait.

## WeChat mute and foreground limitations

The helper does **not** expose WeChat's per-group mute switch. The intended UX
is: keep mention-only groups muted, and unmute groups where participation is
welcome. This works only insofar as WeChat emits notifications accordingly:

- A muted group with only @mention notifications still only wakes on those
  received mentions. The feature does not poll its chat history.
- If WeChat also posts ordinary system notifications for a muted group, the
  gateway cannot identify that group as muted and those notifications can enter
  participation. Use a higher-priority explicit policy rule for a hard exclusion.
- Being unmuted does not guarantee notification delivery, particularly when the
  relevant chat is open. Missing notifications are not recovered by this feature.
- Only authenticated helper events explicitly classified `group`, with a usable
  conversation title, enter the buffer. Unknown and summary notifications do not.
  With participation enabled the legacy `wechat_assume_unmentioned_private`
  heuristic is disabled; unknown non-@ notifications are no longer assumed DMs.
  Explicitly classified DMs and existing explicit-mention policy paths retain
  their behavior.

No shared memory settings are changed. If different chats must not see the
owner's global `MEMORY.md`/`USER.md`, configure a fixed phone-task profile with
appropriate memory settings before enabling participation.

Pending observations and timers are in memory and are discarded on disconnect
or explicit global phone pause. Closing a buffer does not cancel a running
batch. Existing explicit owner pause controls retain their cancellation behavior.
Durable send receipts use the composed batch identity plus destination and text;
they suppress replay of the same batch/send, not arbitrary regroupings of old
notifications after restart.

## Offline verification

```sh
PYTHONPATH=. python3 -m pytest -q tests
PYTHONPATH="$PWD:/path/to/hermes-agent" /path/to/hermes-agent/venv/bin/python scripts/check_group_participation.py
PYTHONPATH="$PWD:/path/to/hermes-agent" /path/to/hermes-agent/venv/bin/python scripts/check_gateway_lifecycle.py
```

The integration checks use real Hermes background task/session machinery, a
temporary Hermes home, in-memory transport and a noop phone. They contact no
model, Telegram or Android device. Live validation should separately check
muted @mentions, unmuted bursts, foreground delivery, notification replacement,
private-message classification and exact destination matching.
