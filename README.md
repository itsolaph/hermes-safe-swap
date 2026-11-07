# Safe-Swap for Hermes Agent

**Safe-Swap never blocks and never nags.** When the agent runs a destructive shell command, a
`pre_tool_call` hook silently swaps it for its reversible twin and keeps a receipt. `rm -rf build`
becomes a move into a quarantine folder, `git push --force` becomes `--force-with-lease
--force-if-includes`, `git reset --hard` takes a snapshot first. `/swaps` lists the receipts and
`/unswap <id>` undoes them. The agent is told what really ran, so it never reasons from a false
picture. A skill or an MCP server cannot do this: only a hook sees the command before it executes.

## Demo

```text
$ rm -rf build
[safe-swap] `rm -rf build` was run as: move-to-quarantine (receipt b2a983). Undo: /unswap b2a983
exit 0

$ git push --force origin main          # a teammate pushed a commit the agent never fetched
 ! [rejected]        main -> main (stale info)
[safe-swap] `git push --force origin main` was run as: git push --force-with-lease --force-if-includes (not --force) (receipt d18287). Not undoable (the lease only refuses unsafe pushes).
exit 1

$ git reset --hard                      # with a half-finished edit in the working tree
HEAD is now at 99072a3 initial import (reworded)
[safe-swap] `git reset --hard` was run as: preceded by a stash snapshot (receipt a00c25). Undo: /unswap a00c25
exit 0

> /swaps
id      age   status   kind      command
a00c25  now   applied  reset     git reset --hard
d18287  now   failed   push      git push --force origin main
b2a983  now   applied  rm        rm -rf build

> /unswap a00c25
Undone a00c25.
restored working-tree changes in /path/to/project
> /unswap b2a983
Undone b2a983.
restored: /path/to/project/build
```

`demo/demo.sh` builds exactly this scenario in a throwaway directory (project, build dir, bare
remote, a teammate commit) and prints the three commands to hand to the agent.

## Install

```bash
hermes plugins install itsolaph/hermes-safe-swap
hermes plugins enable safe-swap
```

Requires Hermes `>=0.23.0`, a POSIX `sh` on the machine where commands run, and git `>=2.30`
for the push rule (`--force-if-includes`).

## What gets swapped

| Command | Runs as | Undo |
|---|---|---|
| `rm -r…`, `rm` with a glob, `rm file` | each target is moved into the quarantine folder (`<id>/<n>`); absolute paths go in `manifest.tsv`. Exit status, `-f` and "No such file" behave like `rm` | `/unswap <id>` moves everything back (refuses to overwrite unless `--force`) |
| `git push --force`, `-f`, `+refspec` | `--force-with-lease --force-if-includes` (an existing lease is left alone) | none: a lease only refuses pushes that would destroy someone else's work |
| `git reset --hard [rev]` | `git stash create` + `stash store` first (the tree is not touched), then the original | `/unswap` moves HEAD back if nothing happened since, then re-applies the snapshot |
| `git checkout -- <paths>`, `git checkout .`, `git restore <paths>` (not `--staged` only) | same snapshot, then the original | `/unswap` re-applies the snapshot |
| `git branch -D <b>…` | records each branch tip first | `/unswap` runs `git branch <b> <sha>` |

Every rewrite is one grouped command (`{ …; }`) with the original's exit status, so `&&`, `||`,
`|` and `;` around it mean what they meant before. Redirections are kept. Compound commands such as
`cd x && rm -rf y | tee log` are rewritten segment by segment. Receipts are plain files, so
recovery works by hand (`mv` the numbered entry back to the path in `manifest.tsv`) even without the plugin.

## What it deliberately does NOT swap

Safe-Swap swaps only what it fully understands; everything else runs **untouched**, and Hermes'
own approval system stays in charge. It is not a guard.

- `/`, `~`, `$HOME`, `/*`, `.`, `..`, top-level system directories: moving those into a quarantine is
  nonsense, so Hermes' dangerous-command handling decides.
- Targets under `/tmp`, `/var/tmp`, `$TMPDIR`, and anything touching `.safe-swap` itself.
- `sudo rm …` / `sudo git …` and other wrappers (`env`, `nohup`, `xargs`, `find -exec/-delete`,
  `bash -c`, `eval`, `source`): root-owned paths cannot be quarantined correctly as the caller.
- Anything with a newline outside quotes, heredocs, backticks, `$(…)`, subshells, `{ }` groups,
  background `&`, process substitution, comments, or unknown `rm` flags (`-i`, `-d`,
  `--no-preserve-root` …).
- `git clean` (not in v1), `git push --mirror/--delete`, deleting refspecs (`:branch`), and a push that
  already mixes `--force` with a lease.
- Branches listed in `trusted_force_branches`.

## Configuration

`plugins.entries.safe-swap.settings` (all optional, shown with defaults):

| Key | Default | Meaning |
|---|---|---|
| `rule_rm` | `true` | rm → quarantine |
| `swap_single_rm` | `true` | also swap plain single-file `rm` (off: only recursive/glob) |
| `rule_git_push` | `true` | force push → lease |
| `rule_git_reset` | `true` | snapshot before `git reset --hard` |
| `rule_git_restore` | `true` | snapshot before `git checkout --` / `git restore` |
| `rule_git_branch` | `true` | record tip before `git branch -D` |
| `trusted_force_branches` | `[]` | glob patterns of destination branches where force push is left alone, e.g. `[wip/*]` |
| `quarantine_dir` | `~/.safe-swap/trash` | absolute path or `~/path`, on the machine where the command runs |
| `note_to_agent` | `true` | append the one-line "what really ran" note to the tool result |

## Commands

- `/swaps`: last 10 receipts. `/swaps purge [--older 7d]`: delete quarantine data. **This is the
  only thing that ever deletes quarantine data; nothing is purged automatically.**
- `/unswap <id|last> [--force]`: restore. Runs through the session's own terminal backend, so a
  receipt from a docker or ssh sandbox is restored there.
- `hermes safe-swap list|undo <id>|purge [--older 7d]`: the same for the local backend.

## What it touches (disclosure)

- **Rewrites `terminal` commands before they execute** (`pre_tool_call`, `modify`); never blocks,
  never raises (any error or doubt means the command runs unchanged), never prompts, so it cannot
  hang under cron or the gateway.
- **Writes quarantine files** under the user's home on the machine where the command runs
  (`~/.safe-swap/trash/<id>/`), and one SQLite file in the plugin's own data directory
  (`plugin-data/safe-swap/receipts.db`).
- Reads nothing outside that: no `.env`, no `auth.json`, no credential files. It reads back only the
  receipts it wrote.
- **No network calls, no telemetry, no background processes, no self-update.**
- Public surfaces only: `register_hook`, `register_command`, `register_cli_command`, `ctx.dispatch_tool`.
  No core function is patched or wrapped.
- **Approvals are respected, with one visible consequence.** The rewritten command goes through
  Hermes' normal approval gate. A swapped `rm -rf` is a move, so it no longer matches the
  "recursive delete" pattern and no longer prompts: that is the point of the swap, and it is the one
  place where Safe-Swap changes how often Hermes asks. The git rewrites still contain the original
  words (`git reset --hard`, `git branch -D`, and a lease push still reads as a force push) and are
  prompted as before.
- `git stash create` needs no identity configuration: the snapshot commit is made with a fixed
  throwaway author (`safe-swap`).

## Relationship to Hermes' built-in checkpoints

Hermes' `/rollback` checkpoints cover edits made through the file tools. Safe-Swap covers the side
effects that happen in the **shell**, which checkpoints cannot see: deleted trees, rewritten remote
history, discarded working-tree changes, deleted branches. They are complementary.

## Known gaps

- A quarantine on another filesystem than the data is a copy, not a rename (large trees are slower).
- A path containing a newline or a tab cannot be recorded in the manifest and is not restorable.
- `git clean` is not swapped. Background (`background: true`) commands are swapped, but their
  receipt is marked applied when they start.
- `/unswap` for `git reset --hard` moves HEAD back only when the tree is clean and nobody committed
  since; otherwise it prints the old HEAD and leaves history alone.

## Development

```bash
python -m pytest -q          # unit table, bash+dash behavioural tests, real-git force-push scenarios
```

The integration test (`tests/test_integration.py`) loads the plugin through Hermes' real plugin
loader in an isolated `HERMES_HOME`/`HOME` and is skipped when `hermes_cli` is not importable.
Layout: `shellscan.py` (tokenizer), `rules.py` (what to swap), `shellgen.py` (POSIX text),
`rewriter.py`, `undo.py` (restore/purge scripts), `receipts.py` (SQLite), `runtime.py` (hooks),
`commands.py` (slash + CLI), `config.py`.

MIT licensed.
