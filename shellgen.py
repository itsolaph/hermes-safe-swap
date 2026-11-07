"""POSIX ``sh`` text for the reversible twins. Pure string builders, no I/O.

Every block is ONE grouped command (``{ ...; }``), so ``&&``, ``||``, ``|`` and ``;`` around it
keep their meaning, and its exit status is the original command's. It runs unchanged under
dash and bash, on any terminal backend, and needs no helper file on disk.
"""

from __future__ import annotations

MARKER = "SS_RCPT="

_UNSET_RM = "unset SS_Q SS_N SS_P SS_T SS_D SS_RC SS_RCPT"
_GIT_IDENT = (
    "GIT_AUTHOR_NAME=safe-swap GIT_AUTHOR_EMAIL=safe-swap@localhost "
    "GIT_COMMITTER_NAME=safe-swap GIT_COMMITTER_EMAIL=safe-swap@localhost"
)


def _slot_dir(base: str, rid: str, slot: str) -> str:
    # *base* is a double-quoted word like "${HOME:-/tmp}/.safe-swap/trash"; splice the slot before the closing quote.
    return f'SS_RCPT={rid}; SS_Q={base[:-1]}/{slot}"'


def rm_block(base: str, rid: str, slot: str, targets: list[str], *, force: bool, recursive: bool, redirs: str) -> str:
    """Move every target into ``<base>/<slot>/<n>`` and log ``n<TAB>absolute path`` in ``manifest.tsv``.

    Mirrors rm: with *force* a missing target is silent, without it the error is printed and
    the status is 1; a directory without *recursive* is refused; any move failure gives status 1.
    """
    missing = ":" if force else (
        "printf \"rm: cannot remove '%s': No such file or directory\\n\" \"$SS_P\" >&2; SS_RC=1"
    )
    is_dir = "" if recursive else (
        'if [ -d "$SS_P" ] && [ ! -L "$SS_P" ]; then '
        "printf \"rm: cannot remove '%s': Is a directory\\n\" \"$SS_P\" >&2; SS_RC=1; continue; fi; "
    )
    body = (
        f'if [ -e "$SS_P" ] || [ -L "$SS_P" ]; then {is_dir}'
        'SS_T=${SS_P%"${SS_P##*[!/]}"}; [ -n "$SS_T" ] || SS_T=$SS_P; '
        'SS_D=$(cd -- "$(dirname -- "$SS_T")" 2>/dev/null && pwd -P) || SS_D=; '
        "SS_N=$((SS_N+1)); "
        'if [ -n "$SS_D" ] && mv -- "$SS_T" "$SS_Q/$SS_N"; then '
        "printf '%s\\t%s/%s\\n' \"$SS_N\" \"${SS_D%/}\" \"$(basename -- \"$SS_T\")\" >> \"$SS_Q/manifest.tsv\"; "
        "else printf \"rm: cannot remove '%s'\\n\" \"$SS_P\" >&2; SS_RC=1; fi; "
        f"else {missing}; fi"
    )
    text = (
        f"{{ {_slot_dir(base, rid, slot)}; SS_N=0; SS_RC=0; "
        'if mkdir -p -- "$SS_Q"; then '
        f"for SS_P in {' '.join(targets)}; do {body}; done; "
        "else printf 'safe-swap: cannot create quarantine %s; nothing was removed\\n' \"$SS_Q\" >&2; SS_RC=1; fi; "
        f'if [ "$SS_RC" = 0 ]; then {_UNSET_RM}; else {_UNSET_RM}; false; fi; }}'
    )
    return f"{text} {redirs}" if redirs else text


def snapshot_block(base: str, rid: str, slot: str, gopts: str, original: str) -> str:
    """Run ``git stash create`` + ``stash store`` (tree untouched), record where, then the original."""
    g = f"git {gopts} " if gopts else "git "
    return (
        f"{{ {_slot_dir(base, rid, slot)}; "
        f'mkdir -p -- "$SS_Q" 2>/dev/null && {{ {g}rev-parse --show-toplevel > "$SS_Q/gitdir" && '
        f'{g}rev-parse HEAD > "$SS_Q/head"; }} 2>/dev/null; '
        f"SS_SHA=$({_GIT_IDENT} {g}stash create 2>/dev/null) || SS_SHA=; "
        f'if [ -n "$SS_SHA" ] && [ -d "$SS_Q" ] && {g}stash store -m "safe-swap {rid}" "$SS_SHA" 2>/dev/null; then '
        "printf '%s\\n' \"$SS_SHA\" > \"$SS_Q/stash.sha\"; "
        f"elif ! {g}diff --quiet HEAD -- 2>/dev/null; then "
        "printf 'safe-swap: could not snapshot the working tree; running unprotected\\n' >&2; fi; "
        f"unset SS_Q SS_SHA SS_RCPT; {original}; }}"
    )


def branch_block(base: str, rid: str, slot: str, gopts: str, names: list[str], original: str) -> str:
    """Record ``name<TAB>sha`` of every branch about to be deleted, then run the original."""
    g = f"git {gopts} " if gopts else "git "
    return (
        f"{{ {_slot_dir(base, rid, slot)}; "
        f'mkdir -p -- "$SS_Q" && {g}rev-parse --show-toplevel > "$SS_Q/gitdir" 2>/dev/null; '
        f"for SS_B in {' '.join(names)}; do "
        f'SS_S=$({g}rev-parse --verify -q "refs/heads/$SS_B") && '
        "printf '%s\\t%s\\n' \"$SS_B\" \"$SS_S\" >> \"$SS_Q/branches.tsv\"; done; "
        f"unset SS_Q SS_B SS_S SS_RCPT; {original}; }}"
    )
