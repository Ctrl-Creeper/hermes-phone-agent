"""Entry point for the phone_use tool — dispatch, approval, response shaping.

Mirrors hermes's tools/computer_use/tool.py pattern.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from .backend import (
    ActionResult,
    CaptureResult,
    PhoneBackend,
    UIElement,
)
from .policy import get_policy

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

_APPROVAL_REQUIRED = frozenset({
    "tap", "double_tap", "long_press", "swipe",
    "type", "clear_text", "set_text", "keyevent",
    "launch_app", "stop_app",
})

# These always prompt, even if session-auto-approve is on.
_ALWAYS_PROMPT = frozenset({
    "install_apk", "shell",
})

_session_auto_approve = False
_always_allow: set = set()

# ── Backend ─────────────────────────────────────────────────────────

_backend_lock = threading.Lock()
_backend: Optional[PhoneBackend] = None


def _get_backend() -> PhoneBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            backend_name = os.environ.get(
                "HERMES_PHONE_BACKEND", "adb"
            ).lower()
            if backend_name == "adb":
                from .adb_backend import AdbBackend
                serial = os.environ.get("ANDROID_SERIAL")
                _backend = AdbBackend(serial=serial)
            elif backend_name in ("hybrid", "appium"):
                from .appium_backend import HybridBackend
                serial = os.environ.get("ANDROID_SERIAL")
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
    global _backend, _session_auto_approve, _always_allow
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = None
    _session_auto_approve = False
    _always_allow = set()


# ── Dispatch ────────────────────────────────────────────────────────

def handle_phone_use(args: Dict[str, Any], **kwargs) -> Any:
    """Main entry point — dispatched by hermes tools.registry."""
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing 'action'"})

    # Approval gate.
    if action in _ALWAYS_PROMPT:
        err = _request_approval(action, args, force_prompt=True)
        if err is not None:
            return err
    elif action in _APPROVAL_REQUIRED:
        err = _request_approval(action, args)
        if err is not None:
            return err

    try:
        backend = _get_backend()
    except Exception as e:
        return json.dumps({
            "error": f"phone backend unavailable: {e}",
            "hint": "Ensure adb is on PATH and an emulator is running.",
        })

    # Policy enforcement: check if the action is allowed for the target package.
    # For launch_app/stop_app, the explicit 'package' arg is the target.
    # For all other non-safe actions, use the foreground app — ignore any
    # agent-supplied 'package' to prevent policy bypass.
    target_pkg = ""
    if action in _PACKAGE_AWARE_ACTIONS:
        target_pkg = args.get("package", "")
    elif action not in _SAFE_ACTIONS:
        try:
            fg = backend.current_app()
            target_pkg = fg.get("package", "")
        except Exception:
            pass
    if target_pkg:
        policy = get_policy()
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
        return _dispatch(backend, action, args)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("phone_use %s failed", action)
        return json.dumps({"error": f"{action} failed: {e}"})


def _request_approval(
    action: str, args: Dict[str, Any], force_prompt: bool = False,
) -> Optional[str]:
    global _session_auto_approve, _always_allow
    if not force_prompt:
        if _session_auto_approve:
            return None
        if action in _always_allow:
            return None
    cb = _approval_callback
    if cb is None:
        if force_prompt:
            return json.dumps({
                "error": f"'{action}' requires explicit user approval but no "
                         f"approval callback is registered.",
                "hint": "This action is high-risk and cannot be auto-approved.",
            })
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
        return f"type {text[:40]!r}" + ("…" if len(text) > 40 else "")
    if action == "set_text":
        text = args.get("text", "")
        return f"set_text {text[:40]!r}" + ("…" if len(text) > 40 else "")
    if action == "keyevent":
        return f"keyevent {args.get('keycode', '?')}"
    if action == "launch_app":
        return f"launch {args.get('package', '?')}"
    if action == "stop_app":
        return f"stop {args.get('package', '?')}"
    if action == "install_apk":
        return f"install APK: {args.get('apk_path', '?')}"
    if action == "shell":
        cmd = args.get("command", "")
        return f"adb shell: {cmd[:60]!r}"
    return action


def _dispatch(backend: PhoneBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = args.get("mode", "som")
        if mode not in ("som", "screenshot", "hierarchy"):
            return json.dumps({"error": f"bad mode {mode!r}"})
        cap = backend.capture(mode=mode)
        return _capture_response(cap)

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _text_response(res)

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
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "double_tap":
        element, x, y = _extract_target(args)
        res = backend.double_tap(element=element, x=x, y=y)
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "long_press":
        element, x, y = _extract_target(args)
        duration = int(args.get("duration_ms", 1000))
        res = backend.long_press(element=element, x=x, y=y, duration_ms=duration)
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "swipe":
        res = backend.swipe(
            direction=args.get("direction"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            duration_ms=int(args.get("duration_ms", 300)),
            element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    # Text
    if action == "type":
        res = backend.type_text(
            args.get("text", ""), element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "clear_text":
        res = backend.clear_text(element=args.get("element"))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "set_text":
        res = backend.set_text(
            args.get("text", ""), element=args.get("element"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "keyevent":
        keycode = args.get("keycode")
        if not keycode:
            return json.dumps({"error": "keyevent requires 'keycode'"})
        res = backend.keyevent(keycode)
        return _maybe_follow_capture(backend, res, capture_after)

    # App management
    if action == "launch_app":
        package = args.get("package")
        if not package:
            return json.dumps({"error": "launch_app requires 'package'"})
        res = backend.launch_app(package, activity=args.get("activity"))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "stop_app":
        package = args.get("package")
        if not package:
            return json.dumps({"error": "stop_app requires 'package'"})
        res = backend.stop_app(package)
        return _text_response(res)

    if action == "install_apk":
        apk_path = args.get("apk_path")
        if not apk_path:
            return json.dumps({"error": "install_apk requires 'apk_path'"})
        res = backend.install_apk(apk_path)
        return _text_response(res)

    if action == "shell":
        command = args.get("command")
        if not command:
            return json.dumps({"error": "shell requires 'command'"})
        res = backend.shell(command)
        return _text_response(res)

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
    return json.dumps(payload)


_DEFAULT_MAX_ELEMENTS = 100


def _capture_response(cap: CaptureResult) -> Any:
    visible = cap.elements[:_DEFAULT_MAX_ELEMENTS]
    total = len(cap.elements)
    truncated = max(0, total - len(visible))

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

    if cap.png_b64 and cap.mode != "hierarchy":
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
        "summary": summary,
    }
    if truncated:
        payload["truncated_elements"] = truncated
    return json.dumps(payload)


def _maybe_follow_capture(
    backend: PhoneBackend, res: ActionResult, do_capture: bool,
) -> Any:
    if not do_capture or not res.ok:
        return _text_response(res)
    try:
        cap = backend.capture(mode="som")
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
    return {
        "index": e.index,
        "class": e.class_name,
        "resource_id": e.resource_id,
        "text": e.text,
        "content_desc": e.content_desc,
        "bounds": list(e.bounds),
        "clickable": e.clickable,
        "enabled": e.enabled,
    }


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
