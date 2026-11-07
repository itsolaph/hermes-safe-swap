"""POSIX ``sh`` text for the reversible twins. Pure string builders, no I/O.

Every block is ONE grouped command (``{ ...; }``), so ``&&``, ``||``, ``|`` and ``;`` around it
keep their meaning, and its exit status is the original command's. It runs unchanged under
dash and bash, on any terminal backend, and needs no helper file on disk.
"""

from __future__ import annotations

MARKER = "SS_RCPT="

STATS = "SS_STATS"  # stderr line carrying the blast radius: SS_STATS <rid> <files> <bytes> <capped> <complete> <copied>
NOT_SWAPPED = "[safe-swap] NOT swapped ("  # runtime failure line: the transform hook appends the original command

MAX_WALK = 50_000  # blast radius: stop counting after this many entries ...
WALK_SECONDS = 2  # ... or this many seconds (when `timeout` exists)
MAX_SNAPSHOT_FILES = 20_000  # git clean: an untracked/ignored set larger than this is not snapshotted
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024

_UNSET_RM = "unset SS_Q SS_N SS_P SS_T SS_D SS_RC SS_RCPT SS_CP SS_ST SS_E SS_I SS_J SS_TO"

# Blast-radius counter, run as `sh -c '<this>' sh <targets>`. Prints cumulative "files bytes capped complete"
# lines (the last one wins, so a `timeout` kill still leaves the totals so far). Files = every non-directory entry.
# `find -print | head` makes find itself die of SIGPIPE at the cap, so a huge tree is never walked to the end even
# without `timeout`; bytes are only measured (second walk) for a target that fit under the cap.
def _count_child() -> str:
    return (
        'for t in find ls head wc awk; do command -v "$t" >/dev/null 2>&1 || exit 0; done; '  # missing tool: no numbers
        f'n=0; b=0; c=0; e=0; for p; do [ "$n" -lt {MAX_WALK} ] || {{ c=1; break; }}; '
        'case "$p" in /*) q=$p;; *) q=./$p;; esac; [ -e "$q" ] || [ -L "$q" ] || continue; e=1; '
        f'f=$(find "$q" ! -type d -print 2>/dev/null | head -n $(({MAX_WALK}-n)) | wc -l); f=$((f+0)); n=$((n+f)); '
        f'if [ "$n" -ge {MAX_WALK} ]; then c=1; else '
        's=$(find "$q" ! -type d -exec ls -lnd {} + 2>/dev/null | awk "/^-/{s+=\\$5} END{print s+0}"); b=$((b+${s:-0})); fi; '
        'printf "%s %s %s 0\\n" "$n" "$b" "$c"; done; '
        '[ "$e" = 0 ] || printf "%s %s %s 1\\n" "$n" "$b" "$c"'
    )


_GIT_IDENT = (
    "GIT_AUTHOR_NAME=safe-swap GIT_AUTHOR_EMAIL=safe-swap "
    "GIT_COMMITTER_NAME=safe-swap GIT_COMMITTER_EMAIL=safe-swap"
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
        'SS_D=$(unset CDPATH; cd -- "$(dirname -- "$SS_T")" 2>/dev/null && pwd -P) || SS_D=; '
        "SS_N=$((SS_N+1)); "
        'SS_I=$(ls -di -- "$SS_T" 2>/dev/null) || :; SS_I=${SS_I%% *}; '
        'if [ -n "$SS_D" ] && mv -- "$SS_T" "$SS_Q/$SS_N"; then '
        # a rename keeps the inode; a different one means mv degraded to copy+delete (another filesystem)
        'SS_J=$(ls -di -- "$SS_Q/$SS_N" 2>/dev/null) || :; SS_J=${SS_J%% *}; '
        'if [ -n "$SS_I" ] && [ -n "$SS_J" ] && [ "$SS_I" != "$SS_J" ]; then SS_CP=1; fi; '
        "printf '%s\\t%s/%s\\n' \"$SS_N\" \"${SS_D%/}\" \"$(basename -- \"$SS_T\")\" >> \"$SS_Q/manifest.tsv\"; "
        "else printf \"rm: cannot remove '%s'\\n\" \"$SS_P\" >&2; SS_RC=1; fi; "
        f"else {missing}; fi"
    )
    words = " ".join(targets)
    count = (
        f"if command -v timeout >/dev/null 2>&1; then SS_TO='timeout {WALK_SECONDS}'; else SS_TO=; fi; "
        # `|| :` keeps whatever was captured and keeps `set -e` / `pipefail` from aborting on a killed walk
        f"SS_ST=$($SS_TO sh -c '{_count_child()}' sh {words} 2>/dev/null | tail -n 1) || :; "
    )
    inner = (
        'if mkdir -p -- "$SS_Q"; then '
        f"{count}for SS_P in {words}; do {body}; done; "
        "else SS_E='quarantine creation failed, nothing was removed'; SS_RC=1; fi;"
    )
    # The user's redirections apply to the work itself; safe-swap's own lines are printed outside them.
    inner = f"{{ {inner} }}" + (f" {redirs}" if redirs else "") + ";"
    return (
        f"{{ {_slot_dir(base, rid, slot)}; SS_N=0; SS_RC=0; SS_CP=0; SS_ST=; SS_E=; {inner} "
        'case "$SS_ST" in ""|*[!0-9\\ ]*) ;; *) printf \'' + STATS + ' %s %s %s\\n\' "$SS_RCPT" "$SS_ST" "$SS_CP" >&2;; esac; '
        f'[ -z "$SS_E" ] || printf \'{NOT_SWAPPED}%s)\\n\' "$SS_E" >&2; '
        f'if [ "$SS_RC" = 0 ]; then {_UNSET_RM}; else {_UNSET_RM}; false; fi; }}'
    )


def snapshot_block(base: str, rid: str, slot: str, gopts: str, original: str) -> str:
    """``git stash create`` + a private ref (tree and stash stack untouched), record where, then the original."""
    g = f"git {gopts} " if gopts else "git "
    return (
        f"{{ {_slot_dir(base, rid, slot)}; "
        f'mkdir -p -- "$SS_Q" 2>/dev/null && {{ {g}rev-parse --show-toplevel > "$SS_Q/gitdir" && '
        f'{g}rev-parse HEAD > "$SS_Q/head"; }} 2>/dev/null; '
        f"SS_SHA=$({_GIT_IDENT} {g}stash create 2>/dev/null) || SS_SHA=; "
        f'if [ -n "$SS_SHA" ] && [ -d "$SS_Q" ] && {g}update-ref "refs/safe-swap/{slot}" "$SS_SHA" 2>/dev/null; then '
        "printf '%s\\n' \"$SS_SHA\" > \"$SS_Q/stash.sha\"; "
        f"elif {g}rev-parse --verify -q HEAD >/dev/null 2>&1 && ! {g}diff --quiet HEAD -- 2>/dev/null; then "
        f"printf '{NOT_SWAPPED}snapshot failed, ran unprotected)\\n' >&2; fi; "
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


def _say(reason: str) -> str:
    return f"printf '{NOT_SWAPPED}{reason})\\n' >&2"


# `ls-files -z` entries ending in "/" are untracked directories or nested repositories: git cannot store them
# in an index, so they are dropped; their content is listed (or deliberately skipped) separately.
_DROP_DIRS = r"""xargs -0 sh -c 'for p; do case "$p" in */) ;; *) printf "%s\0" "$p";; esac; done' sh"""


def _tier(g0: str, name: str, ls_flags: str, pathspecs: str, what: str) -> str:
    """List one tier of files into ``$SS_Q/<name>.z`` (NUL separated, repository-relative) and apply the size caps."""
    f = f'"$SS_Q/{name}.z"'
    return (
        f"{g0}ls-files -z --full-name {ls_flags}{pathspecs} 2>/dev/null | {_DROP_DIRS} > {f} || :; "
        f"SS_C=$(tr -cd '\\000' < {f} | wc -c) || SS_C=0; SS_C=$((SS_C+0)); "
        f"if [ \"$SS_C\" -gt {MAX_SNAPSHOT_FILES} ]; then : > {f}; {_say(what + ' too many to snapshot')}; "
        f"elif [ \"$SS_C\" -gt 0 ]; then SS_BY=$(xargs -0 ls -lnd -- < {f} 2>/dev/null | "
        'awk "/^-/{s+=\\$5} END{print s+0}") || SS_BY=; '
        f"if [ \"${{SS_BY:-0}}\" -gt {MAX_SNAPSHOT_BYTES} ]; then : > {f}; {_say(what + ' too large to snapshot')}; fi; fi; "
    )


def clean_block(base: str, rid: str, slot: str, gopts: str, *, untracked: bool, ignored: bool, recurse: bool,
                pathspecs: list[str], original: str) -> str:
    """Snapshot what ``git clean`` may delete into a private ref, then run the original clean unchanged.

    The snapshot is a commit (``refs/safe-swap/<slot>``) built from a throwaway index, so the real index, the
    work tree and the stash stack are untouched, and every file name is passed NUL-separated (never through the
    shell). It is a superset of what clean removes; undo only restores files that are missing.
    """
    g0 = f"git {gopts} " if gopts else "git "
    g1 = g0 + '-C "$SS_TL" '
    ps = (" -- " + " ".join(pathspecs)) if pathspecs else ""
    dirs = "" if recurse else "--directory "
    tiers = ""
    if untracked:
        tiers += _tier(g0, "t1", f"-o --exclude-standard {dirs}", ps, "untracked files")
    if ignored:
        tiers += _tier(g0, "t2", f"-o -i --exclude-standard {dirs}", ps, "ignored files")
    lists = " ".join(f'"$SS_Q/{n}.z"' for n, on in (("t1", untracked), ("t2", ignored)) if on)
    ident = f"{_GIT_IDENT} "
    build = (
        f"SS_Z=$(cat {lists} | tr -cd '\\000' | wc -c) || SS_Z=0; SS_Z=$((SS_Z+0)); "
        'if [ "$SS_Z" -gt 0 ]; then SS_X="$SS_Q/snapshot.index"; '
        f'if cat {lists} | GIT_INDEX_FILE="$SS_X" {g1}update-index --add -z --stdin 2>/dev/null && '
        f'SS_TR=$(GIT_INDEX_FILE="$SS_X" {g1}write-tree 2>/dev/null) && [ -n "$SS_TR" ] && '
        f'SS_CM=$({ident}{g1}commit-tree "$SS_TR" -m "safe-swap: files git clean may delete" </dev/null 2>/dev/null) && '
        f'[ -n "$SS_CM" ] && {g1}update-ref "refs/safe-swap/{slot}" "$SS_CM" 2>/dev/null; then '
        "printf '%s\\n' \"$SS_CM\" > \"$SS_Q/clean.sha\"; "
        f"else {_say('snapshot of untracked files failed, ran unprotected')}; fi; fi; "
    )
    return (
        f"{{ {_slot_dir(base, rid, slot)}; "
        f"SS_TL=$({g0}rev-parse --show-toplevel 2>/dev/null) || SS_TL=; "
        'if [ -n "$SS_TL" ]; then '
        'case "$SS_Q/" in "$SS_TL"/*) '
        f"{_say('quarantine is inside this repository, ran unprotected')} ;; "
        '*) if mkdir -p -- "$SS_Q" 2>/dev/null && printf \'%s\\n\' "$SS_TL" > "$SS_Q/gitdir"; then '
        f"{tiers}{build}"
        f"else {_say('could not create the snapshot directory, ran unprotected')}; fi;; esac; fi; "
        "unset SS_Q SS_TL SS_C SS_BY SS_Z SS_X SS_TR SS_CM SS_RCPT; "
        f"{original}; }}"
    )


def stash_block(base: str, rid: str, slot: str, gopts: str, *, clear: bool, ref: str, original: str) -> str:
    """Record the SHA(s) of the stash entries about to be dropped (oldest first) under one private ref."""
    g = f"git {gopts} " if gopts else "git "
    find = (  # `stash list` is newest first; the loop reverses it so entries are recorded (and restored) oldest first
        f'SS_L=$({g}stash list --format=%H 2>/dev/null) || SS_L=; SS_R=; for SS_S in $SS_L; do SS_R="$SS_S $SS_R"; done; SS_L=$SS_R'
        if clear
        else f"SS_L=$({g}rev-parse --verify -q {ref} 2>/dev/null) || SS_L="
    )
    return (
        f"{{ {_slot_dir(base, rid, slot)}; "
        f"SS_TL=$({g}rev-parse --show-toplevel 2>/dev/null) || SS_TL=; "
        'if [ -n "$SS_TL" ]; then '
        'if mkdir -p -- "$SS_Q" 2>/dev/null && printf \'%s\\n\' "$SS_TL" > "$SS_Q/gitdir"; then '
        f"{find}; "
        'if [ -n "$SS_L" ]; then SS_P=; for SS_S in $SS_L; do SS_P="$SS_P -p $SS_S"; '
        "printf '%s\\n' \"$SS_S\" >> \"$SS_Q/stash.shas\"; done; "
        f'SS_K=$({_GIT_IDENT} {g}commit-tree "$({g}mktree </dev/null)" $SS_P '
        '-m "safe-swap: stash entries dropped" </dev/null 2>/dev/null) || SS_K=; '
        f'if [ -n "$SS_K" ] && {g}update-ref "refs/safe-swap/{slot}" "$SS_K" 2>/dev/null; then :; '
        f"else {_say('could not record the stash entries, ran unprotected')}; fi; fi; "
        f"else {_say('could not create the snapshot directory, ran unprotected')}; fi; fi; "
        f"unset SS_Q SS_TL SS_L SS_R SS_P SS_S SS_K SS_RCPT; {original}; }}"
    )
