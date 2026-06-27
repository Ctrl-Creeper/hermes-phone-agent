"""Phone control tool plugin for Hermes Agent.

Registers the `phone_use` tool into the `phone_use` toolset.
Install: hermes plugins install <repo>/plugins/phone_use
"""

from .schema import PHONE_USE_SCHEMA
from .tool import (
    check_phone_use_requirements,
    handle_phone_use,
    set_approval_callback,
)


def register(ctx) -> None:
    ctx.register_tool(
        name="phone_use",
        toolset="phone_use",
        schema=PHONE_USE_SCHEMA,
        handler=handle_phone_use,
        check_fn=check_phone_use_requirements,
        emoji="📱",
    )
