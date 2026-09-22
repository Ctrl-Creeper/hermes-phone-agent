# hermes-phone-agent

English | [中文](README_CN.md)

Two [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins + one
Android helper APK that let Hermes control and react to a virtual Android phone.

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

Authenticated WeChat friend-request notifications bypass the model and are
reported directly to the configured Telegram destination. Reply `/approve` to
accept the named requester without setting a remark, or `/deny` to ignore it.
The approval wait does not hold the phone-operation queue, so ordinary phone
tasks continue while the request is pending.

The hybrid backend uses ADB for fast operations (screenshot, tap, swipe, keyevent, app management) and only starts Appium lazily when it needs Unicode text input or when ADB's `uiautomator dump` fails. If Appium is not installed, it falls back to pure ADB automatically.

## Using Without Hermes

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
