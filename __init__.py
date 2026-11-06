"""Safe-Swap: swap destructive shell commands for reversible twins (stub, filled in below)."""


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", lambda **_: None)
    ctx.register_hook("post_tool_call", lambda **_: None)
    ctx.register_hook("transform_tool_result", lambda **_: None)
