# Automatic WeChat task reliability

Authenticated automatic phone tasks retain ownership of the phone from their
first phone tool call until the Hermes `on_session_end` hook finishes cleanup.
Other tasks, manual phone calls, and approved friend requests wait in the
existing device queue. The owner can keep reading, researching, and replying
without another task switching its screen between calls. This ownership is
within one gateway process; run one controller per device.

The adapter waits for the actual Hermes background task, not merely the return
from `handle_message`. Notifications in the same conversation are dispatched
individually so each retains its own authenticated policy and event identity.
This integration uses Hermes' `_session_tasks` map; verify it after upgrading
Hermes with the offline smoke check below.

## Durable reply receipts

The phone-events adapter derives a host-only identity from the device, package,
notification key, notification post time, title, and body before redaction.
The reply receipt additionally identifies the destination and exact reply text.
Replaying that event after a gateway restart does not depend on the new Hermes
turn ID. A later notification with a different post time is a new request, even
if its text is identical. An acknowledgement and a different final answer get
separate receipts.

Receipts are stored in `<HERMES_HOME>/phone/delivery/receipts.sqlite3` (normally
`~/.hermes/phone/delivery/receipts.sqlite3`). Only hashes and states are stored,
not chat titles or message bodies. The directory is created with mode 0700 and
the database and writer lock with mode 0600. SQLite commits precede the Send
tap; an OS lock is released on process death. If storage fails, sending stops.
Durable receipt locking currently targets macOS/Linux hosts (POSIX `flock`).

| State | Replay behavior |
| --- | --- |
| `prepared` | No Send attempt was recorded; normal navigation/recovery may retry. |
| `attempted` | Send may have reached WeChat; return uncertain without sending again. |
| `confirmed` | Return the prior confirmed status without sending again. |
| `uncertain` | Report uncertainty through Telegram; do not automatically resend. |

Confirmation still means the existing on-screen verification succeeded, not
that the recipient read the message. This is duplicate suppression for the
same event/destination/text, not exactly-once delivery: WeChat offers no atomic
transaction with the receipt database. A crash after the receipt commit but
before the tap deliberately leaves an uncertain result. Changing the reply
text produces a different receipt. Receipts survive turn cleanup and have no
automatic expiry; removing the database removes replay protection.

The journal does not persist incoming notifications or resume unfinished model
reasoning after restart. It protects sends when a task/event is retried. Manual
approved replies and standalone MCP callers retain their existing behavior.
The shared `wechat.reply` function accepts an optional delivery callback and
does not require Hermes or the journal when used by phone-mcp-server.

## Offline verification

Run `PYTHONPATH=. python -m pytest -q tests` for existing behavior plus process
death, persisted receipt, task serialization, and background-dispatch tests.

With Hermes installed, run from this checkout (substitute your Hermes path):

```sh
PYTHONPATH="$PWD:/path/to/hermes-agent" /path/to/hermes-agent/venv/bin/python scripts/check_gateway_lifecycle.py
```

This exercises the real gateway base class using a temporary Hermes home, an
in-memory message transport, and a noop phone. It sends no Telegram or WeChat
messages and does not contact ADB or a model provider.
