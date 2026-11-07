"""Turn a shell command into its reversible twin, or return ``None`` to leave it alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config, quarantine_expr
from .rules import Edit, apply_rules
from .shellgen import MARKER
from .shellscan import Bail, Simple, scan

__all__ = ["MAX_COMMAND_LEN", "Rewrite", "extract_receipt_id", "rewrite"]

MAX_COMMAND_LEN = 100_000
_HINT = re.compile(r"\b(rm|git)\b")
_SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "fish"}
_OPAQUE = {"eval", "xargs", "source", ".", "parallel"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
_MARKER_ID = re.compile(re.escape(MARKER) + r"([0-9a-f]{6})\b")


@dataclass(frozen=True)
class Rewrite:
    command: str
    kinds: tuple[str, ...]
    original: str


def extract_receipt_id(command: object) -> str | None:
    """The receipt id embedded in an already-rewritten command, if any."""
    if not isinstance(command, str):
        return None
    m = _MARKER_ID.search(command)
    return m.group(1) if m else None


def _opaque(seg: Simple) -> bool:
    if not seg.argv:
        return False
    name = seg.argv[0].val.rsplit("/", 1)[-1]
    args = [t.val for t in seg.argv[1:]]
    if name in _OPAQUE:
        return True
    if name in _SHELLS:
        return any(a.startswith("-") and not a.startswith("--") and "c" in a for a in args)
    return name == "find" and any(a in _FIND_EXEC for a in args)


def rewrite(command: object, cfg: Config, rid: str) -> Rewrite | None:
    """Return the reversible twin of *command*, or ``None`` (run it untouched).

    Never raises for any input: unexpected types, oversized or unparseable commands, and
    already-rewritten commands (carrying :data:`MARKER`) all yield ``None``.
    """
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND_LEN:
        return None
    if MARKER in command or not _HINT.search(command):
        return None
    try:
        segments = scan(command)
        if any(_opaque(s) for s in segments):
            return None
        base = quarantine_expr(cfg.quarantine_dir)
        edits: list[Edit] = []
        for seg in segments:
            slot = rid if not edits else f"{rid}.{len(edits)}"
            edit = apply_rules(command, seg, cfg, base, rid, slot)
            if edit is not None:
                edits.append(edit)
    except Bail:
        return None
    if not edits:
        return None
    out = command
    for e in sorted(edits, key=lambda e: e.start, reverse=True):
        out = out[: e.start] + e.text + out[e.end :]
    kinds = tuple(dict.fromkeys(e.kind for e in edits))
    return Rewrite(out, kinds, command)
