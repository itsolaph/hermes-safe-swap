"""Does a command *look* destructive? Used only to decide when the agent is told "NOT swapped".

Two entry points, both pure and conservative (a ``False`` never changes what runs):

* :func:`argv_destructive` classifies one already-tokenised simple command.
* :func:`text_destructive` is the fallback for text the scanner refused to parse (heredocs,
  command substitution, ``bash -c`` strings ...). It splits crudely into chunks, so it can be
  wrong in either direction; printing commands (``echo``, ``grep`` ...) and heredoc bodies fed
  to a non-shell are ignored to keep false positives rare.
"""

from __future__ import annotations

import re
from typing import Sequence

__all__ = ["argv_destructive", "text_destructive"]

_PRINTERS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "man", "less", "more", "head", "tail", "cat",
             "which", "type", "whatis", "apropos", "tee", "wc", "sort", "diff"}
_SHELLS = {"bash", "sh", "dash", "zsh", "ksh"}
_WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "nice", "ionice", "command", "exec", "builtin", "xargs", "eval",
             "stdbuf", "timeout", "setsid", "source", "."} | _SHELLS
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_CHUNK_SPLIT = re.compile(r"\$\(|[;&|(){}`\n]")
_SUBST = re.compile(r"\$\(([^()`]*)\)|`([^`]*)`")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(?:.*?\n)?[ \t]*\2[ \t]*(?=\n|$)", re.S)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _short_flags(v: str) -> str:
    """The letters of a ``-abc`` cluster ('' for long options and non-flags)."""
    return v[1:] if v.startswith("-") and not v.startswith("--") and len(v) > 1 else ""


def _rm(args: Sequence[str]) -> bool:
    for v in args:
        if v == "--":
            return False
        if v == "--recursive" or any(ch in "rR" for ch in _short_flags(v)):
            return True
    return False


def _git(args: Sequence[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args):
        return False
    sub, rest = args[i], list(args[i + 1 :])
    if sub == "reset":
        return any(re.fullmatch(r"--ha(rd?)?", v) for v in rest)
    if sub == "push":
        return any(v == "--force" or "f" in _short_flags(v) or (v.startswith("+") and len(v) > 1) for v in rest)
    if sub == "clean":
        return not any(v == "--dry-run" or "n" in _short_flags(v) for v in rest if v != "--")
    if sub == "checkout":
        return "--" in rest or rest == ["."]
    if sub == "restore":
        staged = any(v == "--staged" or "S" in _short_flags(v) for v in rest)
        worktree = any(v == "--worktree" or "W" in _short_flags(v) for v in rest)
        paths = any(not v.startswith("-") for v in rest)
        return paths and (worktree or not staged)
    if sub == "branch":
        delete = force = False
        for v in rest:
            if v == "--delete":
                delete = True
            elif v == "--force":
                force = True
            else:
                letters = _short_flags(v)
                if "D" in letters:
                    delete = force = True
                delete |= "d" in letters
                force |= "f" in letters
        return delete and force
    if sub == "stash":
        return rest[:1] in (["drop"], ["clear"])
    return False


def argv_destructive(vals: Sequence[str]) -> bool:
    """*vals* is the command word followed by its (unquoted) arguments."""
    if not vals:
        return False
    name, args = _base(vals[0]), list(vals[1:])
    if name == "rm":
        return _rm(args)
    if name == "git":
        return _git(args)
    if name == "find":
        return any(
            v in _FIND_EXEC and j + 1 < len(args) and argv_destructive(args[j + 1 :]) for j, v in enumerate(args)
        )
    if name in _WRAPPERS:
        for j, tok in enumerate(args):
            if _ASSIGN.match(tok) or (tok.startswith("-") and " " not in tok):
                continue
            if " " in tok or ";" in tok:  # a command string: bash -c '...', eval "..."
                if text_destructive(tok):
                    return True
                continue
            if _base(tok) in ("rm", "git", "find") or _base(tok) in _WRAPPERS:
                return argv_destructive(args[j:])
        return False
    return False


def _without_heredoc_bodies(text: str) -> str:
    def drop(m: re.Match) -> str:
        head = text[text.rfind("\n", 0, m.start()) + 1 : m.start()].split()
        shellish = bool(head) and _base(head[0].strip("'\"")) in _SHELLS
        return m.group(0) if shellish else text[m.start() : text.index("\n", m.start())]

    return _HEREDOC.sub(drop, text)


def _peel_substitutions(text: str) -> list[str]:
    """The text with each innermost ``$(...)`` / backtick pair replaced by a placeholder, plus every inner text.

    The inner commands really run, so they are analysed on their own; the outer text keeps its shape, so
    ``echo $(date) rm -rf x`` is still an echo and ``rm -rf $(pwd)/x`` is still an rm.
    """
    inner: list[str] = []
    while (m := _SUBST.search(text)) is not None:
        inner.append(m.group(1) or m.group(2) or "")
        text = text[: m.start()] + " SUBST " + text[m.end() :]
    return [text, *inner]


def text_destructive(text: str) -> bool:
    """Fallback for text the scanner refused to parse (see module docstring for the caveats)."""
    for part in _peel_substitutions(_without_heredoc_bodies(text)):
        for chunk in _CHUNK_SPLIT.split(part):
            toks = [t.strip("'\"") for t in chunk.split()]
            toks = [t for t in toks if t]
            while toks and _ASSIGN.match(toks[0]):
                toks.pop(0)
            if not toks or _base(toks[0]) in _PRINTERS:
                continue
            if argv_destructive(toks):
                return True
    return False
