"""Owner-only Telegram command for global Hermes phone admission control."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from . import tool
from .pause import pause_gate

logger = logging.getLogger(__name__)

_PHRASES = {
    "暂停手机操控": "pause", "暂停手机控制": "pause", "暂停手机": "pause",
    "停止手机操控": "pause", "停用手机操控": "pause",
    "恢复手机操控": "resume", "恢复手机控制": "resume", "继续手机操控": "resume",
    "启动手机操控": "resume", "重新启动手机操控": "resume",
    "手机操控状态": "status", "手机控制状态": "status",
}


def _command(text: str) -> Optional[str]:
    normalized = " ".join(str(text or "").strip().split()).rstrip("。！!")
    if normalized in _PHRASES:
        return _PHRASES[normalized]
    slash = re.fullmatch(r"/phone\s+(pause|resume|status)", normalized, re.I)
    return slash.group(1).lower() if slash else None


def _extra(gateway: Any) -> dict:
    platforms = getattr(getattr(gateway, "config", None), "platforms", {}) or {}
    for key, config in platforms.items():
        if getattr(key, "value", key) == "phone_events":
            return getattr(config, "extra", {}) or {}
    return {}


def _is_forwarded(raw: Any) -> bool:
    if raw is None:
        return False
    fields = ("forward_origin", "forward_from", "forward_sender_name",
              "forward_date", "is_automatic_forward")
    if isinstance(raw, dict):
        return any(raw.get(name) for name in fields)
    return any(getattr(raw, name, None) for name in fields)


def _is_owner(event: Any, gateway: Any) -> bool:
    source = getattr(event, "source", None)
    if source is None or getattr(event, "internal", False) or _is_forwarded(
        getattr(event, "raw_message", None)
    ):
        return False
    if getattr(getattr(source, "platform", None), "value", None) != "telegram":
        return False
    extra = _extra(gateway)
    owner, main = str(extra.get("telegram_user_id") or ""), str(extra.get("telegram_chat_id") or "")
    if not owner or not main or (str(getattr(source, "user_id", "")) != owner
                                  or str(getattr(source, "chat_id", "")) != main):
        return False
    thread = str(extra.get("telegram_thread_id") or "")
    if str(getattr(source, "thread_id", None) or "") != thread:
        return False
    if str(getattr(source, "profile", None) or "") != str(extra.get("telegram_profile") or ""):
        return False
    try:
        return bool(gateway._is_user_authorized(source))
    except Exception:
        logger.exception("Cannot authenticate phone control owner")
        return False


def _phone_adapter(gateway: Any):
    for key, adapter in (getattr(gateway, "adapters", {}) or {}).items():
        if getattr(key, "value", key) == "phone_events":
            return adapter
    return None


async def _ack(gateway: Any, source: Any, text: str) -> None:
    adapter = None
    getter = getattr(gateway, "_adapter_for_source", None)
    if callable(getter):
        adapter = getter(source)
    if adapter is None:
        return
    metadata = {"thread_id": str(source.thread_id)} if getattr(source, "thread_id", None) else None
    try:
        await adapter.send(source.chat_id, text, metadata=metadata)
    except Exception:
        logger.exception("Cannot acknowledge phone control to Telegram")


async def _cancel_turns(gateway: Any, source: Any, keys: set[str],
                        phone_sources: dict) -> None:
    for key in keys:
        cached = phone_sources.get(key) or getattr(
            gateway, "_get_cached_session_source", lambda _key: None
        )(key)
        if cached is None:
            cached = source if getattr(gateway, "_session_key_for_source", lambda _s: None)(source) == key else None
        if cached is None:
            logger.warning("Phone turn %s has no gateway source; admission closed", key)
            continue
        try:
            await gateway._interrupt_and_clear_session(
                key, cached,
                interrupt_reason="Phone control paused",
                invalidation_reason="phone_paused",
            )
        except Exception:
            logger.exception("Failed to interrupt phone turn %s", key)


async def _apply(gateway: Any, source: Any, command: str, keys: set[str]) -> None:
    lock = getattr(gateway, "_phone_control_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        gateway._phone_control_lock = lock
    async with lock:
        adapter = _phone_adapter(gateway)
        cleanups = getattr(gateway, "_phone_control_cleanups", None)
        if cleanups is None:
            cleanups = gateway._phone_control_cleanups = set()
        if command == "pause":
            sources = {}
            if adapter is not None:
                sources = getattr(adapter, "abort_phone_dispatches", lambda: {})()
                keys.update(sources)
            await _cancel_turns(gateway, source, keys, sources)
            disconnect_failed = False
            if adapter is not None:
                try:
                    await adapter.suspend_monitors()
                except Exception:
                    disconnect_failed = True
                    logger.exception("Phone monitors failed to disconnect")
            for key in keys:
                cleanup = asyncio.create_task(asyncio.to_thread(tool._finish_turn, key, ""))
                cleanups.add(cleanup)
                def finished(task):
                    cleanups.discard(task)
                    try:
                        task.result()
                    except Exception:
                        logger.exception("Phone pause HOME cleanup failed")
                        gateway._phone_control_cleanup_failed = True
                cleanup.add_done_callback(finished)
            text = ("手机操控已暂停；当前手机任务已请求中断，必要的回桌面清理仍可能在进行。"
                    "已发出的单次操作无法撤回。")
            if disconnect_failed:
                text += " 事件监听断开失败，但新手机操作已被阻止。"
            await _ack(gateway, source, text)
        elif command == "resume":
            if (any(not cleanup.done() for cleanup in cleanups)
                    or tool._device_operation_queue.cleanup_pending()):
                await _ack(gateway, source, "手机操控仍暂停：旧任务正在安全清理，请稍后重试恢复。")
                return
            if (getattr(gateway, "_phone_control_cleanup_failed", False)
                    or tool._device_operation_queue.cleanup_failed()):
                await _ack(gateway, source, "手机操控仍暂停：回桌面清理失败，请检查设备状态后重试。")
                return
            if adapter is None:
                await _ack(gateway, source, "手机操控仍暂停：手机事件插件尚未连接。")
                return
            try:
                await adapter.resume_monitors()
            except Exception as exc:
                logger.exception("Phone monitors failed to reconnect")
                await _ack(gateway, source, f"手机操控仍暂停：监听重连失败（{type(exc).__name__}）。")
                return
            tool.resume_phone_operations()
            await _ack(gateway, source, "手机操控已恢复；暂停期间的消息和旧任务不会补跑。")
        else:
            paused, _ = pause_gate.snapshot()
            status = "已暂停" if paused else "运行中"
            running = sum(not cleanup.done() for cleanup in cleanups)
            suffix = f"，还有 {running} 项清理中" if running else ""
            if (getattr(gateway, "_phone_control_cleanup_failed", False)
                    or tool._device_operation_queue.cleanup_failed()):
                suffix += "，上次清理失败"
            await _ack(gateway, source, f"手机操控状态：{status}{suffix}。")


def pre_gateway_dispatch(*, event: Any, gateway: Any, **_kwargs) -> Optional[dict]:
    """Handle only authenticated owner commands before the busy-turn path."""
    command = _command(getattr(event, "text", ""))
    if command is None or not _is_owner(event, gateway):
        return None
    try:
        keys = tool.pause_phone_operations() if command == "pause" else set()
    except Exception:
        logger.exception("Could not persist phone pause")
        return {"action": "skip", "reason": "phone-control-error"}
    asyncio.get_running_loop().create_task(_apply(gateway, event.source, command, keys))
    return {"action": "skip", "reason": "phone-control"}
