"""Entry point for the phone_use tool — dispatch, approval, response shaping.

Mirrors hermes's tools/computer_use/tool.py pattern.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .backend import (
    ActionResult,
    CaptureResult,
    PhoneBackend,
    UIElement,
)
from .policy import bind_event_policy as bind_event_policy, get_event_policy, get_policy
from .wechat import open_chat as open_wechat_chat
from .wechat import reply as reply_to_wechat
from .wechat import accept_friend_request as accept_wechat_friend_request
from .wechat_context import collect_context as collect_wechat_context

logger = logging.getLogger(__name__)

# ── Approval ────────────────────────────────────────────────────────

_approval_callback = None


def set_approval_callback(cb) -> None:
    global _approval_callback
    _approval_callback = cb


_SAFE_ACTIONS = frozenset({
    "capture", "wait", "list_apps", "current_app", "device_info",
})

# Only these actions take an explicit package argument. For all other
# non-safe actions (tap, swipe, type, etc.), the policy check uses the
# foreground app — agent-supplied 'package' is ignored to prevent bypass.
_PACKAGE_AWARE_ACTIONS = frozenset({"launch_app", "stop_app"})
_FIXED_PACKAGE_ACTIONS = {
    "wechat_open_chat": "com.tencent.mm",
    "wechat_reply": "com.tencent.mm",
    "wechat_collect_context": "com.tencent.mm",
}

_APPROVAL_REQUIRED = frozenset({
    "tap", "double_tap", "long_press", "swipe",
    "type", "clear_text", "set_text", "keyevent",
    "launch_app", "stop_app",
    "wechat_open_chat",
    "wechat_collect_context",
})

# These always prompt, even if session-auto-approve is on.
_ALWAYS_PROMPT = frozenset({
    "install_apk", "shell", "wechat_reply",
})

_WORKFLOW_ACTIONS = _APPROVAL_REQUIRED
_WORKFLOW_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class WorkflowScope:
    task_id: str
    session_id: str
    goal: str
    allowed_actions: frozenset[str]
    expires_at: float


_workflow_lock = threading.Lock()
_workflow_scopes: Dict[tuple[str, str], WorkflowScope] = {}
_reply_result_lock = threading.Lock()
_reply_results: Dict[tuple[str, str, str, str, str], str] = {}


class DeviceOperationQueue:
    """Fairly serialize physical operations against the shared phone."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = False
        self._owner = ""

    @contextmanager
    def turn(self, action: str, task_id: str = "", session_id: str = "", *, retain: bool = False):
        token = object()
        with self._condition:
            self._waiting.append(token)
            position = len(self._waiting) + int(self._active)
            if position > 1:
                logger.info(
                    "phone device queue: queued action=%s position=%d task=%s session=%s",
                    action, position, task_id or "-", session_id or "-",
                )
            while self._active or (self._owner and self._owner != task_id) or (
                not self._owner and self._waiting[0] is not token
            ):
                self._condition.wait()
            self._waiting.remove(token)
            self._active = True
            if retain and task_id:
                self._owner = task_id

        logger.info(
            "phone device queue: starting action=%s task=%s session=%s",
            action, task_id or "-", session_id or "-",
        )
        try:
            yield
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()

    def owns(self, task_id: str) -> bool:
        with self._condition:
            return bool(task_id) and self._owner == task_id

    def release(self, task_id: str) -> None:
        with self._condition:
            if task_id and self._owner == task_id:
                self._owner = ""
                self._condition.notify_all()


_device_operation_queue = DeviceOperationQueue()

_session_auto_approve = False
_always_allow: set = set()

# ── Backend ─────────────────────────────────────────────────────────

_backend_lock = threading.Lock()
_backend: Optional[PhoneBackend] = None


def _resolve_android_serial(config_path: Optional[Path] = None) -> Optional[str]:
    """Resolve the configured emulator serial, with env as a legacy fallback."""
    if config_path is None:
        try:
            from hermes_constants import get_hermes_home

            config_path = Path(get_hermes_home()) / "config.yaml"
        except Exception:
            config_path = Path.home() / ".hermes" / "config.yaml"

    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        serial = (
            raw.get("platforms", {})
            .get("phone_events", {})
            .get("extra", {})
            .get("serial")
        )
        if isinstance(serial, str) and serial.strip():
            return serial.strip()
    except (OSError, TypeError, AttributeError, ValueError):
        logger.debug("Unable to read Android serial from %s", config_path, exc_info=True)

    return os.environ.get("ANDROID_SERIAL") or None


def _get_backend() -> PhoneBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            backend_name = os.environ.get(
                "HERMES_PHONE_BACKEND", "adb"
            ).lower()
            if backend_name == "adb":
                from .adb_backend import AdbBackend
                serial = _resolve_android_serial()
                _backend = AdbBackend(serial=serial)
            elif backend_name in ("hybrid", "appium"):
                from .appium_backend import HybridBackend
                serial = _resolve_android_serial()
                _backend = HybridBackend(serial=serial)
            elif backend_name == "noop":
                _backend = _NoopBackend()
            else:
                raise RuntimeError(
                    f"Unknown HERMES_PHONE_BACKEND={backend_name!r}"
                )
            try:
                _backend.start()
            except Exception:
                _backend = None
                raise
        return _backend


def reset_backend_for_tests() -> None:
    global _backend, _session_auto_approve, _always_allow, _device_operation_queue
    _device_operation_queue = DeviceOperationQueue()
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = None
    _session_auto_approve = False
    _always_allow = set()
    with _workflow_lock:
        _workflow_scopes.clear()
    with _reply_result_lock:
        _reply_results.clear()


def _reply_result_key(
    task_id: str, session_id: str, chat: str, text: str, quote_identity: str = '',
) -> tuple[str, str, str, str, str]:
    return (task_id.strip(), session_id.strip(), chat.strip(), text, quote_identity)


def _cached_reply_result(
    task_id: str, session_id: str, chat: str, text: str, quote_identity: str = '',
) -> Optional[str]:
    if not task_id.strip():
        return None
    with _reply_result_lock:
        return _reply_results.get(
            _reply_result_key(task_id, session_id, chat, text, quote_identity),
        )


def _remember_reply_result(
    task_id: str, session_id: str, chat: str, text: str, result: Any, quote_identity: str = '',
) -> None:
    if not task_id.strip() or not isinstance(result, str):
        return
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or not (
        payload.get("meta", {}).get("delivery_attempted")
    ):
        return
    with _reply_result_lock:
        if len(_reply_results) >= 256:
            _reply_results.pop(next(iter(_reply_results)))
        _reply_results[
            _reply_result_key(task_id, session_id, chat, text, quote_identity)
        ] = result


def _clear_reply_results(task_id: str, session_id: str) -> None:
    prefix = (task_id.strip(), session_id.strip())
    with _reply_result_lock:
        stale = [key for key in _reply_results if key[:2] == prefix]
        for key in stale:
            _reply_results.pop(key, None)


def _workflow_key(task_id: str, session_id: str) -> tuple[str, str]:
    return ((task_id or "").strip(), (session_id or "").strip())


def _get_live_workflow(task_id: str, session_id: str) -> Optional[WorkflowScope]:
    key = _workflow_key(task_id, session_id)
    if not key[0]:
        return None
    with _workflow_lock:
        scope = _workflow_scopes.get(key)
        if scope is not None and scope.expires_at <= time.monotonic():
            _workflow_scopes.pop(key, None)
            return None
        return scope


def _workflow_authorizes(action: str, task_id: str, session_id: str) -> bool:
    scope = _get_live_workflow(task_id, session_id)
    return scope is not None and action in scope.allowed_actions


def _clear_workflow(task_id: str, session_id: str) -> bool:
    key = _workflow_key(task_id, session_id)
    with _workflow_lock:
        return _workflow_scopes.pop(key, None) is not None


def _begin_workflow(args: Dict[str, Any], task_id: str, session_id: str) -> str:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return json.dumps({"error": "begin_workflow requires a non-empty 'goal'"})
    if not (task_id or "").strip():
        return json.dumps({
            "error": "begin_workflow requires a turn-scoped task_id",
            "hint": "Start the workflow from a normal Hermes conversation turn.",
        })

    requested = args.get("allowed_actions")
    if requested is None:
        allowed = _WORKFLOW_ACTIONS
    elif not isinstance(requested, list) or not all(
        isinstance(item, str) for item in requested
    ):
        return json.dumps({"error": "allowed_actions must be an array of action names"})
    else:
        allowed = frozenset(item.strip().lower() for item in requested if item.strip())
        unsupported = sorted(allowed - _WORKFLOW_ACTIONS)
        if unsupported:
            return json.dumps({
                "error": "workflow contains actions that cannot inherit approval",
                "actions": unsupported,
            })
        if not allowed:
            return json.dumps({"error": "allowed_actions cannot be empty"})

    approval_args = dict(args)
    approval_args["goal"] = goal
    err = _request_approval("begin_workflow", approval_args, force_prompt=True)
    if err is not None:
        return err

    key = _workflow_key(task_id, session_id)
    scope = WorkflowScope(
        task_id=key[0],
        session_id=key[1],
        goal=goal,
        allowed_actions=allowed,
        expires_at=time.monotonic() + _WORKFLOW_TTL_SECONDS,
    )
    with _workflow_lock:
        _workflow_scopes[key] = scope
    return json.dumps({
        "ok": True,
        "workflow_active": True,
        "goal": goal,
        "allowed_actions": sorted(allowed),
        "expires_in_seconds": int(_WORKFLOW_TTL_SECONDS),
        "instruction": "Complete the workflow, then call end_workflow.",
    }, ensure_ascii=False)


def _finish_workflow(task_id: str, session_id: str) -> bool:
    existed = _clear_workflow(task_id, session_id)
    if existed:
        return_phone_home(task_id)
    return existed


def _finish_turn(task_id: str, session_id: str) -> None:
    try:
        if _device_operation_queue.owns(task_id):
            _clear_workflow(task_id, session_id)
            return_phone_home(task_id)
        else:
            _finish_workflow(task_id, session_id)
    finally:
        _clear_reply_results(task_id, session_id)
        _device_operation_queue.release(task_id)


def _result_failed(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("error")) or result.get("ok") is False
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and (
        bool(payload.get("error")) or payload.get("ok") is False
    )


def _finish_failed_workflow(
    result: Any, task_id: str, session_id: str, had_scope: bool,
) -> Any:
    if had_scope and _result_failed(result):
        _finish_workflow(task_id, session_id)
    return result


# ── Dispatch ────────────────────────────────────────────────────────

def handle_phone_use(args: Dict[str, Any], **kwargs) -> Any:
    """Main entry point — dispatched by hermes tools.registry."""
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing 'action'"})

    task_id = str(kwargs.get("task_id") or "")
    session_id = str(kwargs.get("session_id") or "")
    if action == "begin_workflow":
        return _begin_workflow(args, task_id, session_id)
    if action == "end_workflow":
        _clear_workflow(task_id, session_id)
        return_phone_home(task_id)
        return json.dumps({
            "ok": True,
            "workflow_active": False,
            "returned_home": True,
        })

    workflow = _get_live_workflow(task_id, session_id)
    workflow_active = workflow is not None

    policy = get_policy()
    event_policy = get_event_policy()
    if event_policy is not None and not event_policy.action_allowed(action):
        return _finish_failed_workflow(json.dumps({
            "error": "blocked by phone event policy",
            "action": action,
            "reason": event_policy.notes or (
                f"event policy does not allow '{action}'"
            ),
        }), task_id, session_id, workflow_active)

    # Approval gate. Globally restricted actions require a fresh approval even
    # in an auto event; ordinary interactive actions may run under an explicit
    # auto event allowlist.
    trusted_auto_wechat_reply = (
        action == "wechat_reply"
        and event_policy is not None
        and event_policy.is_auto
        and event_policy.instruction_source
        and event_policy.action_allowed(action)
    )
    quote_identity = json.dumps([args.get(key, '') for key in (
        'quote_text', 'quote_sender', 'quote_context',
    )], ensure_ascii=False) if args.get('quote_text') else ''
    if trusted_auto_wechat_reply:
        cached = _cached_reply_result(
            task_id, session_id, args.get("chat", ""), args.get("text", ""),
            quote_identity,
        )
        if cached is not None:
            return cached
    if (action in _ALWAYS_PROMPT and not trusted_auto_wechat_reply) or policy.requires_approval(action):
        err = _request_approval(action, args, force_prompt=True)
        if err is not None:
            return _finish_failed_workflow(
                err, task_id, session_id, workflow_active,
            )
    elif action in _APPROVAL_REQUIRED and not _workflow_authorizes(
        action, task_id, session_id,
    ):
        err = _request_approval(action, args)
        if err is not None:
            return _finish_failed_workflow(
                err, task_id, session_id, workflow_active,
            )

    try:
        backend = _get_backend()
    except Exception as e:
        return _finish_failed_workflow(json.dumps({
            "error": f"phone backend unavailable: {e}",
            "hint": "Ensure adb is on PATH and an emulator is running.",
        }), task_id, session_id, workflow_active)

    # A trusted automatic turn owns the phone across tool calls, including
    # web research between them. The existing on_session_end hook releases it.
    retain = bool(task_id and event_policy and event_policy.is_auto
                  and event_policy.instruction_source)
    with _device_operation_queue.turn(action, task_id, session_id, retain=retain):
        result = _dispatch_under_device_lock(backend, action, args, task_id, session_id,
                                            trusted_auto_wechat_reply, policy)
    return _finish_failed_workflow(result, task_id, session_id, workflow_active)


def _dispatch_under_device_lock(backend, action, args, task_id, session_id,
                                trusted_auto_wechat_reply, policy):
    # Check the foreground only after acquiring ownership: another task may
    # have changed it while this task was waiting.
    # For launch_app/stop_app, the explicit 'package' arg is the target.
    # For all other non-safe actions, use the foreground app — ignore any
    # agent-supplied 'package' to prevent policy bypass.
    target_pkg = ""
    if action in _FIXED_PACKAGE_ACTIONS:
        target_pkg = _FIXED_PACKAGE_ACTIONS[action]
    elif action in _PACKAGE_AWARE_ACTIONS:
        target_pkg = args.get("package", "")
    elif action not in _SAFE_ACTIONS:
        try:
            fg = backend.current_app()
            target_pkg = fg.get("package", "")
        except Exception as e:
            return json.dumps({
                "error": "unable to enforce phone app policy",
                "action": action,
                "reason": f"foreground app lookup failed: {e}",
            })
        if not target_pkg:
            return json.dumps({
                "error": "unable to enforce phone app policy",
                "action": action,
                "reason": "foreground app package is unknown",
            })
    if target_pkg:
        decision = policy.check_action(action, target_pkg)
        if not decision.action_allowed(action):
            return json.dumps({
                "error": "blocked by phone policy",
                "action": action,
                "package": target_pkg,
                "reason": decision.notes or f"policy restricts '{action}' on '{target_pkg}'",
                "hint": "Edit phone-policy.yaml to change this rule.",
            })

    try:
        if trusted_auto_wechat_reply:
            cached = _cached_reply_result(task_id, session_id, args.get("chat", ""), args.get("text", ""))
            if cached is not None:
                return cached
            decision = get_event_policy()
            if decision.delivery_identity:
                return _durable_wechat_reply(backend, args, decision.delivery_identity)
            result = _dispatch(backend, action, args)
        else:
            result = _dispatch(backend, action, args)
    except Exception as e:
        logger.exception("phone_use %s failed", action)
        result = _error_response_with_capture(backend, action, f"{action} failed: {e}")

    if trusted_auto_wechat_reply:
        _remember_reply_result(
            task_id,
            session_id,
            args.get("chat", ""),
            args.get("text", ""),
            result,
            quote_identity,
        )
    return result


def _durable_wechat_reply(backend, args, identity: str) -> str:
    from .delivery import DeliveryJournal, state_directory

    chat, text = args.get("chat", ""), args.get("text", "")
    if not isinstance(chat, str) or not chat.strip() or not isinstance(text, str) or not text.strip():
        return json.dumps({"ok": False, "error": "wechat_reply requires chat and text"})
    journal = DeliveryJournal(state_directory())
    with journal.receipt(identity, _resolve_android_serial() or "default", chat, text) as receipt:
        state = receipt.state
        if state != "prepared":
            return json.dumps({
                "ok": state == "confirmed", "action": "wechat_reply",
                "message": ("Previously confirmed reply; not sent again" if state == "confirmed"
                            else "Previous send may have completed; not sent again. Report uncertainty through Telegram."),
                "meta": {"delivery_attempted": True, "delivery_status":
                         "confirmed" if state == "confirmed" else "uncertain", "duplicate_suppressed": True},
            })
        receipt.record("prepared")
        try:
            result = reply_to_wechat(backend, chat, text, on_delivery=receipt.record)
        except Exception as exc:
            state = receipt.state
            if state == "prepared":
                raise
            result = ActionResult(
                ok=state == "confirmed", action="wechat_reply", message=str(exc),
                meta={"delivery_attempted": True, "delivery_status":
                      "confirmed" if state == "confirmed" else "uncertain"},
            )
        if result.meta.get("delivery_attempted") and receipt.state != "confirmed":
            receipt.record("uncertain")
        return _text_response(result)


def _request_approval(
    action: str, args: Dict[str, Any], force_prompt: bool = False,
) -> Optional[str]:
    global _session_auto_approve, _always_allow
    event_policy = get_event_policy()
    if (
        not force_prompt
        and event_policy is not None
        and event_policy.is_auto
        and event_policy.action_allowed(action)
    ):
        return None
    cb = _approval_callback
    if cb is None:
        # Gateway turns run in a worker thread and cannot use the CLI's
        # prompt callback. Reuse Hermes' per-session approval queue so the
        # Telegram/Discord/etc. adapter that owns the session receives the
        # request and /approve or /deny unblocks this exact tool call.
        try:
            from tools import approval as gateway_approval

            if gateway_approval._is_gateway_approval_context():
                session_key = gateway_approval.get_current_session_key("")
                pattern_key = f"phone_use:{action}"
                if (
                    not force_prompt
                    and session_key
                    and gateway_approval.is_approved(session_key, pattern_key)
                ):
                    return None
                with gateway_approval._lock:
                    notify_cb = gateway_approval._gateway_notify_cbs.get(session_key)
                if not session_key or notify_cb is None:
                    return json.dumps({
                        "error": f"'{action}' requires gateway approval, but no "
                        "approval channel is registered for this session.",
                    })
                summary = _summarize_action(action, args)
                approval_data = {
                    "command": f"phone_use {summary}",
                    "description": f"Phone action: {summary}",
                    "pattern_key": pattern_key,
                    "pattern_keys": [pattern_key],
                    "allow_session": not force_prompt,
                    "allow_permanent": not force_prompt,
                }
                decision = gateway_approval._await_gateway_decision(
                    session_key,
                    notify_cb,
                    approval_data,
                    surface="gateway",
                )
                choice = decision.get("choice") if decision.get("resolved") else "deny"
                if decision.get("notify_failed"):
                    choice = "deny"
                if choice in ("once", "session", "always"):
                    if choice == "session" and not force_prompt:
                        gateway_approval.approve_session(session_key, pattern_key)
                    elif choice == "always" and not force_prompt:
                        gateway_approval.approve_session(session_key, pattern_key)
                        gateway_approval.approve_permanent(pattern_key)
                        gateway_approval.save_permanent_allowlist(
                            gateway_approval._permanent_approved
                        )
                    return None
                return json.dumps({"error": "denied by user", "action": action})
        except Exception as e:
            logger.warning("gateway phone approval failed: %s", e)
            return json.dumps({
                "error": f"'{action}' approval could not be completed: {e}",
            })
    if cb is None:
        if force_prompt:
            return json.dumps({
                "error": f"'{action}' requires explicit user approval but no "
                         f"approval callback is registered.",
                "hint": "This action is high-risk and cannot be auto-approved.",
            })
        return None
    if not force_prompt:
        if _session_auto_approve:
            return None
        if action in _always_allow:
            return None
    summary = _summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as e:
        logger.warning("approval callback failed: %s", e)
        verdict = "deny"
    if verdict == "approve_once":
        return None
    if verdict in ("approve_session", "always_approve"):
        if not force_prompt:
            _always_allow.add(action)
            if verdict == "always_approve":
                _session_auto_approve = True
        return None
    return json.dumps({"error": "denied by user", "action": action})


def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    if action in ("tap", "double_tap", "long_press"):
        if args.get("element") is not None:
            return f"{action} element #{args['element']}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}"
        return action
    if action == "swipe":
        d = args.get("direction", "")
        return f"swipe {d}" if d else f"swipe {args.get('from_coordinate')}→{args.get('to_coordinate')}"
    if action == "type":
        text = args.get("text", "")
        return f"type {len(text)} characters"
    if action == "set_text":
        text = args.get("text", "")
        return f"set_text {len(text)} characters"
    if action == "keyevent":
        return f"keyevent {args.get('keycode', '?')}"
    if action == "launch_app":
        return f"launch {args.get('package', '?')}"
    if action == "stop_app":
        return f"stop {args.get('package', '?')}"
    if action == "install_apk":
        return "install APK from local path"
    if action == "shell":
        return "run adb shell command"
    if action == "wechat_open_chat":
        return f"open WeChat chat {args.get('chat', '?')!r}"
    if action == "wechat_reply":
        text = args.get("text", "")
        description = f"reply to WeChat chat {args.get('chat', '?')!r} with: {text}"
        if args.get('quote_text'):
            description += (f"; quoting {args['quote_text']!r}"
                            f"; sender={args.get('quote_sender', '')!r}"
                            f"; context={args.get('quote_context', '')!r}")
        return description
    if action == "begin_workflow":
        return f"approve phone workflow: {args.get('goal', '?')}"
    return action


def _dispatch(backend: PhoneBackend, action: str, args: Dict[str, Any]) -> Any:
    if action == "capture":
        mode = args.get("mode", "som")
        if mode not in ("som", "screenshot", "hierarchy"):
            return json.dumps({"error": f"bad mode {mode!r}"})
        cap = backend.capture(mode=mode)
        return _capture_response(cap)

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _maybe_follow_capture(backend, res, True)

    if action == "device_info":
        info = backend.device_info()
        return json.dumps({
            "serial": info.serial, "model": info.model,
            "android_version": info.android_version,
            "sdk_version": info.sdk_version,
            "screen": f"{info.screen_width}x{info.screen_height}",
            "density": info.density, "is_emulator": info.is_emulator,
        })

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)})

    if action == "current_app":
        return json.dumps(backend.current_app())

    # Touch actions
    if action == "tap":
        element, x, y = _extract_target(args)
        res = backend.tap(element=element, x=x, y=y)
        return _maybe_follow_capture(backend, res, True)

    if action == "double_tap":
        element, x, y = _extract_target(args)
        res = backend.double_tap(element=element, x=x, y=y)
        return _maybe_follow_capture(backend, res, True)

    if action == "long_press":
        element, x, y = _extract_target(args)
        duration = int(args.get("duration_ms", 1000))
        res = backend.long_press(element=element, x=x, y=y, duration_ms=duration)
        return _maybe_follow_capture(backend, res, True)

    if action == "swipe":
        res = backend.swipe(
            direction=args.get("direction"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            duration_ms=int(args.get("duration_ms", 300)),
            element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, True)

    # Text
    if action == "type":
        res = backend.type_text(
            args.get("text", ""), element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, True)

    if action == "clear_text":
        res = backend.clear_text(element=args.get("element"))
        return _maybe_follow_capture(backend, res, True)

    if action == "set_text":
        res = backend.set_text(
            args.get("text", ""), element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, True)

    if action == "keyevent":
        keycode = args.get("keycode")
        if not keycode:
            return json.dumps({"error": "keyevent requires 'keycode'"})
        res = backend.keyevent(keycode)
        return _maybe_follow_capture(backend, res, True)

    # App management
    if action == "launch_app":
        package = args.get("package")
        if not package:
            return json.dumps({"error": "launch_app requires 'package'"})
        res = backend.launch_app(package, activity=args.get("activity"))
        return _maybe_follow_capture(backend, res, True)

    if action == "stop_app":
        package = args.get("package")
        if not package:
            return json.dumps({"error": "stop_app requires 'package'"})
        res = backend.stop_app(package)
        return _maybe_follow_capture(backend, res, True)

    if action == "install_apk":
        apk_path = args.get("apk_path")
        if not apk_path:
            return json.dumps({"error": "install_apk requires 'apk_path'"})
        res = backend.install_apk(apk_path)
        return _maybe_follow_capture(backend, res, True)

    if action == "shell":
        command = args.get("command")
        if not command:
            return json.dumps({"error": "shell requires 'command'"})
        res = backend.shell(command)
        return _maybe_follow_capture(backend, res, True)

    if action == "wechat_open_chat":
        chat = args.get("chat", "")
        res = open_wechat_chat(backend, chat)
        return _maybe_follow_capture(backend, res, False)

    if action == "wechat_reply":
        chat = args.get("chat", "")
        text = args.get("text", "")
        quote_options = {key: args[key] for key in (
            'quote_text', 'quote_sender', 'quote_context', 'quote_max_pages',
        ) if key in args}
        res = reply_to_wechat(backend, chat, text, **quote_options)
        logger.info(
            "wechat_reply outcome: ok=%s chat=%r detail=%s",
            res.ok,
            chat,
            res.message,
        )
        # The composite action captures and verifies every step internally.
        # Returning the final Home hierarchy duplicates thousands of tokens.
        return _text_response(res)

    if action == "wechat_collect_context":
        include_images = args.get("include_images", args.get("open_images", False)) is True
        open_images = include_images and args.get("open_images", False) is True
        res = collect_wechat_context(
            backend,
            args.get("chat", ""),
            scope=args.get("scope", ""),
            max_messages=int(args.get("max_messages", 50)),
            max_pages=int(args.get("max_pages", 8 if args.get("scope") or open_images else 1)),
            max_minutes=int(args.get("max_minutes", 10)),
            include_images=include_images,
            open_images=open_images,
            max_images=int(args.get("max_images", 3)),
            transcribe_voice=args.get("transcribe_voice", False) is True,
            max_voice=int(args.get("max_voice", 3)),
        )
        payload = dict(res.meta)
        screenshots = payload.pop("screenshots", [])
        payload.update({"ok": res.ok, "action": res.action, "message": res.message})
        if screenshots:
            summary = json.dumps(payload, ensure_ascii=False)
            return {
                "_multimodal": True,
                "content": [
                    {"type": "text", "text": summary},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image}"},
                        }
                        for image in screenshots
                    ],
                ],
                "text_summary": summary,
                "meta": {"image_count": len(screenshots)},
            }
        return json.dumps(payload, ensure_ascii=False)

    return json.dumps({"error": f"unknown action: {action!r}"})


# ── Helpers ─────────────────────────────────────────────────────────

def _extract_target(args: Dict[str, Any]):
    element = args.get("element")
    coord = args.get("coordinate")
    x = coord[0] if coord else None
    y = coord[1] if coord else None
    return element, x, y


def _text_response(res: ActionResult) -> str:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    return json.dumps(payload)


_DEFAULT_MAX_ELEMENTS = 100


def _capture_response(cap: CaptureResult) -> Any:
    visible = cap.elements[:_DEFAULT_MAX_ELEMENTS]
    total = len(cap.elements)
    truncated = max(0, total - len(visible))

    if cap.png_b64 and cap.mode != "hierarchy":
        element_index = _format_elements(visible)
        summary_lines = [
            f"phone capture mode={cap.mode} {cap.width}x{cap.height}",
            f"foreground: {cap.current_package}/{cap.current_activity}",
            f"{total} UI element(s):",
        ]
        if element_index:
            summary_lines.extend(element_index)
        if truncated:
            summary_lines.append(
                f"  (showing {len(visible)} of {total} elements)"
            )
        summary = "\n".join(summary_lines)
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {
                "mode": cap.mode,
                "width": cap.width,
                "height": cap.height,
                "elements": total,
                "png_bytes": cap.png_bytes_len,
            },
        }

    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "foreground": f"{cap.current_package}/{cap.current_activity}",
        "elements": [_element_to_dict(e) for e in visible],
        "total_elements": total,
    }
    if truncated:
        payload["truncated_elements"] = truncated
    return json.dumps(payload)


def _maybe_follow_capture(
    backend: PhoneBackend, res: ActionResult, do_capture: bool,
) -> Any:
    cap = res.capture
    if cap is None and not do_capture:
        return _text_response(res)
    if cap is None:
        try:
            cap = backend.capture(mode="hierarchy")
        except Exception as e:
            logger.warning("follow-up capture failed: %s", e)
            return _text_response(res)
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get("_multimodal"):
        prefix = f"[{res.action}] ok={res.ok}" + (f" — {res.message}" if res.message else "")
        resp["content"][0]["text"] = prefix + "\n\n" + resp["content"][0]["text"]
        resp["text_summary"] = prefix + "\n\n" + resp["text_summary"]
        return resp
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {"capture": resp}
    data["action"] = res.action
    data["ok"] = res.ok
    if res.message:
        data["message"] = res.message
    return json.dumps(data)


def _error_response_with_capture(
    backend: PhoneBackend, action: str, error: str,
) -> str:
    payload: Dict[str, Any] = {
        "ok": False,
        "action": action,
        "error": error,
    }
    try:
        captured = _capture_response(backend.capture(mode="hierarchy"))
        if isinstance(captured, str):
            capture_payload = json.loads(captured)
            payload.update(capture_payload)
            payload.update({"ok": False, "action": action, "error": error})
    except Exception as capture_error:
        logger.warning(
            "follow-up capture failed after %s exception: %s",
            action,
            capture_error,
        )
    return json.dumps(payload)


def return_phone_home(task_id: str = "") -> None:
    """Best-effort workflow cleanup used by the phone event adapter."""
    try:
        backend = _get_backend()
        with _device_operation_queue.turn("return_home", task_id):
            res = backend.keyevent("HOME")
            _maybe_follow_capture(backend, res, True)
    except Exception:
        logger.exception("Could not return phone to Home after workflow")


def accept_approved_wechat_friend_request(requester: str) -> str:
    """Execute a host-approved friend request through the shared device FIFO."""
    try:
        backend = _get_backend()
        with _device_operation_queue.turn("wechat_accept_friend"):
            return _text_response(accept_wechat_friend_request(backend, requester))
    except Exception as exc:
        logger.exception("Approved WeChat friend request failed")
        return json.dumps({
            "ok": False,
            "action": "wechat_accept_friend",
            "message": f"accepting friend request failed: {exc}",
        }, ensure_ascii=False)


def _format_elements(elements: List[UIElement], max_lines: int = 40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        flags = []
        if e.clickable:
            flags.append("clickable")
        if e.scrollable:
            flags.append("scrollable")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        out.append(f"  #{e.index} {e.class_name.rsplit('.', 1)[-1]} {label!r} "
                   f"bounds={e.bounds}{flag_str}")
    if len(elements) > max_lines:
        out.append(f"  … +{len(elements) - max_lines} more")
    return out


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "index": e.index,
        "class": e.class_name,
        "bounds": list(e.bounds),
    }
    if e.resource_id:
        payload["resource_id"] = e.resource_id
    if e.text:
        payload["text"] = e.text
    if e.content_desc:
        payload["content_desc"] = e.content_desc
    if e.clickable:
        payload["clickable"] = True
    if not e.enabled:
        payload["enabled"] = False
    return payload


# ── Availability check ─────────────────────────────────────────────

def check_phone_use_requirements() -> bool:
    from .adb_backend import adb_available
    return adb_available()


# ── Noop backend for tests ──────────────────────────────────────────

class _NoopBackend(PhoneBackend):
    def __init__(self):
        self.calls = []
        self._started = False

    def start(self): self._started = True
    def stop(self): self._started = False
    def is_available(self): return True

    def device_info(self):
        from .backend import DeviceInfo
        return DeviceInfo(serial="noop", model="Noop", screen_width=1080,
                          screen_height=2400, is_emulator=True)

    def capture(self, mode="som"):
        self.calls.append(("capture", {"mode": mode}))
        return CaptureResult(mode=mode, width=1080, height=2400)

    def tap(self, **kw):
        self.calls.append(("tap", kw))
        return ActionResult(ok=True, action="tap")

    def double_tap(self, **kw):
        self.calls.append(("double_tap", kw))
        return ActionResult(ok=True, action="double_tap")

    def long_press(self, **kw):
        self.calls.append(("long_press", kw))
        return ActionResult(ok=True, action="long_press")

    def swipe(self, **kw):
        self.calls.append(("swipe", kw))
        return ActionResult(ok=True, action="swipe")

    def type_text(self, text, element=None):
        self.calls.append(("type", {"text": text}))
        return ActionResult(ok=True, action="type")

    def clear_text(self, element=None):
        self.calls.append(("clear_text", {}))
        return ActionResult(ok=True, action="clear_text")

    def set_text(self, text, element=None):
        self.calls.append(("set_text", {"text": text}))
        return ActionResult(ok=True, action="set_text")

    def keyevent(self, keycode):
        self.calls.append(("keyevent", {"keycode": keycode}))
        return ActionResult(ok=True, action="keyevent")

    def launch_app(self, package, activity=None):
        self.calls.append(("launch_app", {"package": package}))
        return ActionResult(ok=True, action="launch_app")

    def stop_app(self, package):
        self.calls.append(("stop_app", {"package": package}))
        return ActionResult(ok=True, action="stop_app")

    def list_apps(self, installed_only=True):
        return []

    def current_app(self):
        return {"package": "com.noop", "activity": ".MainActivity"}

    def install_apk(self, apk_path):
        self.calls.append(("install_apk", {"path": apk_path}))
        return ActionResult(ok=True, action="install_apk")

    def shell(self, command):
        self.calls.append(("shell", {"command": command}))
        return ActionResult(ok=True, action="shell")
