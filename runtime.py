"""The three hooks. Every entry point swallows its own exceptions: a ``pre_tool_call`` that raises
or times out would BLOCK the user's command, so on any doubt these return ``None`` (run untouched)."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config, load_config
from .receipts import Receipt, Store, new_id
from .rewriter import Rewrite, extract_receipt_id, rewrite

__all__ = ["Runtime", "describe", "plugin_db_path"]

logger = logging.getLogger(__name__)
_CONFIG_TTL = 5.0
_PLUGIN_NAME = "safe-swap"
_WHAT = {
    "rm": "move-to-quarantine",
    "push": "git push --force-with-lease --force-if-includes (not --force)",
    "reset": "preceded by a stash snapshot",
    "checkout": "preceded by a stash snapshot",
    "restore": "preceded by a stash snapshot",
    "branch": "preceded by a record of the branch tip",
}


def plugin_db_path() -> Path:
    """``<hermes home>/plugin-data/safe-swap/receipts.db`` (the sanctioned per-plugin data dir)."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(_PLUGIN_NAME) / "receipts.db"


def describe(receipt: Receipt, *, limit: int = 100) -> str:
    """One-line, honest account of what really ran for *receipt*."""
    original = " ".join(receipt.original.split()).replace("`", "'")
    if len(original) > limit:
        original = original[: limit - 1] + "…"
    what = "; ".join(dict.fromkeys(_WHAT.get(k, k) for k in receipt.kinds))
    undo = "Not undoable (the lease only refuses unsafe pushes)." if receipt.kind == "push" else f"Undo: /unswap {receipt.id}"
    return f"[safe-swap] `{original}` was run as: {what} (receipt {receipt.id}). {undo}"


class Runtime:
    def __init__(self, get_config: Callable[[str, Any], Any], db_path: Callable[[], Path] = plugin_db_path) -> None:
        self._get_config = get_config
        self._db_path = db_path
        self._cfg: tuple[float, Config] | None = None

    def config(self) -> Config:
        now = time.monotonic()
        if self._cfg is None or now - self._cfg[0] > _CONFIG_TTL:
            self._cfg = (now, load_config(self._get_config))
        return self._cfg[1]

    def store(self) -> Store:
        return Store(self._db_path())

    # ------------------------------------------------------------------ hooks

    def pre_tool_call(self, tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "",
                      tool_call_id: str = "", **_: Any) -> dict[str, Any] | None:
        try:
            if tool_name != "terminal" or not isinstance(args, dict):
                return None
            command = args.get("command")
            cfg = self.config()
            rid = new_id()
            swapped = rewrite(command, cfg, rid)
            if swapped is None:
                return None
            store = self.store()
            for _attempt in range(4):
                try:
                    store.insert_pending(_receipt(rid, swapped, cfg, args, task_id, session_id, tool_call_id))
                    break
                except sqlite3.IntegrityError:
                    rid = new_id()
                    swapped = rewrite(command, cfg, rid)
                    if swapped is None:
                        return None
            else:
                return None
            return {"action": "modify", "args": {"command": swapped.command}}
        except Exception:  # noqa: BLE001 - fail open, always
            logger.debug("safe-swap pre_tool_call failed open", exc_info=True)
            return None

    def post_tool_call(self, tool_name: str = "", args: Any = None, result: Any = None, tool_call_id: str = "",
                       status: Any = None, **_: Any) -> None:
        try:
            if tool_name != "terminal":
                return
            receipt = self._receipt_for(args, tool_call_id)
            if receipt is None:
                return
            exit_code = _exit_code(result)
            failed = status == "blocked" or (exit_code not in (None, 0))
            self.store().settle(receipt.id, "failed" if failed else "applied", exit_code)
        except Exception:  # noqa: BLE001
            logger.debug("safe-swap post_tool_call failed", exc_info=True)

    def transform_tool_result(self, tool_name: str = "", args: Any = None, result: Any = None,
                              tool_call_id: str = "", **_: Any) -> str | None:
        try:
            if tool_name != "terminal" or not isinstance(result, str) or not self.config().note_to_agent:
                return None
            receipt = self._receipt_for(args, tool_call_id)
            if receipt is None:
                return None
            return _append_note(result, describe(receipt))
        except Exception:  # noqa: BLE001
            logger.debug("safe-swap transform_tool_result failed", exc_info=True)
            return None

    def _receipt_for(self, args: Any, tool_call_id: str) -> Receipt | None:
        """The receipt of this call: by the id embedded in the command, else by ``tool_call_id``."""
        store = self.store()
        rid = extract_receipt_id(args.get("command")) if isinstance(args, dict) else None
        receipt = store.get(rid) if rid else None
        return receipt or store.by_call(tool_call_id)


def _receipt(rid: str, swapped: Rewrite, cfg: Config, args: dict, task_id: str, session_id: str, call_id: str) -> Receipt:
    workdir = args.get("workdir")
    return Receipt(
        id=rid, ts=time.time(), session_id=str(session_id or ""), task_id=str(task_id or ""),
        tool_call_id=str(call_id or ""), kind=",".join(swapped.kinds), original=swapped.original,
        rewritten=swapped.command, workdir=workdir if isinstance(workdir, str) else "",
        qdir=cfg.quarantine_dir, status="pending", exit_code=None,
    )


def _exit_code(result: Any) -> int | None:
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return None
    code = data.get("exit_code") if isinstance(data, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _append_note(result: str, note: str) -> str:
    try:
        data = json.loads(result)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("output"), str):
        out = data["output"]
        data["output"] = f"{out}{'' if not out or out.endswith(chr(10)) else chr(10)}{note}"
        return json.dumps(data, ensure_ascii=False)
    return f"{result}\n{note}"
