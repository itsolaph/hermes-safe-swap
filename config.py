"""Plugin settings (``plugins.entries.safe-swap.settings``) with safe defaults."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["DEFAULT_QUARANTINE", "Config", "load_config", "quarantine_expr"]

DEFAULT_QUARANTINE = "~/.safe-swap/trash"
_SAFE_DIR = re.compile(r"^(~|/)[A-Za-z0-9._/+@-]*$")


@dataclass(frozen=True)
class Config:
    rule_rm: bool = True
    swap_single_rm: bool = True
    rule_git_push: bool = True
    rule_git_reset: bool = True
    rule_git_restore: bool = True
    rule_git_branch: bool = True
    trusted_force_branches: tuple[str, ...] = ()
    quarantine_dir: str = DEFAULT_QUARANTINE
    note_to_agent: bool = True


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def load_config(get: Callable[[str, Any], Any]) -> Config:
    """Build a :class:`Config` from a ``get(key, default)`` accessor; bad values fall back."""
    d = Config()
    trusted = get("trusted_force_branches", ())
    qdir = get("quarantine_dir", d.quarantine_dir)
    if not (isinstance(qdir, str) and _SAFE_DIR.match(qdir) and ".." not in qdir.split("/")):
        qdir = d.quarantine_dir
    return Config(
        rule_rm=_bool(get("rule_rm", d.rule_rm), d.rule_rm),
        swap_single_rm=_bool(get("swap_single_rm", d.swap_single_rm), d.swap_single_rm),
        rule_git_push=_bool(get("rule_git_push", d.rule_git_push), d.rule_git_push),
        rule_git_reset=_bool(get("rule_git_reset", d.rule_git_reset), d.rule_git_reset),
        rule_git_restore=_bool(get("rule_git_restore", d.rule_git_restore), d.rule_git_restore),
        rule_git_branch=_bool(get("rule_git_branch", d.rule_git_branch), d.rule_git_branch),
        trusted_force_branches=tuple(x for x in trusted if isinstance(x, str)) if isinstance(trusted, (list, tuple)) else (),
        quarantine_dir=qdir.rstrip("/") or DEFAULT_QUARANTINE,
        note_to_agent=_bool(get("note_to_agent", d.note_to_agent), d.note_to_agent),
    )


def quarantine_expr(qdir: str) -> str:
    """The quarantine root as a double-quoted POSIX shell word (``~`` becomes ``${HOME:-/tmp}``).

    *qdir* is validated by :func:`load_config` to contain only characters that are inert
    inside double quotes, so no escaping is needed.
    """
    if not _SAFE_DIR.match(qdir):
        qdir = DEFAULT_QUARANTINE
    if qdir.startswith("~"):
        qdir = "${HOME:-/tmp}" + qdir[1:]
    return f'"{qdir}"'
