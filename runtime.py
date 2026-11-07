"""The three hooks. Every entry point swallows its own exceptions: a ``pre_tool_call`` that raises
or times out would BLOCK the user's command, so on any doubt these return ``None`` (run untouched)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Config, load_config
from .receipts import Receipt, Store, new_id
from .rewriter import Rewrite, analyze, extract_receipt_id, rewrite
from .shellgen import NOT_SWAPPED, STATS

__all__ = ["BlastRadius", "Runtime", "describe", "human_bytes", "not_swapped_line", "plugin_db_path", "receipt_size"]

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
    "clean": "preceded by a snapshot of the untracked files",
    "stash": "preceded by a record of the dropped stash entries",
}
_STATS_LINE = re.compile(r"^" + STATS + r" ([0-9a-f]{6}) (\d+) (\d+) ([01]) ([01]) ([01])[ \t]*\r?(?:\n|$)", re.M)
_RUNTIME_FAILURE = re.compile(r"^" + re.escape(NOT_SWAPPED) + r"([^)\n]{1,200})\)[ \t]*(?=\r?$)", re.M)
_ORIGINAL_CHARS = 80


@dataclass(frozen=True)
class BlastRadius:
    files: int
    bytes: int
    capped: bool = False  # the count stopped at its cap: both numbers are lower bounds
    copied: bool = False  # the move degraded to copy + delete (another filesystem)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if unit == "B":
            if size < 1024:
                return f"{int(size)} B"
        elif round(size) < 1024 or unit == "GB":  # 1023.6 KB rounds up to 1024: show it as MB instead
            return f"{round(size)} {unit}" if size >= 9.95 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _blast_text(b: BlastRadius) -> str:
    if b.capped:  # the walk stopped early: the count is a lower bound and the bytes are not reported
        return f"{b.files}+ files"
    return f"{b.files} file{'' if b.files == 1 else 's'}, {human_bytes(b.bytes)}"


def receipt_size(receipt: Receipt) -> str:
    """The blast radius recorded for *receipt* (``312 files, 14 MB``), or ``-`` when it was not measured."""
    if receipt.files is None:
        return "-"
    text = _blast_text(BlastRadius(receipt.files, receipt.bytes or 0, bool(receipt.capped)))
    return f"{text}, copied" if receipt.copied else text


def not_swapped_line(reason: str, original: str) -> str:
    """The agent-facing line for a destructive-looking command that ran untouched."""
    return f"{NOT_SWAPPED}{reason}): {' '.join(original.split())[:_ORIGINAL_CHARS]}"


def _take_stats(text: str, rid: str) -> tuple[str, BlastRadius | None]:
    """Remove every stats line from *text*; sum the ones for receipt *rid* (one per rewritten rm segment)."""
    files = size = 0
    capped = copied = found = False
    for m in _STATS_LINE.finditer(text):
        if m.group(1) != rid:
            continue
        found = True
        files, size = files + int(m.group(2)), size + int(m.group(3))
        capped |= m.group(4) == "1" or m.group(5) == "0"  # capped by entries, or killed by the time limit
        copied |= m.group(6) == "1"
    return _STATS_LINE.sub("", text), (BlastRadius(files, size, capped, copied) if found else None)


def _split(result: str) -> tuple[dict | None, str]:
    try:
        data = json.loads(result)
    except ValueError:
        return None, result
    if isinstance(data, dict) and isinstance(data.get("output"), str):
        return data, data["output"]
    return None, result


def _join(data: dict | None, text: str) -> str:
    if data is None:
        return text
    data["output"] = text
    return json.dumps(data, ensure_ascii=False)


def plugin_db_path() -> Path:
    """``<hermes home>/plugin-data/safe-swap/receipts.db`` (the sanctioned per-plugin data dir)."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(_PLUGIN_NAME) / "receipts.db"


def describe(receipt: Receipt, *, limit: int = 100, blast: BlastRadius | None = None) -> str:
    """One-line, honest account of what really ran for *receipt* (with its blast radius for an rm swap)."""
    original = " ".join(receipt.original.split()).replace("`", "'")
    if len(original) > limit:
        original = original[: limit - 1] + "…"
    what = "; ".join(dict.fromkeys(_WHAT.get(k, k) for k in receipt.kinds))
    undo = "Not undoable (the lease only refuses unsafe pushes)." if receipt.kind == "push" else f"Undo: /unswap {receipt.id}"
    if blast is None and receipt.files is not None:
        blast = BlastRadius(receipt.files, receipt.bytes or 0, bool(receipt.capped), bool(receipt.copied))
    parts = [_blast_text(blast)] if blast else []
    if blast and blast.copied:
        parts.append("copied across filesystems")
    parts.append(f"receipt {receipt.id}")
    return f"[safe-swap] `{original}` was run as: {what} ({', '.join(parts)}). {undo}"


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
            failed = status in ("blocked", "cancelled") or (exit_code not in (None, 0))
            store = self.store()
            store.settle(receipt.id, "failed" if failed else "applied", exit_code)
            if isinstance(result, str) and "rm" in receipt.kinds:
                blast = _take_stats(_split(result)[1], receipt.id)[1]
                if blast is not None:
                    store.set_stats(receipt.id, blast.files, blast.bytes, blast.capped, blast.copied)
        except Exception:  # noqa: BLE001
            logger.debug("safe-swap post_tool_call failed", exc_info=True)

    def transform_tool_result(self, tool_name: str = "", args: Any = None, result: Any = None,
                              tool_call_id: str = "", **_: Any) -> str | None:
        """Strip safe-swap's own marker lines and append what the agent must know.

        Three kinds of lines can be appended: what really ran (a swap happened), a rewrite of safe-swap's
        own runtime failure lines (a snapshot or quarantine could not be made) and, for a command that
        looks destructive but was left untouched, why. ``note_to_agent`` switches off only the first;
        ``notify_unswapped`` switches off the other two.
        """
        try:
            if tool_name != "terminal" or not isinstance(result, str):
                return None
            cfg = self.config()
            command = args.get("command") if isinstance(args, dict) else None
            try:
                receipt = self._receipt_for(args, tool_call_id)
            except Exception:  # noqa: BLE001 - a broken store must not hide the notice
                receipt = None
            original = receipt.original if receipt else (command if isinstance(command, str) else "")
            data, text = _split(result)
            text, blast = _take_stats(text, receipt.id if receipt else "")
            failures = _RUNTIME_FAILURE.findall(text) if receipt is not None else []  # only our own rewritten commands
            if failures:  # safe-swap's own line -> the full notice (or nothing when notices are off)
                repl = (lambda m: not_swapped_line(m.group(1), original)) if cfg.notify_unswapped else ""
                text = _RUNTIME_FAILURE.sub(repl, text)
                if not cfg.notify_unswapped:
                    text = re.sub(r"\n{3,}", "\n\n", text)
            notes: list[str] = []
            # A failed snapshot/quarantine means the swap did not protect anything: never claim it did.
            if receipt is not None and cfg.note_to_agent and not failures:
                notes.append(describe(receipt, blast=blast))
            if cfg.notify_unswapped and original:
                analysis = analyze(original, cfg, "000000")
                if receipt is None and analysis.rewrite is not None:
                    notes.append(not_swapped_line("could not record a receipt, ran as written", original))
                elif analysis.skips:
                    notes.append(not_swapped_line(analysis.skips[0], original))
            if not notes and text == _split(result)[1]:
                return None
            for note in notes:
                text = f"{text}{'' if not text or text.endswith(chr(10)) else chr(10)}{note}"
            return _join(data, text)
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
