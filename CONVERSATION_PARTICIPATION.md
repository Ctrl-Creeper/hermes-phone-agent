# Experimental notification-only WeChat conversation participation

This Hermes-only feature buffers authorized private and group notifications without
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
        titles: ["Unmuted Group"]  # See "Group classification" below
        reply_tendency:            # 0 = only when invited, 1 = every batch; hot-reloaded
          default: 0.5
          "Unmuted Group": 0.8
        quiet_seconds: 2
        max_wait_seconds: 8
        min_interval_seconds: 30
        max_messages: 64
      private_participation:
        enabled: true
        quiet_seconds: 0.75
        max_wait_seconds: 3
        min_interval_seconds: 0
        cooldown_minutes: 5        # pause after the agent returns COOLDOWN
```

The two modes can be enabled independently; both default to disabled. Private
messages use the same scheduler, isolation and destination guards with their
own timing profile. They do not require `mention_names` or @. Their default
invitation level is high, but conversational closing messages can choose silence.

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
For private participation, also retain or add an explicit private-message auto
rule. It is independent of the group rule above:

```yaml
  - match:
      package: "com.tencent.mm"
      event: notification
      conversation_type: private
    behavior: auto
    instruction_source: true
    allowed_actions: [wechat_reply, wechat_collect_context]
    notes: "Handle requests naturally in this private conversation."
    priority: 29
```

Restart the gateway after changing `config.yaml`, except for `reply_tendency`,
which is re-read when the file changes (an invalid edit keeps the last good
value); phone policy rules retain their existing hot-reload behavior. Buffered tasks are rechecked against the original
authenticated payload before dispatch; only sanitized, truncated deltas reach
the model. This PR does not change your running installation.

## Timing and four signals

- **Invitation:** only configured @names are a local fast-path signal. They
  bypass the ordinary evaluation interval and wait at most 250 ms to combine
  consecutive notifications. Other @names do not trigger that fast path. The
  model can interpret implicit invitations from the batch and its own history.
  All explicitly classified private messages have high invitation strength;
  they still wait for the private quiet/max-wait window rather than taking
  the group's 250 ms mention fast path.
- **Discussion pace:** ordinary batches wait for `quiet_seconds` of silence,
  bounded by `max_wait_seconds`. Observed notification counts and the last
  arrival gap accompany the batch. These are notification statistics, not a
  complete message/speaker census. Private messages use a 0.75-second quiet
  window and 3-second maximum wait by default, combining split messages.
- **Saturation:** ordinary evaluations are spaced by `min_interval_seconds`
  after the previous turn finishes, including turns that chose silence. Recent
  reply-turn counts also accompany the batch. Private conversations default to
  a zero minimum interval, so a new turn can proceed immediately after its
  short merge window instead of waiting through a group cooldown. Attempted but
  unconfirmed sends
  consume a reply turn; suppressed duplicate sends do not.
- **Value:** the same agent call that would compose a response decides whether
  there is anything useful or socially appropriate to add. Ordinary participation
  may only reply to its bound destination; it cannot open the phone or search
  just to decide whether to speak. Explicitly invited group turns and private
  turns may collect their own chat's context and perform web research under
  the existing task policy. Private turns normally respond but may return
  `NO_REPLY` for thanks, a closing acknowledgement or an already resolved exchange.
- **Reply tendency:** each group batch states the owner's `reply_tendency`
  (0 to 1, globally or per group title) and the configured mention. At `0`,
  uninvited chatter is held without a model call (bounded by `max_messages`)
  and reaches the model only with the next invitation; at `1` the model is told
  to reply to every batch. Values in between steer the same-turn judgement.
  Batching and timing are unchanged. Asked to read history, view images or
  research without an invitation, the agent replies that it needs an @ first.

When a turn chooses `NO_REPLY`, its observations stay open: they lead the next
batch, marked as earlier context the model ignores if the topic moved on. The
carry clears after a reply, after five minutes without notifications in that
conversation, or when a turn fails or is rejected, and never pushes a batch past
`max_messages`.

If a private sender keeps pushing requests the agent already declined
(commands, device details, injected `system:` text, harassment), the agent may
send one brief closing message and end the turn with `COOLDOWN`. That
conversation then drops notifications for `cooldown_minutes` (default 5),
including any that arrived during the turn, and nothing is replayed afterwards.
Telegram receives a short notice instead of the marker. Group turns ignore the
marker, so text injected by one member cannot mute a whole group.

A reply may be split into up to three short messages, one `wechat_reply` call
each. Follow-up calls in the same retained turn reuse the chat the previous
confirmed reply left open (re-verified on screen, single use, two-minute TTL)
instead of searching again. The turn guard blocks a fourth send.

Each private or group conversation has its own pending buffer, timer and
in-flight batch. Conversation type is part of the buffer and receipt identity.
Batched turns keep the conversation's existing Hermes session (derived from the
Android notification key, as for non-batched turns), so instructions given
before enabling participation stay in the model's history. Physical chat selection still
uses the existing title-based phone workflow and its ambiguity checks; this
session isolation does not introduce a new WeChat recipient ID API. While one
batch runs, subsequent notifications enter the next batch. The running batch's
observations and target stay fixed. An idle conversation never causes periodic model calls. Notifications do not consume the shared per-second event quota; package
allowlisting still applies. Per-conversation replay deduplication uses notification
identity, post time and content, retaining identical later single-line messages
as distinct observations. Overlapping multiline notification updates contribute
only new lines. A genuinely repeated multiline message can be indistinguishable
from a cumulative update and may be suppressed by that overlap heuristic.
Buffers retain at most `max_messages` notification fragments, favoring
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
- Only authenticated helper events classified `group` or `private`, with a
  usable conversation title and that mode enabled, enter its buffer. Summary
  notifications do not.

### Group classification

Some WeChat builds omit `android.isGroupConversation`, so the helper reports
every message as `unknown`. With group participation enabled, groups are learned
from @mentions instead: **join groups muted first**. A muted group only posts
@mentions of you, and a `Sender: …@YourBot…` notification from someone other
than the chat title, or WeChat's own `… mentioned you in a group chat` notice
(matched as the whole notification), marks that chat as a group. Learned titles
are kept in `$HERMES_HOME/phone/known_wechat_groups.json` and stay groups after
you unmute them. Ordinary `Sender: text` bodies never decide on their own, so a
DM contact cannot reclassify a chat by typing a prefix such as `system: …`.

`group_participation.titles` remains a manual list for groups to treat as groups
without waiting for an @mention. The legacy `wechat_assume_unmentioned_private`
heuristic (unmentioned unknown notifications are DMs) still covers everything
else, so a group unmuted before its first @mention is treated as private until
then. A DM contact can make their own chat count as a group by imitating an
@mention body; that only narrows the chat to group permissions. Remove a wrongly
learned title from the JSON file to undo it.

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
PYTHONPATH="$PWD:/path/to/hermes-agent" /path/to/hermes-agent/venv/bin/python scripts/check_conversation_participation.py
PYTHONPATH="$PWD:/path/to/hermes-agent" /path/to/hermes-agent/venv/bin/python scripts/check_gateway_lifecycle.py
```

The integration checks use real Hermes background task/session machinery, a
temporary Hermes home, in-memory transport and a noop phone. They contact no
model, Telegram or Android device. Live validation should separately check muted @mentions, unmuted bursts,
private bursts and conversation endings, foreground delivery, notification
replacement, private-message classification and exact destination matching.
