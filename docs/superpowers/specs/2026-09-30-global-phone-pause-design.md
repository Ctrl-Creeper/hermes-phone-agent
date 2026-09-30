# Global Hermes phone pause from the Telegram owner chat

## Goal and boundaries

The authorized owner can say an unambiguous phrase such as “暂停手机操控” or
“恢复手机操控” in the configured Telegram main chat. A pause takes effect before
the message reaches a possibly busy model turn. It halts Hermes-owned phone
automation globally while leaving Telegram and non-phone chat available. MCP,
Neko, Android itself, and independently running ADB clients are outside scope.
No existing chat memory is deleted. This is a control switch, not an LLM tool.

An issued WeChat send/tap cannot be undone. “Immediate” means synchronously
closing the admission gate, signalling all currently running Hermes phone
turns, and preventing their next device action. A subprocess or an already
entered Android primitive may need to return before safe cleanup; the user
receives an honest status rather than a false claim of physical preemption.

## Entry, identity, and state

Compare normalized **whole messages** against a small, documented allowlist
of Chinese pause/resume/status phrases and explicit `/phone pause`,
`/phone resume`, `/phone status` forms. No substring match, quoting, forwarded
content, phone notifications, or model interpretation can change state.
Authenticate using the Telegram source identity and configured owner user and
main chat/thread; compare IDs, not names or the displayed message body. A
non-owner gets no privileged action. The command has a direct acknowledgement
with the effective state and any still-unwinding operation. Repeating pause or
resume is idempotent.

Persist the paused flag and a monotonically changing generation in the
profile-aware Hermes home as an atomic, owner-only state file. An absent file
defaults to running; malformed/unreadable state fails closed to paused, with
an actionable diagnostic. Do not add a non-secret `.env` switch. The gate is
global to this Hermes profile, not the chat's agent session. A restart reads
the gate **before** connecting the phone event adapter. On resume, it does not
replay paused-period notifications, previously queued tasks, or old receipts.

## Wiring

Two implementation options were considered. A model-callable `phone_pause`
tool is too late when a turn is busy, and a separate Telegram bot would
duplicate authorization and command routing. Use the existing Telegram gateway
and its `pre_gateway_dispatch` hook, which runs before busy-session handling.
It must not change past conversation messages, system prompts, or model tool
schemas. The Hermes phone plugin owns phrase recognition, persisted state,
and phone cancellation. This hook runs before Hermes's own authentication, so
the plugin must check configured owner IDs and gateway authorization explicitly.
Unrelated messages follow the existing route unchanged.

At the `phone_use` boundary, check the pause generation at admission, after
approval waits, after acquiring the device queue, and before each physical
backend operation in composite workflows. In-flight backend primitives use
bounded timeouts and cooperative cancellation where available. The pause
transition first closes admission and revokes queued phone turns/workflow
scopes, then uses Hermes's existing session interruption/generation invalidation
for each active phone-origin turn. It also interrupts any owner-chat turn
currently operating the phone. Release phone queue ownership only when the
worker has unwound, not by pretending a still-running worker finished.

The phone event adapter disconnects its helper socket/logcat monitors and
removes forwarding while paused. Any racing callback checks the same gate
before friend-approval, report, or agent dispatch; queued phone events are
dropped. The pause acknowledgment itself travels over Telegram, which stays
connected. A resumed adapter establishes fresh authentication without
processing old event backlog. Prefer adapter lifecycle operations on the
gateway loop, and surface disconnect/reconnect failure in the direct status
reply; an unsuccessful reconnect does **not** open the action gate.

## Cleanup and races

On cancellation, preserve durable send receipts as attempted/uncertain; never
retry a possibly sent message. Abort pending approvals and clear turn-local
auto-task policy, workflow grants, queued messages, and reply caches. Once the
current primitive yields, perform at most one best-effort HOME cleanup under
the device lock and release the owner in a `finally` path. A failure to reach
HOME is reported rather than silently labelled clean. The pause generation
prevents a stale worker from continuing after resume: old work is cancelled,
not resumed. Cleanup HOME is the only permitted device action while paused.
It cannot roll back an already completed send or external side effect.

## Validation and delivery

Use isolated tests, not real WeChat messages. Exercise owner/non-owner and
forwarded/quoted text, whole-message phrases, busy owner and phone-origin
turns, queued work, approval waits, pause versus send races, restart-persistent
state, failed reconnect, repeated commands, and no replay after resume.
Verify the actual gateway dispatch path with an isolated Hermes home and
plugins installed; check the native phone queue and helper listener lifecycle.
Ensure ordinary Telegram conversation remains usable and no prompt-cache
prefix or toolset is changed. Develop in an isolated checkout and submit a
hermes-phone-agent PR; the existing hook avoids a Hermes core change. Deploy
only after reviewed integration tests. No shared
backend/helper change is expected, so MCP/Neko need no synchronization unless
implementation reveals otherwise.
