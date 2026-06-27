# Phone Agent Policy Configuration

The policy file controls what the AI agent can do with your phone — what it handles automatically, what it reports to you first, and what it ignores entirely.

## Quick Start

Copy the default config to your hermes directory:

```bash
cp phone-policy.yaml ~/.hermes/phone-policy.yaml
```

Edit it to match your preferences. Changes take effect immediately (hot-reload).

## Config Location

The agent searches for the config in this order (first found wins):

| Priority | Path | Use case |
|----------|------|----------|
| 1 | `$PHONE_POLICY_PATH` | CI / testing override |
| 2 | `./.hermes/phone-policy.yaml` | Project-specific policy |
| 3 | `~/.hermes/phone-policy.yaml` | Your personal policy |
| 4 | *(none found)* | Conservative defaults: everything = `report` |

## Three Behavior Levels

| Level | What happens | Use for |
|-------|-------------|---------|
| `auto` | Agent acts immediately, tells you the result afterward | Crashes, toasts, system events |
| `report` | Agent summarizes the event, waits for your instruction | Messages, payments, anything you care about |
| `ignore` | Event is silently dropped — agent never sees it | Spam, OTPs, routine app switches |

## Config Structure

```yaml
# What happens when no rule matches
default_behavior: report

# Actions that ALWAYS need your approval, even in auto mode
global_restrict:
  - install_apk
  - shell

# Group apps by trust level
app_profiles:
  profile_name:
    packages: [com.example.app]
    on_event: report
    blocked_actions: [tap, type]      # these actions are forbidden
    allowed_actions: [capture, tap]   # only these are allowed (if set)
    notes: "Instructions for the agent"

# Fine-grained rules for specific events
event_rules:
  - match:
      event: notification             # event type
      package: "com.example.app"      # exact or glob (com.example.*)
      title_regex: "payment|付款"     # regex on title
      body_regex: "\\d{4,6}.*code"   # regex on body
    behavior: report
    allowed_actions: [capture]
    notes: "Report payment notifications, don't interact."
    priority: 10                      # higher = matched first
```

### Evaluation Order

1. **Event rules** — checked first, highest priority wins
2. **App profiles** — checked if no event rule matched
3. **Default behavior** — used if nothing else matched
4. **Global restrict** — always applied on top of everything

### Available Actions

| Category | Actions |
|----------|---------|
| Read-only | `capture`, `wait`, `list_apps`, `current_app`, `device_info` |
| Interactive | `tap`, `double_tap`, `long_press`, `swipe`, `type`, `clear_text`, `set_text`, `keyevent`, `launch_app`, `stop_app` |
| Dangerous | `install_apk`, `shell` |

### Package Matching

- **Exact**: `com.tencent.mm` — matches only WeChat
- **Glob**: `com.android.*` — matches all `com.android` packages
- **Regex** (event rules only): `title_regex: "支付|payment"` — regex on event content

## Examples

### "I want the agent to auto-dismiss spam but never touch my bank app"

```yaml
default_behavior: report

app_profiles:
  banking:
    packages: [com.mybank.app]
    on_event: report
    blocked_actions: [tap, type, swipe, set_text, keyevent, launch_app, stop_app]

event_rules:
  - match:
      event: notification
      title_regex: "广告|spam|promotion"
    behavior: ignore
    priority: 5
```

### "Auto-reply to my girlfriend on WeChat, report everyone else"

```yaml
default_behavior: report

event_rules:
  - match:
      package: "com.tencent.mm"
      event: notification
      title_regex: "^Alice$"
    behavior: auto
    notes: "Auto-reply to Alice on WeChat. Be friendly and brief."
    priority: 10

  - match:
      package: "com.tencent.mm"
      event: notification
    behavior: report
    notes: "Other WeChat messages — ask me first."
```

### "Full lockdown — report everything, block all interaction"

```yaml
default_behavior: report
global_restrict:
  - install_apk
  - shell
  - tap
  - type
  - swipe
  - set_text
  - keyevent
  - launch_app
  - stop_app
```

---

## Auto-Generate Your Config with AI

You can ask any AI assistant (including hermes itself) to generate a policy config for you. Use this prompt:

---

**Prompt template** — copy everything below, fill in the blanks, and send it to an AI:

````
Generate a phone-policy.yaml config for the hermes phone agent.

Here are my requirements:

**My apps and how I want them handled:**
- [app name]: [auto / report / ignore] — [any restrictions?]
  (example: "WeChat: report — never auto-reply, but allow reading")
  (example: "Alipay: report — read-only, block all interaction")
  (example: "Calendar: auto — let the agent dismiss reminders")

**Events I want handled automatically (auto):**
- [describe what the agent should do on its own]
  (example: "auto-capture screenshots when apps crash")
  (example: "auto-dismiss spam notifications matching '广告' or 'promotion'")

**Events I want reported to me first (report):**
- [describe what the agent should tell you about]
  (example: "all messages from messaging apps")
  (example: "any notification from unknown apps")

**Events I want completely ignored (ignore):**
- [describe what to drop silently]
  (example: "OTP / verification code notifications")
  (example: "routine app switches")
  (example: "all notifications from com.spam.app")

**Special restrictions:**
- [any extra rules?]
  (example: "never type anything into the browser")
  (example: "never interact with any finance app")
  (example: "only allow capture and device_info globally")

**My default stance (when no rule matches):**
- [auto / report / ignore]
  (example: "report — I want to approve before the agent acts")

---

Output format: a valid YAML file following this schema:

```yaml
default_behavior: auto | report | ignore

global_restrict:       # actions always needing human approval
  - action_name

app_profiles:          # group apps by trust level
  profile_name:
    packages: [android.package.name, ...]
    on_event: auto | report | ignore
    blocked_actions: [action, ...]   # forbidden (blacklist)
    allowed_actions: [action, ...]   # if set, only these are allowed (whitelist)
    notes: "instruction for the AI agent"

event_rules:           # fine-grained pattern matching
  - match:
      event: notification | crash | app_switch | toast | broadcast
      package: "exact.name" or "glob.pattern.*"
      title_regex: "regex pattern"
      body_regex: "regex pattern"
    behavior: auto | report | ignore
    allowed_actions: [action, ...]
    notes: "instruction for the AI agent"
    priority: 0-10     # higher = matched first
```

Available actions:
  Read-only: capture, wait, list_apps, current_app, device_info
  Interactive: tap, double_tap, long_press, swipe, type, clear_text, set_text, keyevent, launch_app, stop_app
  Dangerous: install_apk, shell

Rules:
- Event rules are checked before app profiles
- Higher priority event rules are checked first
- First match wins
- global_restrict is always enforced, even in auto mode
- The "notes" field is shown to the AI agent — write it as an instruction
- Keep dangerous actions (install_apk, shell) in global_restrict unless you have a specific reason
- Use allowed_actions as a whitelist OR blocked_actions as a blacklist, not both
- Package names are Android package IDs (e.g., com.tencent.mm for WeChat)
```

Put the result in `~/.hermes/phone-policy.yaml`.
````

---

### Example conversation

> **You:** Generate a phone-policy.yaml. I want:
> - WeChat: report messages, auto-dismiss group chat spam with "广告" in the title
> - Alipay: read-only, never interact
> - Auto-capture on crash
> - Ignore OTPs and app switches
> - Default: report

> **AI:** *(generates a complete phone-policy.yaml)*

> **You:** Also add Telegram as auto — let it reply to messages from "Mom"

> **AI:** *(updates the config with a Telegram event rule)*

Save the output to `~/.hermes/phone-policy.yaml` and it takes effect immediately.
