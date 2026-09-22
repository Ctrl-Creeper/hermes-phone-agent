"""Offline smoke check against an installed Hermes (put it on PYTHONPATH).

Uses the real BasePlatformAdapter background-task machinery with an in-memory
transport and noop phone. No model calls, Telegram traffic, or ADB operations.
"""
import asyncio
import os
import tempfile
import sys
import types
from pathlib import Path


async def check():
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
    from gateway.session import SessionSource
    namespace = types.ModuleType("hermes_plugins")
    namespace.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins")]
    sys.modules["hermes_plugins"] = namespace
    from hermes_plugins.phone_events.adapter import _dispatch_with_event_policy
    from hermes_plugins.phone_use import _cleanup_workflow_at_turn_end
    from hermes_plugins.phone_use import tool
    from hermes_plugins.phone_use.policy import PolicyDecision, get_event_policy

    class Transport(BasePlatformAdapter):
        async def connect(self): return True
        async def disconnect(self): pass
        async def send(self, *args, **kwargs): return SendResult(success=True)
        async def get_chat_info(self, chat_id): return {}

    transport = Transport(PlatformConfig(enabled=True, extra={}), Platform.TELEGRAM)
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def handler(event):
        identity = get_event_policy().delivery_identity
        try:
            await asyncio.to_thread(tool.handle_phone_use, {"action": "capture"}, task_id=identity)
            observed.append(identity)
            if identity == "first":
                started.set()
                await release.wait()
        finally:
            await asyncio.to_thread(_cleanup_workflow_at_turn_end, task_id=identity)

    transport.set_message_handler(handler)

    def dispatch(identity):
        message = MessageEvent(text="phone task", message_type=MessageType.TEXT,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="test", user_id="phone",
                                 chat_type="group", role_authorized=True))
        message.internal = True
        return _dispatch_with_event_policy(transport, message,
            PolicyDecision(behavior="auto", instruction_source=True, delivery_identity=identity))

    first = asyncio.create_task(dispatch("first"))
    await asyncio.wait_for(started.wait(), 5)
    second = asyncio.create_task(dispatch("second"))
    await asyncio.sleep(0)
    assert observed == ["first"] and not first.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert observed == ["first", "second"], observed
    assert not tool._device_operation_queue.owns("first")
    assert not tool._device_operation_queue.owns("second")
    print("real Hermes gateway lifecycle: passed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="phone-lifecycle-") as home:
        os.environ["HERMES_HOME"] = home
        os.environ["HERMES_PHONE_BACKEND"] = "noop"
        asyncio.run(check())
