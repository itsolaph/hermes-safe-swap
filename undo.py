"""POSIX ``sh`` scripts that restore or purge a receipt from its quarantine directories.

Receipts are plain files (``manifest.tsv``, ``stash.sha``, ``branches.tsv`` ...), so the
scripts work on whatever backend the original command ran on, and by hand without the plugin.
A receipt id owns the directories ``<base>/<id>`` and ``<base>/<id>.<n>`` (one per rewritten
segment of a compound command).
"""

from __future__ import annotations

import re

__all__ = ["purge_script", "restore_script"]

_ID = re.compile(r"^[0-9a-f]{6}$")


def _checked(rid: str) -> str:
    if not _ID.match(rid):
        raise ValueError(f"invalid receipt id: {rid!r}")
    return rid


def restore_script(base: str, rid: str, *, force: bool = False) -> str:
    """Put everything a receipt quarantined or snapshotted back. Exit status 0 = fully restored."""
    rid = _checked(rid)
    exists = (
        'echo "exists, not overwritten (use --force): $SS_O"; SS_OK=0; continue'
        if not force
        else f'mv -- "$SS_O" "$SS_O.pre-unswap-{rid}" || {{ echo "cannot move aside: $SS_O"; SS_OK=0; continue; }}; '
        f'echo "moved existing to $SS_O.pre-unswap-{rid}"'
    )
    return f"""SS_B={base}; SS_RC=0; SS_ANY=0; SS_TAB=$(printf '\\t')
for SS_Q in "$SS_B/{rid}" "$SS_B/{rid}".*; do
  [ -d "$SS_Q" ] || continue
  if [ -f "$SS_Q/manifest.tsv" ]; then
    SS_ANY=1; SS_OK=1
    while IFS="$SS_TAB" read -r SS_N SS_O; do
      [ -n "$SS_N" ] && [ -n "$SS_O" ] || continue
      if [ ! -e "$SS_Q/$SS_N" ] && [ ! -L "$SS_Q/$SS_N" ]; then echo "missing in quarantine: $SS_O"; SS_OK=0; continue; fi
      if [ -e "$SS_O" ] || [ -L "$SS_O" ]; then {exists}; fi
      if mkdir -p -- "$(dirname -- "$SS_O")" && mv -- "$SS_Q/$SS_N" "$SS_O"; then echo "restored: $SS_O"; else echo "failed: $SS_O"; SS_OK=0; fi
    done < "$SS_Q/manifest.tsv"
    if [ "$SS_OK" = 1 ]; then mv -- "$SS_Q/manifest.tsv" "$SS_Q/manifest.tsv.restored"; else SS_RC=1; fi
  fi
  if [ -f "$SS_Q/gitdir" ] && [ -f "$SS_Q/head" ]; then
    SS_ANY=1; SS_G=$(cat "$SS_Q/gitdir"); SS_H=$(cat "$SS_Q/head")
    SS_NOW=$(cd -- "$SS_G" && git rev-parse HEAD 2>/dev/null)
    if [ "$SS_H" != "$SS_NOW" ]; then
      if (cd -- "$SS_G" && git merge-base --is-ancestor "$SS_NOW" "$SS_H" && git diff --quiet HEAD -- && git reset --hard --quiet "$SS_H"); then
        echo "moved HEAD back to $SS_H"
      else echo "note: HEAD was $SS_H before the command (not moved: the tree or history changed since); 'git reset --hard $SS_H' would bring the commits back"; fi
    fi
  fi
  if [ -f "$SS_Q/stash.sha" ] && [ -f "$SS_Q/gitdir" ]; then
    SS_ANY=1; SS_G=$(cat "$SS_Q/gitdir")
    if (cd -- "$SS_G" && git stash apply --quiet "$(cat "$SS_Q/stash.sha")"); then
      echo "restored working-tree changes in $SS_G"; mv -- "$SS_Q/stash.sha" "$SS_Q/stash.sha.applied"
    else echo "git stash apply failed in $SS_G (conflicts?); snapshot kept: git stash list | grep {rid}"; SS_RC=1; fi
  elif [ -f "$SS_Q/head" ]; then
    echo "working tree was clean, no changes to re-apply"
  fi
  if [ -f "$SS_Q/branches.tsv" ] && [ -f "$SS_Q/gitdir" ]; then
    SS_ANY=1; SS_G=$(cat "$SS_Q/gitdir")
    while IFS="$SS_TAB" read -r SS_BN SS_BS; do
      [ -n "$SS_BN" ] && [ -n "$SS_BS" ] || continue
      if (cd -- "$SS_G" && git branch "$SS_BN" "$SS_BS" >/dev/null); then echo "restored branch: $SS_BN at $SS_BS"; else echo "failed to restore branch: $SS_BN"; SS_RC=1; fi
    done < "$SS_Q/branches.tsv"
    mv -- "$SS_Q/branches.tsv" "$SS_Q/branches.tsv.restored"
  fi
done
[ "$SS_ANY" = 1 ] || echo "nothing to restore for {rid}"
if [ "$SS_RC" = 0 ]; then true; else false; fi"""


def purge_script(base: str, rids: list[str]) -> str:
    """Delete the quarantine directories of the given receipts (the only deletion Safe-Swap does)."""
    words = " ".join(f'"$SS_B/{_checked(r)}" "$SS_B/{r}".*' for r in rids)
    return f'SS_B={base}; for SS_D in {words}; do [ -d "$SS_D" ] && rm -rf -- "$SS_D"; done; true'
