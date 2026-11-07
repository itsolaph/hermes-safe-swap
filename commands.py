"""``/swaps``, ``/unswap`` and ``hermes safe-swap``: list, undo and purge receipts.

All shell work is a generated POSIX script (see :mod:`undo`) handed to a *runner*. The slash
commands use the session's own terminal backend through ``ctx.dispatch_tool``, so a receipt
from a docker or ssh sandbox is restored where its files are; the CLI runs scripts locally.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from typing import Any, Callable

from .config import quarantine_expr
from .receipts import Receipt, Store
from .undo import purge_script, restore_script

__all__ = ["Service", "cli_handler", "cli_setup", "dispatch_runner", "local_runner"]

Runner = Callable[[str, str], "tuple[int, str]"]  # (script, task_id) -> (exit code, output)
_ID = re.compile(r"^[0-9a-f]{6}$")
_AGE = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


def local_runner(script: str, task_id: str = "") -> tuple[int, str]:
    p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout + p.stderr).strip()


def dispatch_runner(dispatch_tool: Callable[..., str]) -> Runner:
    """Run scripts through the Hermes terminal tool (same backend as the agent's commands)."""

    def run(script: str, task_id: str = "") -> tuple[int, str]:
        kwargs = {"task_id": task_id} if task_id else {}
        raw = dispatch_tool("terminal", {"command": script, "timeout": 120}, **kwargs)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return 1, str(raw)
        if not isinstance(data, dict):
            return 1, str(raw)
        code = data.get("exit_code")
        text = str(data.get("output") or data.get("error") or "").strip()
        if data.get("approval_pending") or data.get("status") in ("pending_approval", "blocked") or (
            not isinstance(code, int) and not text
        ):
            reason = data.get("description") or data.get("message") or data.get("error") or "no reason given"
            return 1, (
                f"Hermes' approval gate did not run the script ({reason}). Approve it when asked, "
                "or run the same action with `hermes safe-swap` in a terminal."
            )
        return (code if isinstance(code, int) else 1), text

    return run


def _age(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


class Service:
    def __init__(self, store: Callable[[], Store], runner: Runner) -> None:
        self._store, self._run = store, runner

    # ------------------------------------------------------------------ /swaps

    def swaps(self, raw_args: str) -> str:
        words = raw_args.split()
        if words[:1] == ["purge"]:
            return self.purge(words[1:])
        if words:
            return "Usage: /swaps | /swaps purge [--older 7d]"
        receipts = self._store().recent(10)
        if not receipts:
            return "No swaps yet. Destructive shell commands are swapped for reversible twins as they run."
        now = time.time()
        rows = [f"{'id':<7} {'age':<5} {'status':<8} {'kind':<9} command"]
        for r in receipts:
            cmd = " ".join(r.original.split())
            rows.append(f"{r.id:<7} {_age(now - r.ts):<5} {r.status:<8} {r.kind:<9} {cmd[:60]}{'…' if len(cmd) > 60 else ''}")
        rows.append("Undo with /unswap <id> (or /unswap last). /swaps purge [--older 7d] frees quarantine space.")
        return "\n".join(rows)

    # ------------------------------------------------------------------ /unswap

    def unswap(self, raw_args: str) -> str:
        words = raw_args.split()
        force = "--force" in words
        ids = [w for w in words if w != "--force"]
        if len(ids) != 1:
            return "Usage: /unswap <id|last> [--force]"
        store = self._store()
        receipt = store.last_undoable() if ids[0] == "last" else (store.get(ids[0]) if _ID.match(ids[0]) else None)
        if receipt is None:
            return f"No receipt {ids[0]!r}. See /swaps."
        if receipt.status == "purged":
            return f"Receipt {receipt.id}: the quarantine was purged, nothing to restore."
        if receipt.status == "undone" and not force:
            return f"Receipt {receipt.id} was already undone (use --force to try again)."
        if receipt.kind == "push":
            return (
                f"Receipt {receipt.id}: `{receipt.original}` ran as a lease-protected push. A push cannot be "
                "undone from here: if the lease held, the remote moved; if it did not, the push was refused and nothing changed."
            )
        code, out = self._run(restore_script(quarantine_expr(receipt.qdir), receipt.id, force=force, kinds=receipt.kinds), receipt.task_id)
        if code == 0:
            store.set_status(receipt.id, "undone")
        extra = "\nNote: the force push in the same command cannot be undone." if "push" in receipt.kinds else ""
        verdict = f"Undone {receipt.id}." if code == 0 else f"Receipt {receipt.id} was NOT fully restored (exit {code})."
        return f"{verdict}\n{out}{extra}".strip()

    # ------------------------------------------------------------------ purge

    def purge(self, args: list[str]) -> str:
        seconds = 0.0
        if args:
            m = _AGE.match(args[1]) if len(args) == 2 and args[0] == "--older" else None
            if not m:
                return "Usage: /swaps purge [--older 7d|12h|30m]"
            seconds = int(m.group(1)) * _UNITS[m.group(2)]
        store = self._store()
        victims = [r for r in store.older_than(seconds) if r.status != "pending"]
        groups: dict[tuple[str, str], list[Receipt]] = {}
        for r in victims:
            groups.setdefault((r.qdir, r.task_id), []).append(r)
        failed = 0
        for (qdir, task_id), items in groups.items():
            code, _ = self._run(purge_script(quarantine_expr(qdir), [r.id for r in items]), task_id)
            if code == 0:
                for r in items:
                    store.set_status(r.id, "purged")
            else:
                failed += len(items)
        done = len(victims) - failed
        return f"Purged {done} receipt(s)." + (f" {failed} could not be purged." if failed else "")


# ---------------------------------------------------------------------- CLI


def cli_setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="safe_swap_action")
    sub.add_parser("list", help="show recent receipts")
    undo = sub.add_parser("undo", help="restore a receipt")
    undo.add_argument("id", help="receipt id or 'last'")
    undo.add_argument("--force", action="store_true", help="move an existing file aside instead of refusing")
    purge = sub.add_parser("purge", help="delete quarantined data (the only deletion Safe-Swap ever does)")
    purge.add_argument("--older", default="", help="only receipts older than this, e.g. 7d, 12h, 30m")


def cli_handler(service: Service, args: Any) -> int:
    action = getattr(args, "safe_swap_action", None) or "list"
    if action == "list":
        print(service.swaps(""))
    elif action == "undo":
        print(service.unswap(f"{args.id} {'--force' if args.force else ''}"))
    elif action == "purge":
        print(service.purge(["--older", args.older] if args.older else []))
    return 0
