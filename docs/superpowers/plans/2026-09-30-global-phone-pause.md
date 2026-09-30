# Global Phone Pause Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The authorized Telegram owner can immediately pause or resume all Hermes-owned Android control without stopping Telegram.

**Architecture:** The existing synchronous `pre_gateway_dispatch` hook recognizes exact owner commands before the busy-turn path. A persisted, profile-scoped pause gate cancels queued/in-flight device work, and the phone-events adapter stops/restarts its monitors. Shared backends and APK remain unchanged.

**Tech Stack:** Python 3.10+, Hermes plugin hooks, asyncio, `threading.Condition`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-30-global-phone-pause-design.md`

## Global Constraints

- Paused flag survives gateway restart and fails closed on corrupt state.
- Owner Telegram ID and chat/thread must match configured settings; hook fires before Hermes authentication and must check it.
- No new core tool, system-prompt edits, Hermes core patch, Neko/MCP file changes, or real WeChat sends.
- An already-issued irreversible tap cannot be undone; report uncertainty and never resend.
- No paused-period notifications or cancelled work replay on resume.

---

### Task 1: Persistent global pause gate

**Files:** Create `plugins/phone_use/pause.py`; test `tests/test_phone_pause.py`.

**Interfaces:** `PhonePauseGate(path: Path)`, `snapshot() -> (paused: bool, generation: int)`, `pause()`, `resume()`, `ensure_active(generation: int | None = None)`, `PhonePaused`.

- [ ] Write a failing test using a temporary Hermes home: `gate.pause(); assert PhonePauseGate(path).snapshot()[0]; gate.resume(); assert not PhonePauseGate(path).snapshot()[0]`. Test corrupt JSON fails closed and repeated commands are idempotent.
- [ ] Run `PYTHONPATH=. python3 -m pytest -q tests/test_phone_pause.py` and confirm red.
- [ ] Implement atomic write (`tempfile.NamedTemporaryFile(dir=path.parent)`, `os.chmod(..., 0o600)`, `os.replace`), process lock, generation increments, and fail-closed reads. `ensure_active` raises `PhonePaused` if paused or a stale generation.
- [ ] Run the same tests to green; commit gate and tests.

### Task 2: Device admission, cancellation, and cleanup

**Files:** Modify `plugins/phone_use/tool.py`; test `tests/test_phone_pause.py`.

**Interfaces:** `pause_phone_operations() -> set[str]` returns tracked task keys, `resume_phone_operations()`, `_finish_turn(task_id, session_id)` reuses existing Home/ownership cleanup; `handle_phone_use` returns `{"error":"phone paused"}` before approvals and after queue acquisition. One special cleanup HOME is allowed.

- [ ] Add tests for paused ordinary/auto tool calls, a pending approval interrupted by pause, a queue waiter waking with `PhonePaused`, and a stale turn after resume. Use the public `handle_phone_use` boundary and fake backend.
- [ ] Run the focused tests and confirm red.
- [ ] Add gate checks before approval and device use, an interruptible queue with `notify_all`, a guarded backend proxy checking generation before each composite-workflow primitive, and finally-only cleanup. Track active/waiting task keys for gateway cancellation; preserve delivery receipt semantics.
- [ ] Run focused and `PYTHONPATH=. python3 -m pytest -q tests`; commit.

### Task 3: Telegram control and event lifecycle

**Files:** Create `plugins/phone_use/control.py`; modify `plugins/phone_use/__init__.py`, `plugins/phone_events/adapter.py`; test `tests/test_phone_pause.py`.

**Interfaces:** `pre_gateway_dispatch(event, gateway, **kwargs) -> dict | None` (synchronous hook); `PhoneEventAdapter.connect/disconnect`, `_on_raw_event` respect gate. Only exact owner messages return `{"action":"skip","reason":"phone-control"}` and schedule direct Telegram acknowledgement and cancellation on the gateway loop.

- [ ] Write failing tests for exact phrases/slash forms, owner/chat/thread/forward rejection, busy phone turn cancellation, helper monitor stop/resume, dropped callbacks, restart-paused adapter, failed reconnect, no queued replay.
- [ ] Run focused tests and confirm red.
- [ ] Implement owner lookup from configured `phone_events.extra`, verify `gateway._is_user_authorized`, reject internal/forwarded events, close gate synchronously, then schedule cancellation using Hermes's `_interrupt_and_clear_session` and cached session sources. Disconnect monitors on pause; reconnect only after success and then open action gate. Do not interrupt non-phone turns.
- [ ] Run focused and full tests, `git diff --check`; commit.

### Task 4: Documentation and deployment verification

**Files:** Modify `README.md`, `README_CN.md`; test `tests/test_phone_pause.py`.

- [ ] Document examples and what “immediate” can/cannot undo, persistence, owner setup, unaffected downstreams.
- [ ] Run `PYTHONPATH=. python3 -m pytest -q tests`, `git diff --check`, and a real import/startup integration test against a temporary `HERMES_HOME`, without ADB effects or Telegram sends.
- [ ] Inspect diff for credentials; submit a plugin PR, then update the installed plugin only after verified and approved. Verify helper and Telegram health without generating phone actions; commit docs.
