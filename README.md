# virtual-phone-agent

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
  Ps: Instruction to install Android Studio is at the end of README

## Install

```bash

# Install both Hermes plugins
hermes plugins install Ctrl-Creeper/virtual-phone-agent/plugins/phone_use 
hermes plugins install Ctrl-Creeper/virtual-phone-agent/plugins/phone_events

# Install helper APK on a running Android emulator and grant permissions
git clone https://github.com/Ctrl-Creeper/virtual-phone-agent.git
cd virtual-phone-agent
./setup.sh

# Install helper APK on running emulator and grant permissions
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
## Android Emulator Setup
Android Studio / Emulator Setup from Scratch

This guide explains how to install Android Studio, create an Android virtual phone, and start it from the command line with adb and emulator.

1. Install Android Studio

Download Android Studio from the official Android Developers website:

https://developer.android.com/studio

Install it with official instruction, this part is easy.

2. Create an Android Virtual Device

Open Android Studio and create a virtual phone:

Android Studio
→ Device Manager
→ Create Virtual Device

Recommended settings:

Device: Pixel / Medium Phone
System Image: Android 12 or newer
Image type: Google Play or Google APIs
Architecture: arm64-v8a on Apple Silicon Macs

After creating the device, start it once from Android Studio to confirm that it boots correctly.

3. Add Android SDK Tools to PATH

On macOS, the Android SDK is usually installed here:

~/Library/Android/sdk

Add the emulator and platform-tools directories to your shell PATH:

echo 'export ANDROID_HOME="$HOME/Library/Android/sdk"' >> ~/.zshrc
echo 'export PATH="$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH"' >> ~/.zshrc
source ~/.zshrc

Verify that the tools are available:

adb version
emulator -version

4. List Available Virtual Devices

Run:

emulator -list-avds

Example output:

Medium_Phone_API_36.1

5. Start the Virtual Phone from the Command Line

Use the AVD name from the previous step.

Example:

emulator @Medium_Phone_API_36.1

You can also start it with lower overhead:

emulator @Medium_Phone_API_36.1 -no-boot-anim -no-audio

Do not forget the @ before the AVD name.

Alternatively, this format also works:

emulator -avd Medium_Phone_API_36.1

6. Check That ADB Can See the Device

Open another terminal window and run:

adb devices

Expected output:

List of devices attached
emulator-5554   device

If the device shows up as device, it is ready.


7. Useful ADB Commands

Take a screenshot:

adb exec-out screencap -p > screen.png

Tap the screen:

adb shell input tap 500 1200

Swipe / scroll:

adb shell input swipe 500 1600 500 500 300

Send the Back button:

adb shell input keyevent 4

Send the Home button:

adb shell input keyevent 3

Push a file from the Mac to the virtual phone:

adb push ./example.pdf /sdcard/Download/

Pull a file from the virtual phone to the Mac:

adb pull /sdcard/Download/example.pdf .


## License

AGPL-3.0 license
