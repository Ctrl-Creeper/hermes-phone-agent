# Security Model

hermes-phone-agent controls an Android emulator — a full operating system
with network access, storage, and installed apps. This document describes
the threat model and the mitigations baked into every layer.

## Threat Model

| Threat | Vector | Mitigation |
|--------|--------|------------|
| **Command injection via ADB** | Malicious text from LLM passed to `adb shell` | All arguments go through `_sanitize_shell_arg()`; no string interpolation into shell commands; use list-based `subprocess` calls |
| **Prompt injection via phone content** | Notification text / UI element labels contain instructions aimed at the agent | Events are tagged as `[PHONE_DATA]` in the prompt; system prompt instructs model to treat phone content as data, never instructions |
| **Sensitive data exfiltration** | Agent reads notifications containing OTPs, passwords, private messages | Configurable redaction filters strip OTP patterns, credit card numbers; notification body is truncated; opt-in allowlist for which apps' notifications are forwarded |
| **Lateral movement** | Agent uses `adb shell` to pivot from emulator to host network | Blocked command patterns prevent `adb forward`, port-binding, reverse shells; `shell` action requires explicit user approval per invocation |
| **Unrestricted app install** | Agent installs malicious APKs | `install_apk` requires user approval; APK path must be on host filesystem (no URL fetch + install) |
| **Denial of service** | Flood of phone events overwhelms agent | Rate limiter caps events per second; deduplication window; cooldown per event type |
| **Helper APK socket hijack** | Another app on the emulator connects to the event socket | Socket bound to localhost only; authentication token required on handshake; token rotated per session |

## Policy Engine

The policy engine (`phone-policy.yaml`) is a hard boundary between the AI and the phone. It governs:
- **Event behavior**: whether the agent sees an event at all (`ignore`), must ask the user before acting (`report`), or may act autonomously (`auto`)
- **Action restrictions**: per-app blocklists/allowlists that prevent the agent from interacting with sensitive apps (e.g., finance apps are read-only)
- **Global restrictions**: actions like `install_apk` and `shell` that always require human approval regardless of any rule

The policy is enforced at two points — the event pipeline (before the LLM sees anything) and the action pipeline (before ADB executes anything). Even if the LLM decides to tap on Alipay, the policy gate blocks it with a clear error. See [POLICY.md](POLICY.md) for the full config reference.

## Design Principles

1. **Allowlist over blocklist** — only explicitly permitted actions execute without approval
2. **No shell string interpolation** — all subprocess calls use argument lists, never f-strings passed to `shell=True`
3. **Phone content is data, not instructions** — all text from the phone is wrapped and labeled before reaching the LLM
4. **Minimal permissions on APK** — the helper requests only NotificationListenerService, AccessibilityService, and RECEIVE_BOOT_COMPLETED
5. **Defense in depth** — even if one layer fails (e.g., sanitization), the approval gate, policy engine, and blocked patterns provide backup
6. **Fail closed** — if the ADB connection drops, the event socket disconnects, or a safety check raises an exception, the action is denied rather than allowed
7. **User-controlled autonomy** — the policy config determines what the agent may do on its own; the default is `report` (always ask)

## Reporting Vulnerabilities

If you find a security issue, please open a private advisory on GitHub
rather than a public issue.
