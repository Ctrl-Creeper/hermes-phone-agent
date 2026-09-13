"""Phone control tool plugin for Hermes Agent.

Registers the `phone_use` tool into the `phone_use` toolset.
Install: hermes plugins install <repo>/plugins/phone_use
"""

from .schema import PHONE_USE_SCHEMA
from .tool import (
    _finish_turn,
    check_phone_use_requirements,
    handle_phone_use,
    set_approval_callback,
)
from .policy import get_event_policy


_PHONE_TASK_TOOLS = frozenset({"phone_use", "web_search", "web_extract"})


def _guard_phone_task_tools(tool_name: str, **kwargs):
    decision = get_event_policy()
    if not (
        decision is not None
        and decision.is_auto
        and decision.instruction_source
    ):
        return None
    if tool_name in _PHONE_TASK_TOOLS:
        return None
    return {
        "action": "block",
        "message": (
            f"Tool {tool_name!r} is blocked in an authenticated phone task; "
            "only phone_use, web_search, and web_extract are allowed"
        ),
    }


def _cleanup_workflow_at_turn_end(
    task_id: str = "", session_id: str = "", **kwargs,
) -> None:
    _finish_turn(task_id, session_id)


def register(ctx) -> None:
    ctx.register_tool(
        name="phone_use",
        toolset="phone_use",
        schema=PHONE_USE_SCHEMA,
        handler=handle_phone_use,
        check_fn=check_phone_use_requirements,
        emoji="📱",
    )
    ctx.register_hook("on_session_end", _cleanup_workflow_at_turn_end)
    ctx.register_hook("pre_tool_call", _guard_phone_task_tools)
