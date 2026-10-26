# WeChat notification-only group participation

Approved scope: buffer received WeChat group notifications without reading or
operating the phone; keep mentions responsive, batch ordinary discussion, and
let one normal agent turn choose silence or formulate a reply. Started turns
run to completion. New arrivals wait in the next per-conversation batch.

The feature is opt-in through platforms.phone_events.extra.group_participation
and still requires an explicit auto/instruction_source phone policy. Existing
ignore/report rules remain authoritative. WeChat mute state is not available:
muted groups remain mention-only insofar as WeChat emits only mention
notifications. There is no polling of muted groups or the foreground UI.

Use asynchronous per-conversation timers and at most one in-flight batch per
conversation. Local admission uses mentions, quiet/burst timing and a minimum
interval between ordinary evaluations; recent notification and confirmed/attempted
reply counts inform the same agent call's value judgment. No periodic model
polling, separate classifier, cancellation or automatic regeneration.

Preserve authenticated policy, delivery receipts and device ownership. Bind
composite phone actions to the batch's destination. Unknown/group summaries do
not qualify for autonomous participation, and enabling this mode disables the
legacy inference that unknown unmentioned notifications are private messages.
Reject shared Telegram-topic and non-isolated routing configurations rather
than allowing the report destination to collapse phone conversation identity.

The phone event host is the only implementation surface. No helper APK, shared
phone control APIs, Hermes core or live user configuration is changed. Existing
manual controls and explicit global pause behavior remain available.

## Approved private-conversation extension

Private conversations share the notification scheduler, per-conversation pending
and running batches, redaction, policy revalidation, destination guards and
receipts. Enable private_participation independently; defaults are a 0.75-second
quiet window, 3-second maximum wait and no group evaluation cooldown. Every
explicitly classified private message has high invitation strength, while the
same agent turn can choose NO_REPLY for natural conversation endings. Unknown
notifications are not inferred to be private when either mode is enabled.
Conversation type namespaces buffered session keys and delivery identities.
