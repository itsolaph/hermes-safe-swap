"""Safe-Swap: silently swap destructive shell commands for their reversible twins.

``pre_tool_call`` rewrites the ``terminal`` command and stores a receipt; ``post_tool_call``
settles the receipt; ``transform_tool_result`` tells the agent what really ran. ``/swaps``,
``/unswap`` and ``hermes safe-swap`` list, undo and purge receipts. Nothing is patched,
nothing is blocked, and Hermes' approval gate still sees the rewritten command.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    # Imported here so that merely importing this file (e.g. test collection) needs no package context.
    from .commands import Service, cli_handler, cli_setup, dispatch_runner, local_runner
    from .runtime import Runtime

    runtime = Runtime(ctx.get_config)
    ctx.register_hook("pre_tool_call", runtime.pre_tool_call)
    ctx.register_hook("post_tool_call", runtime.post_tool_call)
    ctx.register_hook("transform_tool_result", runtime.transform_tool_result)

    chat = Service(runtime.store, dispatch_runner(ctx.dispatch_tool))
    ctx.register_command("swaps", lambda raw_args="": chat.swaps(raw_args),
                         description="List Safe-Swap receipts; '/swaps purge [--older 7d]' frees quarantine space",
                         args_hint="[purge [--older 7d]]")
    ctx.register_command("unswap", lambda raw_args="": chat.unswap(raw_args),
                         description="Undo a swapped command by receipt id (or 'last')", args_hint="<id|last> [--force]")

    local = Service(runtime.store, local_runner)
    ctx.register_cli_command("safe-swap", "List, undo and purge Safe-Swap receipts", cli_setup,
                             lambda args: cli_handler(local, args),
                             description="Safe-Swap receipts for the local terminal backend")
