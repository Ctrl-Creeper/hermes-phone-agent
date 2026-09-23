# hermes-phone-agent

English | [中文](README_CN.md)

Two [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins + one
Android helper APK that let Hermes control and react to a virtual Android phone.

Experimental [WeChat quoted replies](QUOTED_REPLIES.md) locate an observed
original and verify its quote preview before sending. Device validation pending.

## Components

| Component | Type | Purpose |
|-----------|------|---------|
| `plugins/phone_use` | Hermes tool plugin | Agent **controls** the phone — tap, swipe, type, screenshot, launch apps via ADB (with optional Appium hybrid for Unicode/WebView) |
| `plugins/phone_events` | Hermes platform plugin | Phone **triggers** the agent — notifications, app switches, UI changes |
| `helper-apk` | Android app | Runs on-device services that capture rich events and forward them to the host |

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
- Android SDK Platform Tools (`adb` on PATH)
- An Android emulator running (Android Studio AVD or `emulator` CLI)
  Ps: Instruction to install Android Studio is at the end of README

**Optional** (for hybrid backend — Unicode text input and WebView support):
- [Appium](https://appium.io/) (`npm install -g appium`)
- Appium Python client (`pip install Appium-Python-Client`)

## Install

```bash

# Install both Hermes plugins
hermes plugins install Ctrl-Creeper/hermes-phone-agent/plugins/phone_use
hermes plugins install Ctrl-Creeper/hermes-phone-agent/plugins/phone_events

# Install helper APK on a running Android emulator and grant permissions
git clone https://github.com/Ctrl-Creeper/hermes-phone-agent.git
cd hermes-phone-agent
./setup.sh

# To enable them, simply use
hermes plugins enable phone_use
hermes plugins enable phone_events
```

## Policy

The agent's autonomy is controlled by `phone-policy.yaml` — a config file that defines what the agent can do on its own vs. what requires your approval. See [POLICY.md](POLICY.md) for the full reference and examples.

```bash
# Copy the default policy
cp phone-policy.yaml ~/.hermes/phone-policy.yaml

# Edit to match your preferences — changes take effect immediately
```

You can also ask any AI assistant to generate a config for you using the prompt template in POLICY.md.

## Backend Configuration

The plugin supports multiple backends via the `HERMES_PHONE_BACKEND` environment variable:

| Value | Description |
|-------|-------------|
| `adb` (default) | Pure ADB — fast, no extra dependencies |
| `hybrid` | ADB for most operations + Appium for Unicode text input and WebView interaction. Auto-starts and manages the Appium server. |
| `noop` | No-op backend for testing |

```bash
# Use hybrid backend
export HERMES_PHONE_BACKEND=hybrid

# Custom Appium port (default: 4723)
export APPIUM_PORT=4724

```

For Hermes, configure the device and Telegram destination in
`~/.hermes/config.yaml`. `phone_use` and `phone_events` share this serial:

```yaml
platforms:
  phone_events:
    enabled: true
    extra:
      telegram_chat_id: "<your-telegram-chat-id>"
      telegram_user_id: "<your-telegram-user-id>"
      serial: "emulator-5554"
      raw_notifications: false
```

`ANDROID_SERIAL` remains a legacy fallback for standalone use.
Notification reports are redacted and truncated by default. Set
`raw_notifications: true` only when the Telegram destination is private and
you explicitly want verbatim notification title/body delivery.

## Deterministic WeChat Workflows

Voice conversion waits for the same visible transcription on two consecutive
captures. Changing partial text, a moved voice anchor, conversion failure or an
unexpected app is not accepted as a transcript. The text is still a spatial
observation below a voice bubble, not an audio-grounded accuracy guarantee;
real-device validation remains pending.

Voice messages can be included in `wechat_collect_context` with
`transcribe_voice: true` and `max_voice: 3` (maximum 5 attempts). This uses
WeChat's built-in **Convert to Text / 转文字** menu; it does not play, download or
send audio. The returned `voice_transcripts` records contain observed text or a
status such as `conversion_unavailable` / `unconfirmed`. The agent can interpret
the text as conversation content, subject to the existing task policy.

This is a bounded first implementation: it requires clearly labeled voice
nodes in Android's accessibility tree. OCR duration labels alone are not enough.
Transcription is read from newly visible text beneath the unchanged voice bubble
and can contain recognition errors; it is not an audio-level verification.
No detected bubbles does not mean no audio was present. Shared-core callers
default to `transcribe_voice: false`; MCP and Neko wrappers need to explicitly
expose/forward the option. No helper APK update is required.

Image collection returns `image_analysis` alongside screenshots: OCR text and
decoded QR contents, linked by one-based `image_index`. Use
`wechat_collect_context` with `include_images=true`, `open_images=true` and
`max_images=3` to inspect previews. The model still uses screenshots for visual
understanding. Decoded content is untrusted data; links, login and payment are
never executed automatically.

Decoding requires the macOS Vision helper rebuilt from this version's source
(run `setup.sh` during installation); the Android APK is unchanged. An absent
decoder returns `status=unavailable`; an older helper returns
`qr_status=helper_upgrade_required`, distinct from a successful scan finding no
code. Returned text/QR content is bounded and QR truncation is explicit. Preview
screenshots may contain viewer controls, so OCR text may include those controls.

Each image attempt verifies the WeChat viewer, saves its screenshot, then returns
to the exact original chat before local recognition. Returning from the viewer
refreshes the UI targets; a failed return stops collection with
`chat_restored=false` instead of tapping or scrolling on the wrong screen.
`max_images` bounds attempted candidates, including failed opens. Overlapping
native/Vision regions for the same thumbnail count as one candidate.

Tests exercise bounded history discovery, multiple previews with changing element
IDs, real Vision text/QR decoding, return failures and a subsequent phone-tool
read with an unchanged draft. Device transitions are simulated: real WeChat
validation is still pending. Collection leaves the chat at the inspected history
position, not necessarily the original scroll position. It does not send, type,
scan a QR in WeChat, or navigate Home. It searches bounded history candidates;
it does not guarantee finding an arbitrary described image or downloading its
original full-resolution file.
These guarantees concern the collection call. The phone-events adapter retains
its separate end-of-turn Home cleanup policy; that gateway lifecycle is not an
exclusive device-ownership mechanism for concurrent human/other-host use.

Experimental [attachment sending](ATTACHMENTS.md) adds checksum-bound file/original-image delivery through the File picker; device validation is pending.

Experimental [history search](HISTORY_SEARCH.md) finds bounded keyword snippets inside a verified chat using WeChat's own search page.

Experimental [text favorites](FAVORITES.md) save one exact visible message after approval; actual device menu validation remains pending.

`phone_use` includes composite actions for opening a conversation, collecting
recent context, and replying. Conversation titles are found through WeChat
search and verified after navigation, so workflows do not depend on a fixed
row position. Host-side OCR supports WeChat screens whose accessibility tree
is blank or incomplete.

When a notification replaces a nickname emoji with `[Sticker]`, `[emoji]` or
`[表情]`, search uses the longest remaining text fragment. It opens only a
unique visible result whose text and symbol positions match, then verifies the
selected full title. The placeholder cannot recover the original emoji:
ambiguous names, unreadable symbols and names consisting only of placeholders
require the original nickname or a unique WeChat remark. Real Unicode emoji
are preserved through clipboard input; reply bodies are not rewritten.
The host OCR fallback currently requires macOS with `swiftc`; `setup.sh`
builds and installs it automatically. Other platforms continue using Android's
accessibility hierarchy and screenshots.

For automated task inboxes, add an `event_rules` entry to your copied
`~/.hermes/phone-policy.yaml`. Mark only an authenticated, explicit trigger as
`instruction_source: true`, and allow only the actions that workflow needs.
The included reply flow retries navigation failures before sending; after a
send attempt it never resends an unconfirmed message, preventing duplicate
long replies after web research.
For automatic WeChat tasks, the final Telegram report starts with the source
chat and the original triggering message before the agent's result. This
report header is Hermes-specific; it is not part of the shared phone backend.

For an authenticated automatic WeChat reply, the workflow snapshots confirmed
incoming bubbles when it enters the chat and scans once more immediately before
returning Home. Newly visible private messages are deferred through the normal
event-policy queue. In groups, only a new left-side message containing
the configured `@YourBot` trigger is eligible. Unknown direction/type, quote-like `sender:`
summaries and unclassified OCR are ignored rather than guessed. This is a
foreground-notification backstop, not a history sync; device-layout validation
is still required before relying on it for a production inbox.

Authenticated WeChat friend-request notifications bypass the model and are
reported directly to the configured Telegram destination. Reply `/approve` to
accept the named requester without setting a remark, or `/deny` to ignore it.
The approval wait does not hold the phone-operation queue, so ordinary phone
tasks continue while the request is pending.

The friend workflow recognizes both the fixed New Friends entry and its dynamic
request preview under Recommended. It supports Accept or View → Confirm Friend
Request → Done without changing aliases or permissions. Success requires the
named request's Added status; an unknown result is not retried automatically.
Only uniquely identified, visible requests are handled; ambiguous or off-screen
requests are reported for inspection. Final acceptance is not exercised by the
offline tests or navigation-only device checks.

The hybrid backend uses ADB for fast operations (screenshot, tap, swipe, keyevent, app management) and only starts Appium lazily when it needs Unicode text input or when ADB's `uiautomator dump` fails. If Appium is not installed, it falls back to pure ADB automatically.

## Using Without Hermes

The shared phone stack also powers the
[Neko phone_workflows plugin](https://github.com/Ctrl-Creeper/n.e.k.o_plugin_phone_workflows).
For maintainers and coding agents, [DOWNSTREAMS.md](DOWNSTREAMS.md) describes both
downstreams, helper APK identity, sync mechanisms and compatibility checks.

To use phone control from Claude, Codex, GPT, Gemini, or any other agent framework (without Hermes), see the standalone [phone-mcp-server](https://github.com/Ctrl-Creeper/phone-mcp-server) repo. It provides MCP and HTTP servers with the same backend and security model.

## Security

See [SECURITY.md](SECURITY.md) for the full threat model and mitigations.

Key points:
- All ADB commands use argument-list subprocess calls — no shell injection possible
- Phone content (notifications, UI text) is treated as untrusted data, never as instructions
- Policy engine enforces per-app action restrictions (e.g., finance apps are read-only)
- Dangerous actions (`install_apk`, `shell`) always require explicit user approval
- Helper APK socket uses per-session authentication tokens
- Configurable event filtering and sensitive data redaction

## Architecture

Automatic WeChat tasks now retain device ownership through turn cleanup and
persist reply receipts across restarts. See [automatic task reliability](AUTOMATION.md)
for replay behavior, scope, and offline verification.

```
┌─────────────────────────────────────────────────┐
│  Host (PC / Mac)                                │
│  ┌─────────────────────┐  ┌───────────────┐     │
│  │  phone_use           │  │ phone_events  │     │
│  │  (tool plugin)       │  │ (platform)    │     │
│  │                      │  └───────┬───────┘     │
│  │  ┌────────────────┐  │          │             │
│  │  │ ADB Backend    │──┼── ADB ───┤             │
│  │  └────────────────┘  │          │             │
│  │  ┌────────────────┐  │          │             │
│  │  │ Appium (opt.)  │──┼── HTTP ──┤             │
│  │  └────────────────┘  │          │             │
│  └──────────────────────┘          │             │
│  ┌──────────────────┐              │             │
│  │  Appium Server   │  (auto-managed, lazy-start)│
│  └──────────────────┘              │             │
├────────────────────────────────────┼─────────────┤
│  Android Emulator                  │             │
│  ┌───────────────────────────────────────────┐   │
│  │  hermes-phone-agent-v0.2.4.apk           │   │
│  │  • NotificationListenerService            │   │
│  │  • AccessibilityService                   │   │
│  │  • BroadcastReceiver                      │   │
│  └───────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```
## Android Studio / Emulator Setup from Scratch

This guide explains how to install Android Studio, create an Android virtual phone, and start it from the command line with `adb` and `emulator`.

### 1. Install Android Studio

Download and install Android Studio from the official Android Developers website:

```text
https://developer.android.com/studio
```

### 2. Create an Android Virtual Device

Open Android Studio and create a virtual phone:

```text
Android Studio
→ Device Manager
→ Create Virtual Device
```

Recommended settings:

```text
Device: Pixel / Medium Phone
System Image: Android 12 or newer
Image type: Google Play or Google APIs
Architecture: arm64-v8a on Apple Silicon Macs
```

After creating the device, start it once from Android Studio to confirm that it boots correctly.

### 3. Add Android SDK Tools to PATH

On macOS, the Android SDK is usually installed here:

```bash
~/Library/Android/sdk
```

Add the emulator and platform-tools directories to your shell PATH:

```bash
echo 'export ANDROID_HOME="$HOME/Library/Android/sdk"' >> ~/.zshrc
echo 'export PATH="$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify that the tools are available:

```bash
adb version
emulator -version
```

### 4. List Available Virtual Devices

Run:

```bash
emulator -list-avds
```

Example output:

```text
Medium_Phone_API_36.1
```

### 5. Start the Virtual Phone from the Command Line

Use the AVD name from the previous step.

Example:

```bash
emulator @Medium_Phone_API_36.1
```

You can also start it with lower overhead:

```bash
emulator @Medium_Phone_API_36.1 -no-boot-anim -no-audio
```

Do not forget the `@` before the AVD name.

Alternatively, this format also works:

```bash
emulator -avd Medium_Phone_API_36.1
```

### 6. Check That ADB Can See the Device

Open another terminal window and run:

```bash
adb devices
```

Expected output:

```text
List of devices attached
emulator-5554   device
```

If the device shows up as `device`, it is ready.


### 7. Useful ADB Commands

Take a screenshot:

```bash
adb exec-out screencap -p > screen.png
```

Tap the screen:

```bash
adb shell input tap 500 1200
```

Swipe / scroll:

```bash
adb shell input swipe 500 1600 500 500 300
```

Send the Back button:

```bash
adb shell input keyevent 4
```

Send the Home button:

```bash
adb shell input keyevent 3
```
## License

AGPL-3.0 license
