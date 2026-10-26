# WeChat Notification Participation Implementation Plan

> Execute inline using superpowers:executing-plans, with test-first checks and a final independent review.

**Goal:** Let authorized group notification batches choose natural participation without per-message model calls or phone use while buffering.

**Architecture:** Add a host-only per-conversation async scheduler in phone_events/group_participation.py. The adapter admits only authenticated, policy-authorized group observations and dispatches an immutable batch through the existing gateway lifecycle. Hooks bind tool targets and collect actual reply receipts.

**Tech Stack:** Python asyncio, dataclasses, ContextVar, pytest; existing Hermes plugin hooks.

**Spec:** docs/superpowers/specs/2026-10-26-wechat-notification-participation.md

## Constraints

- Feature defaults off; settings live in config.yaml and phone-policy.yaml.
- No phone or model work while buffering.
- No cancellation of a started turn in response to new messages.
- Per-group history, buffers, timers, policy scopes and targets stay separate.
- Existing device ownership and durable delivery semantics remain intact.
- No live settings, APK, shared phone stack or Hermes core modifications.

## Task 1: Notification scheduler

Files: create plugins/phone_events/group_participation.py and tests/test_group_participation.py.
Interface: ParticipationSettings.from_config, GroupParticipation.observe/close;
GroupBatch carries copied events, decisions, metrics and reply accounting.

- [x] Write async behavior tests for quiet-window batching, mention priority,
  independent groups, immutable running batches, minimum evaluation interval,
  cumulative notification updates, duplicate replay, bounded buffers and close.
- [x] Run `PYTHONPATH=. python3 -m pytest -q tests/test_group_participation.py` and
  observe failures for the missing scheduler.
- [x] Implement local settings, per-lane timers and one in-flight batch per lane.
- [x] Re-run the scheduler tests and inspect results.

## Task 2: Gateway integration and scope

Files: modify plugins/phone_events/adapter.py, event_filter.py and __init__.py;
add adapter/hook tests in tests/test_group_participation.py.

- [x] Add failing tests that use the real adapter admission path: ignore/report
  policy and authentication remain enforced; buffering never touches the phone;
  global event rate does not discard other groups; batch context and send target
  stay isolated, rejecting report-topic routing that would merge conversations.
- [x] Add optional group admission before ordinary forwarding, after policy and
  redaction; schedule observations on the gateway loop.
- [x] Return and await the existing dispatch future, binding group context to
  each actual background turn and recording send results through plugin hooks.
- [x] Use a stable group participation prompt allowing NO_REPLY or direct
  wechat_reply in the same model call; preserve the original prompt otherwise.
- [x] Clear only pending buffers/timers on pause/disconnect; keep existing
  explicit pause cancellation semantics, without adding message cancellation.
- [x] Verify relevant integration and existing lifecycle tests.

## Task 3: Documentation and delivery

Files: update README.md, README_CN.md and AUTOMATION.md with the opt-in recipe,
notification limitations, policy requirements and unchanged device serialization.

- [x] Run `PYTHONPATH=. python3 -m pytest -q tests` and `git diff --check`.
- [x] Run scripts/check_gateway_lifecycle.py with installed Hermes, noop phone
  and a temporary HERMES_HOME.
- [x] Obtain an independent read-only code review and resolve material findings.
- [ ] Commit, push the feature branch, and create a PR against main describing
  offline evidence and unverified real-WeChat mute/foreground behavior.
