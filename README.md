# hermes-phone-agent

Two [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins + one
Android helper APK that let Hermes control and react to a virtual Android phone.

## Components

| Component | Type | Purpose |
|-----------|------|---------|
| `plugins/phone_use` | Hermes tool plugin | Agent **controls** the phone — tap, swipe, type, screenshot, launch apps via ADB |
| `plugins/phone_events` | Hermes platform plugin | Phone **triggers** the agent — notifications, app switches, UI changes |
| `helper-apk` | Android app | Runs on-device services that capture rich events and forward them to the host |

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
- Android SDK Platform Tools (`adb` on PATH)
- An Android emulator running (Android Studio AVD or `emulator` CLI)

## Install

```bash
# Install both plugins
hermes plugins install yourname/hermes-phone-agent/plugins/phone_use
hermes plugins install yourname/hermes-phone-agent/plugins/phone_events

# Install helper APK on running emulator and grant permissions
./setup.sh
```

## Policy

The agent's autonomy is controlled by `phone-policy.yaml` — a config file that defines what the agent can do on its own vs. what requires your approval. See [POLICY.md](POLICY.md) for the full reference and examples.

```bash
# Copy the default policy
cp phone-policy.yaml ~/.hermes/phone-policy.yaml

# Edit to match your preferences — changes take effect immediately
```

You can also ask any AI assistant to generate a config for you using the prompt template in POLICY.md.

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
┌──────────────────────────────────────────┐
│  Host (PC / Mac)                         │
│  ┌─────────────────┐  ┌───────────────┐  │
│  │  phone_use      │  │ phone_events  │  │
│  │  (tool plugin)  │  │ (platform)    │  │
│  └────────┬────────┘  └───────┬───────┘  │
│           │    ADB            │          │
├───────────┼───────────────────┼──────────┤
│  Android Emulator             │          │
│  ┌─────────────────────────────────────┐ │
│  │  phone-agent-helper.apk            │ │
│  │  • NotificationListenerService     │ │
│  │  • AccessibilityService            │ │
│  │  • BroadcastReceiver               │ │
│  └─────────────────────────────────────┘ │
└──────────────────────────────────────────┘
```

## License

AGPL-3.0 license
