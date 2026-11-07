# Progress

- 2026-11-07: milestone 1 done: scanner, rules, shell generators, rewriter, undo scripts; 302 tests (table + bash/dash behavioural + real git force-push scenarios) green.
- 2026-11-07: milestone 2+3 done: receipts store, hooks, /swaps /unswap + hermes safe-swap CLI, integration test through the real plugin loader and handle_function_call; hermes plugins validate passes (security scan: safe); hook latency p50 ~1.7 ms with a DB write.
- 2026-11-07: milestone 4 done: README (transcript from a real run), demo/demo.sh, catalog-entry.yaml (parses with Hermes' catalog loader).
- 2026-11-07: review pass: hardline sweep (3.4k variants), per-kind restore scripts, private refs/safe-swap snapshots, approval-aware runner, CDPATH fix; 3429 tests green; validate passes. Latency p50 0.8 ms (no rule) / 1.5-1.8 ms (rewrite incl. DB write), max 15 ms.

## v2 (2026-11-07)

Four changes on top of the reviewed v1, same design (never block, never prompt, fail open, receipts + `/swaps` + `/unswap`).

1. **Visible "NOT swapped" notice.** `rewriter.analyze()` returns the rewrite plus the reasons why destructive-looking
   parts were left alone; `rules.py` rules return `Edit | Skip | None`; the new `destructive.py` decides whether a
   command really looks destructive (token level when the scanner succeeds, a conservative text fallback when it bails,
   ignoring printers such as `echo`/`grep` and heredoc bodies fed to a non-shell). The hook appends
   `[safe-swap] NOT swapped (<reason>): <first 80 chars>`. Runtime failures (snapshot or quarantine could not be
   created) are printed by the generated shell itself and rewritten into the same line by the hook; they replace the
   "was run as" claim. Silent for harmless commands and for exclusions the user configured. New key `notify_unswapped`
   (default on, independent of `note_to_agent`). Our own `/unswap` and purge scripts are exempt.
2. **Blast radius.** The rm block counts non-directory entries and bytes before moving (cap 50,000 entries, 2 s when
   `timeout` exists, then `50000+ files`) and flags a move that degraded to copy by comparing inodes. The numbers travel
   as one `SS_STATS` stderr line printed outside the user's redirections; `post_tool_call` stores them in the receipt
   (columns added to an existing `receipts.db` in place), `transform_tool_result` strips the line and puts them in the
   note, `/swaps` has a `size` column.
3. **New git rules.** `git clean -f/-fd/-fdx/-fX`: the untracked (and ignored) files are committed to
   `refs/safe-swap/<id>` through a throwaway index (NUL-safe, real index/stash/work tree untouched), then the original
   clean runs; undo checks out only the missing files. `git stash drop [<stash>]` / `git stash clear`: the dropped
   commit(s) are kept alive under one private ref and recorded oldest first; undo runs `git stash store`. `-n`,
   `-i`, `-ff` are left alone (`-i`/`-ff` with a notice). Behaviour tests against real repos under bash and dash.
4. **README + catalog text.** Removed the false "only a hook sees the command" claim, rewrote the checkpoint section
   with a three-way comparison (safe-swap / undo-effects / core `/rollback`), added "What it will not do", led with
   the force-push lease, updated `catalog-entry.yaml` and `plugin.yaml`, replaced flagged `rm -rf` examples with
   `rm -r`.

Verification: `hermes_cli.plugin_validate.validate_plugin_dir` on a copy of the plugin passes with security scan
`safe` and no warnings; the catalog entry parses with Hermes' catalog loader. The suite sets `HERMES_HOME` to a
throwaway directory for the whole run (root `conftest.py`).

Known limits chosen on purpose: the clean snapshot is a superset of what clean deletes and is skipped (with a notice)
above 20,000 files / 200 MB per tier; empty directories are not restored; a dropped `stash@{n}` returns as
`stash@{0}`; `mv`, `cp`, `sed -i`, `>`, `write_file`/`patch`, `find -delete` and `rebase` stay untouched.
