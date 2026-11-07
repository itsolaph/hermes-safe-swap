"""Per-command rules. Each returns an :class:`Edit` for one simple command, or ``None``.

A rule only fires when it understands every word of the command. Unknown flags, expansions in
flag position, wrappers (``sudo``, ``env`` ...) and dangerous targets all mean ``None``: the
command then runs exactly as written and Hermes' own approval system stays in charge.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .config import Config
from .shellgen import branch_block, rm_block, snapshot_block
from .shellscan import Simple, Tok

__all__ = ["Edit", "apply_rules"]

_BIN_DIRS = ("/bin/", "/usr/bin/", "/usr/local/bin/", "/opt/homebrew/bin/")
_RESERVED = {"do", "done", "then", "fi", "else", "elif", "esac", "in", "case", "if", "while", "until", "for"}
_HOME_TARGET = re.compile(r"^(~|\$HOME|\$\{HOME\})/*\*?$")
_ROOT_TARGET = re.compile(r"^/+\*?$")
_TOP_LEVEL_TARGET = re.compile(r"^/+[^/*]*/*\*?$")  # "/x", "/x/", "/x/*": a top-level directory or its contents
_SYSTEM_TARGET = re.compile(
    r"^/(etc|usr|bin|sbin|lib|lib32|lib64|boot|dev|proc|sys|var|root|home|opt|srv|mnt|media|run)(/[^/*]+)?/*\*?$"
)
_TMP_PREFIXES = ("/tmp/", "/var/tmp/", "$TMPDIR/", "${TMPDIR}/")
_LEASE = "--force-with-lease --force-if-includes"


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str
    kind: str


def _command_name(tok: Tok) -> str:
    if tok.expands or tok.glob:
        return ""
    name = tok.val
    for prefix in _BIN_DIRS:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def apply_rules(command: str, seg: Simple, cfg: Config, base: str, rid: str, slot: str) -> Edit | None:
    """Dispatch on the command word of *seg*."""
    name = _command_name(seg.argv[0]) if seg.argv else ""
    if name == "rm" and cfg.rule_rm:
        return _rm(command, seg, cfg, base, rid, slot)
    if name == "git":
        return _git(command, seg, cfg, base, rid, slot)
    return None


# --------------------------------------------------------------------------- rm


def _dangerous_target(tok: Tok) -> bool:
    v = tok.val
    stripped = v.rstrip("/") or v
    if stripped in (".", "..") or stripped.endswith(("/.", "/..")):
        return True
    if ".safe-swap" in v:
        return True
    return bool(
        _HOME_TARGET.match(v) or _ROOT_TARGET.match(v) or _TOP_LEVEL_TARGET.match(v) or _SYSTEM_TARGET.match(v)
    )


def _tmp_target(tok: Tok) -> bool:
    return tok.val.startswith(_TMP_PREFIXES) and ".." not in tok.val.split("/")


def _redir_text(command: str, seg: Simple) -> str:
    return " ".join(command[op.start : target.end] for op, target in seg.redirs)


def _rm(command: str, seg: Simple, cfg: Config, base: str, rid: str, slot: str) -> Edit | None:
    force = recursive = False
    targets: list[Tok] = []
    options_done = False
    for tok in seg.argv[1:]:
        v = tok.val
        if options_done or not v.startswith("-") or v == "-":
            targets.append(tok)
            continue
        if tok.expands or tok.glob:
            return None
        if v == "--":
            options_done = True
        elif v.startswith("--"):
            if v == "--recursive":
                recursive = True
            elif v == "--force":
                force = True
            elif v not in ("--verbose", "--preserve-root"):
                return None
        else:
            for ch in v[1:]:
                if ch in "rR":
                    recursive = True
                elif ch == "f":
                    force = True
                elif ch != "v":
                    return None
    if not targets or any(_dangerous_target(t) for t in targets) or all(_tmp_target(t) for t in targets):
        return None
    if not (recursive or any(t.glob for t in targets) or cfg.swap_single_rm):
        return None
    raws = [("./" + t.raw) if t.raw in _RESERVED else t.raw for t in targets]
    text = rm_block(base, rid, slot, raws, force=force, recursive=recursive, redirs=_redir_text(command, seg))
    return Edit(seg.start, seg.end, text, "rm")


# --------------------------------------------------------------------------- git

_GLOBAL_NOARG = {"--no-pager", "-P", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs"}
_GLOBAL_ARG = {"-C", "-c"}


def _git(command: str, seg: Simple, cfg: Config, base: str, rid: str, slot: str) -> Edit | None:
    argv = seg.argv
    i, gopts = 1, []
    while i < len(argv) and argv[i].val.startswith("-"):
        v = argv[i].val
        if v in _GLOBAL_ARG and i + 1 < len(argv):
            gopts += [argv[i].raw, argv[i + 1].raw]
            i += 2
        elif v in _GLOBAL_NOARG or v.startswith(("--git-dir=", "--work-tree=")):
            gopts.append(argv[i].raw)
            i += 1
        else:
            return None
    if i >= len(argv) or argv[i].expands or argv[i].glob:
        return None
    sub, rest, gtext = argv[i].val, argv[i + 1 :], " ".join(gopts)
    original = command[seg.start : seg.end]
    if sub == "push" and cfg.rule_git_push:
        return _push(command, seg, argv[i], rest, cfg)
    if sub == "reset" and cfg.rule_git_reset:
        if any(re.fullmatch(r"--ha(rd?)?", t.val) for t in rest):
            return Edit(seg.start, seg.end, snapshot_block(base, rid, slot, gtext, original), "reset")
    elif sub == "checkout" and cfg.rule_git_restore:
        if any(t.val == "--" for t in rest) or [t.val for t in rest] == ["."]:
            return Edit(seg.start, seg.end, snapshot_block(base, rid, slot, gtext, original), "checkout")
    elif sub == "restore" and cfg.rule_git_restore:
        if _restore_touches_worktree(rest):
            return Edit(seg.start, seg.end, snapshot_block(base, rid, slot, gtext, original), "restore")
    elif sub == "branch" and cfg.rule_git_branch:
        names = _branch_delete_names(rest)
        if names:
            return Edit(seg.start, seg.end, branch_block(base, rid, slot, gtext, names, original), "branch")
    return None


def _restore_touches_worktree(rest: list[Tok]) -> bool:
    staged = worktree = paths = False
    for t in rest:
        v = t.val
        if v == "--staged" or (v.startswith("-") and not v.startswith("--") and "S" in v):
            staged = True
        if v == "--worktree" or (v.startswith("-") and not v.startswith("--") and "W" in v):
            worktree = True
        if not v.startswith("-"):
            paths = True
    return paths and (worktree or not staged)


def _branch_delete_names(rest: list[Tok]) -> list[str] | None:
    delete = force = False
    names: list[str] = []
    for t in rest:
        v = t.val
        if v == "--":
            continue
        if not v.startswith("-"):
            if t.expands or t.glob or not t.val:
                return None
            names.append(t.raw)
            continue
        if t.expands or t.glob:
            return None
        if v == "--delete":
            delete = True
        elif v == "--force":
            force = True
        elif v.startswith("--"):
            return None
        else:
            for ch in v[1:]:
                if ch == "D":
                    delete = force = True
                elif ch == "d":
                    delete = True
                elif ch == "f":
                    force = True
                elif ch != "q":
                    return None
    return names if (delete and force and names) else None


# --------------------------------------------------------------------------- git push

_PUSH_VALUED = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _trusted(refspecs: list[Tok], patterns: tuple[str, ...]) -> bool:
    if not patterns or not refspecs:
        return False
    for t in refspecs:
        spec = t.val.lstrip("+")
        dest = spec.split(":", 1)[1] if ":" in spec else spec
        dest = dest.removeprefix("refs/heads/")
        if not dest or not any(fnmatch.fnmatchcase(dest, p) for p in patterns):
            return False
    return True


def _push(command: str, seg: Simple, push_tok: Tok, rest: list[Tok], cfg: Config) -> Edit | None:
    has_lease = False
    force_edits: list[tuple[Tok, str]] = []  # (token, replacement without the lease flags)
    positional: list[Tok] = []
    skip_value = options_done = False
    for tok in rest:
        v = tok.val
        if skip_value:
            skip_value = False
            continue
        if options_done or not v.startswith("-"):
            positional.append(tok)
            continue
        if tok.expands or tok.glob:
            return None
        if v == "--":
            options_done = True
        elif v.startswith("--"):
            name = v.split("=", 1)[0]
            if name == "--force":
                if "=" in v:
                    return None
                force_edits.append((tok, ""))
            elif name == "--force-with-lease":
                has_lease = True
            elif name in ("--force-if-includes", "--no-force-with-lease", "--no-force-if-includes", "--mirror", "--delete"):
                return None
            elif name in _PUSH_VALUED and "=" not in v:
                skip_value = True
        else:
            kept, pos = [], 1
            while pos < len(v):
                ch = v[pos]
                if ch == "o":  # -o <opt> / -o<opt>: the rest of the cluster is a value
                    kept.append(v[pos:])
                    skip_value = pos == len(v) - 1
                    break
                if ch == "d":
                    return None
                if ch != "f":
                    kept.append(ch)
                pos += 1
            if len("".join(kept)) != len(v) - 1:  # an f was removed
                force_edits.append((tok, ("-" + "".join(kept)) if kept else ""))
    refspecs = positional[1:]
    plus = [t for t in refspecs if t.val.startswith("+")]
    if any(t.val.lstrip("+").startswith(":") for t in refspecs) or any(t.raw[:1] in "'\"" for t in plus):
        return None
    if not (force_edits or plus) or (force_edits and has_lease) or _trusted(refspecs, cfg.trusted_force_branches):
        return None
    edits: list[tuple[int, int, str]] = [(t.start, t.end, t.raw[1:]) for t in plus]
    if has_lease:
        edits += [(t.start, t.end, new) for t, new in force_edits]
    else:
        edits += [(t.start, t.end, (new + " " + _LEASE).strip() if k == 0 else new) for k, (t, new) in enumerate(force_edits)]
        if not force_edits:
            edits.append((push_tok.end, push_tok.end, " " + _LEASE))
    text = command[seg.start : seg.end]
    for start, end, new in sorted(edits, reverse=True):
        text = text[: start - seg.start] + new + text[end - seg.start :]
    return Edit(seg.start, seg.end, text, "push")
