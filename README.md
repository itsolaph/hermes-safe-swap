# Safe-Swap for Hermes Agent

**Safe-Swap never blocks and never nags.** When the agent runs a destructive shell command, a
`pre_tool_call` hook silently swaps it for a safe twin *before it runs*, and keeps a receipt. The
point is the strategy: instead of copying files and hoping to put them back after the fact, the
command itself is rewritten so the damage cannot happen, or can be undone exactly.

- **`git push --force` becomes `--force-with-lease --force-if-includes`.** It still overwrites your own
  history, but it can no longer destroy a teammate's commits it has not seen. Nothing that snapshots
  files can offer that: a remote cannot be restored from a local copy.
- **`rm -r build` becomes a move into a quarantine folder**, with the number of files and bytes
  that moved, so the agent (and you) see the blast radius.
- **`git reset --hard`, `git checkout -- <paths>`, `git restore <paths>`, `git clean -f…` and
  `git stash drop/clear` take a private snapshot first**, and `git branch -D` records the branch tip.
  Those git rules are not covered by the other undo tools for Hermes (see the comparison below).

`/swaps` lists the receipts and `/unswap <id>` undoes them. The agent is told what really ran, so it
never reasons from a false picture. It is also told when a destructive command was **not** swapped, so
it never assumes protection it does not have.

## Demo

Real output from one run through the Hermes plugin loader (sandbox paths shortened; the teammate pushed a commit the agent never fetched):

```text
$ git push --force origin main
To ~/demo/remote.git
 ! [rejected]        main -> main (stale info)
error: failed to push some refs to '~/demo/remote.git'
[safe-swap] `git push --force origin main` was run as: git push --force-with-lease --force-if-includes (not --force) (receipt 4dfef3). Not undoable (the lease only refuses unsafe pushes).
[exit 1]

$ rm -r build
[safe-swap] `rm -r build` was run as: move-to-quarantine (4002 files, 10 MB, receipt 4dc78a). Undo: /unswap 4dc78a

$ git clean -fd
Removing notes.txt
Removing scratch.txt
Removing scratch/
Removing tmp_dir/
[safe-swap] `git clean -fd` was run as: preceded by a snapshot of the untracked files (receipt 664434). Undo: /unswap 664434

$ git stash drop
Dropped refs/stash@{0} (55a7a4add0af30ad725aabed754885f3f83f1793)
[safe-swap] `git stash drop` was run as: preceded by a record of the dropped stash entries (receipt 04405f). Undo: /unswap 04405f

$ rm -r $(echo nothing-here)
rm: cannot remove 'nothing-here': No such file or directory
[safe-swap] NOT swapped (unsupported syntax: command substitution / arithmetic): rm -r $(echo nothing-here)
[exit 1]

> /swaps
id      age   status   kind      size              command
04405f  now   applied  stash     -                 git stash drop
664434  now   applied  clean     -                 git clean -fd
4dc78a  now   applied  rm        4002 files, 10 MB rm -r build
4dfef3  now   failed   push      -                 git push --force origin main
Undo with /unswap <id> (or /unswap last). /swaps purge [--older 7d] frees quarantine space.

> /unswap 4dc78a
Undone 4dc78a.
restored: ~/demo/project/build
```

`demo/demo.sh` builds a throwaway scenario (project, build dir, bare remote, a teammate commit) and
prints the commands to hand to the agent.

## Install

```bash
hermes plugins install itsolaph/hermes-safe-swap --enable
```

Requires Hermes `>=0.23.0`, a POSIX `sh` on the machine where commands run, and git `>=2.30`
for the push rule (`--force-if-includes`).

## What gets swapped

| Command | Runs as | Undo |
|---|---|---|
| `git push --force`, `-f`, `+refspec` | `--force-with-lease --force-if-includes` (an existing lease is left alone) | none: a lease only refuses pushes that would destroy someone else's work |
| `rm -r…`, `rm` with a glob, `rm file` | each target is moved into the quarantine folder (`<id>/<n>`); absolute paths go in `manifest.tsv`. Exit status, `-f` and "No such file" behave like `rm`. Files and bytes are counted first | `/unswap <id>` moves everything back (refuses to overwrite unless `--force`) |
| `git reset --hard [rev]` | `git stash create` + a private ref `refs/safe-swap/<id>` first (the tree and your stash stack are not touched), then the original | `/unswap` moves HEAD back if nothing happened since, then re-applies the snapshot |
| `git checkout -- <paths>`, `git checkout .`, `git restore <paths>` (not `--staged` only) | same snapshot, then the original | `/unswap` re-applies the snapshot |
| `git clean -f`, `-fd`, `-fdx`, `-fX` | the untracked files (and the ignored ones with `-x`/`-X`) are committed to `refs/safe-swap/<id>` through a throwaway index, then the **original** `git clean` runs. `git clean -n` / `--dry-run` is left alone | `/unswap` writes back the files that are missing (never overwrites a file with different content unless `--force`) |
| `git stash drop [<stash>]`, `git stash clear` | the dropped stash commit(s) are kept reachable under `refs/safe-swap/<id>`, then the original | `/unswap` runs `git stash store` for each, oldest first. A dropped `stash@{n}` comes back as `stash@{0}` |
| `git branch -D <b>…` | records each branch tip first | `/unswap` runs `git branch <b> <sha>` |

Every rewrite is one grouped command (`{ …; }`) with the original's exit status, so `&&`, `||`,
`|` and `;` around it mean what they meant before. Redirections are kept (they apply to the work
itself; Safe-Swap's own marker lines are printed outside them). Compound commands such as
`cd x && rm -r y | tee log` are rewritten segment by segment. Receipts are plain files, so
recovery works by hand (`mv` the numbered entry back to the path in `manifest.tsv`, or
`git stash store <sha>` / `git checkout <ref> -- <path>` for the private refs) even without the plugin.

## What the agent is told

Every terminal result that Safe-Swap touched gets lines appended:

- **A swap happened:** ``[safe-swap] `rm -r build` was run as: move-to-quarantine (312 files, 14 MB, receipt ff0db2). Undo: /unswap ff0db2``.
  The numbers are the blast radius, counted before the move (at most 50,000 files or 2 seconds, then
  it says `50000+ files`). If the quarantine move had to copy across filesystems the note says
  `copied across filesystems`.
- **A destructive command was recognised but left untouched:**
  `[safe-swap] NOT swapped (<short reason>): <first 80 characters of the command>`. The reasons are:
  a protected target (`/`, home, system directories, temp paths), a wrapper (`sudo`, `env`, `xargs`,
  `find -exec`), a command string (`bash -c`, `eval`), shell syntax it will not guess about (command
  substitution, heredoc, subshell, newline, background `&`), an unsupported option, a receipt that
  could not be written, and a snapshot or quarantine that could not be created when the command ran.
  Only commands that really look destructive (`rm -r`, `git reset --hard`, `git push --force`,
  `git clean`, `git checkout --`, `git restore`, `git branch -D`, `git stash drop/clear`) ever get this
  line; `echo "rm -r x"` and `git log --grep reset` never do, and neither does anything you configured
  away (a rule switched off, `trusted_force_branches`).

A failed snapshot or quarantine replaces the "was run as" line, so the agent is never told a command was
protected when it was not.

## What it deliberately does NOT swap

Safe-Swap swaps only what it fully understands; everything else runs **untouched**, and Hermes'
own approval system stays in charge. It is not a guard.

- `/`, `~`, `$HOME`, `/*`, `.`, `..`, and every top-level absolute directory (`/etc`, `/usr`, `/nix`,
  `/workspace`, …): moving those into a quarantine is nonsense, and Hermes hard-blocks several of them,
  so Hermes' own handling decides. A test sweeps every hardline variant to prove none is ever rewritten.
- Targets under `/tmp`, `/var/tmp`, `$TMPDIR`, and anything touching `.safe-swap` itself.
- `sudo rm …` / `sudo git …` and other wrappers (`env`, `nohup`, `xargs`, `find -exec/-delete`,
  `bash -c`, `eval`, `source`): root-owned paths cannot be quarantined correctly as the caller.
- Anything with a newline outside quotes, heredocs, backticks, `$(…)`, subshells, `{ }` groups,
  background `&`, process substitution, comments, or unknown `rm` flags (`-i`, `-d`,
  `--no-preserve-root` …). In each of these cases that looks destructive, the agent gets a `NOT swapped` line.
- `git clean -i` (interactive) and `git clean -ff` (nested repositories cannot be snapshotted),
  `git push --mirror/--delete`, deleting refspecs (`:branch`), and a push that already mixes
  `--force` with a lease.
- Branches listed in `trusted_force_branches`.

## What it will not do

Safe-Swap is narrow on purpose. These all run exactly as written and are **not** covered:

- `mv` over an existing file, and `cp` that overwrites
- `sed -i` and other in-place edits
- `>` redirects that truncate a file, and `tee`
- the `write_file` and `patch` tools
- `find -delete`
- `git rebase`

Pair it with `undo-effects` (a catalog plugin, `hermes plugins install undo-effects`, that copies the
files those commands are about to change) or with Hermes' own `/rollback` checkpoints for them.

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
| `rule_git_clean` | `true` | snapshot the untracked files before `git clean` |
| `rule_git_stash` | `true` | record the stash entries before `git stash drop` / `clear` |
| `trusted_force_branches` | `[]` | glob patterns of destination branches where force push is left alone, e.g. `[wip/*]` |
| `quarantine_dir` | `~/.safe-swap/trash` | absolute path or `~/path`, on the machine where the command runs |
| `note_to_agent` | `true` | append the one-line "what really ran" note to the tool result |
| `notify_unswapped` | `true` | append the `NOT swapped` line when a destructive command was left untouched (independent of `note_to_agent`) |

## Commands

- `/swaps`: last 10 receipts, with the number of files and bytes an `rm` moved. `/swaps purge [--older 7d]`:
  delete quarantine data and snapshot refs. **This is the only thing that ever deletes quarantine
  data; nothing is purged automatically.**
- `/unswap <id|last> [--force]`: restore. Runs through the session's own terminal backend, so a
  receipt from a docker or ssh sandbox is restored there.
- `hermes safe-swap list|undo <id>|purge [--older 7d]`: the same for the local backend.

## What it touches (disclosure)

- **Rewrites `terminal` commands before they execute** (`pre_tool_call`, `modify`); the hook never
  blocks, never raises (any error or doubt means the command runs unchanged) and never prompts, so it
  cannot hang under cron or the gateway.
- `/unswap` and `/swaps purge` run a generated shell script through the session's terminal tool, so
  they go through Hermes' normal approval gate like any other command (a purge is an `rm -r`, a
  `git reset --hard` undo is a hard reset). Where Hermes asks (for example in the gateway) you will be
  asked; if the gate does not let the script run, the command says so instead of waiting. The local
  `hermes safe-swap` CLI runs the same scripts directly.
- **Writes quarantine files** under the user's home on the machine where the command runs
  (`~/.safe-swap/trash/<id>/`), and one SQLite file in the plugin's own data directory
  (`plugin-data/safe-swap/receipts.db`). An older receipts file is upgraded in place.
- **Writes git objects into the repository you are working in.** `git clean`, `git reset --hard` and
  `git stash drop` snapshots are commits kept alive by private refs `refs/safe-swap/<id>` (your branches,
  index, stash stack and working tree are not touched). A `git clean` snapshot copies the content of the
  files clean may delete into `.git/objects` (untracked files always, ignored files with `-x`/`-X`)
  until you `/swaps purge` and git collects the garbage. Sets above 20,000 files or 200 MB are not
  snapshotted; the command then runs unprotected and says so in a `NOT swapped` line.
- **Prints one marker line to stderr** for an `rm` swap (`SS_STATS <id> <files> <bytes> …`, outside any
  redirection of yours); the `transform_tool_result` hook removes it from the output the agent sees.
- Reads nothing outside that: no `.env`, no `auth.json`, no credential files. It reads back only the
  receipts it wrote. (It lists file sizes, never their content, to measure the blast radius.)
- **No network calls, no telemetry, no background processes, no self-update.**
- Public surfaces only: `register_hook`, `register_command`, `register_cli_command`, `ctx.dispatch_tool`.
  No core function is patched or wrapped.
- **Approvals are respected, with one visible consequence.** The rewritten command goes through
  Hermes' normal approval gate. A swapped `rm -r` is a move, so it no longer matches the
  "recursive delete" pattern and no longer prompts: that is the point of the swap, and it is the one
  place where Safe-Swap changes how often Hermes asks. The git rewrites still contain the original
  words (`git reset --hard`, `git branch -D`, `git clean`, and a lease push still reads as a force push)
  and are prompted as before.
- `git stash create` needs no identity configuration: the snapshot commit is made with a fixed
  throwaway author (`safe-swap`). `purge` deletes the private refs together with the quarantine folder.

## Relationship to other undo tools

Three things in the Hermes ecosystem can help after an agent runs a destructive command. They differ in
mechanism, so they cover different damage and are meant to be combined.

| | **safe-swap** | **undo-effects** (catalog plugin) | **core `/rollback`** (checkpoints) |
|---|---|---|---|
| Mechanism | `pre_tool_call` hook **rewrites the command** into a safe twin (move, lease, snapshot-then-run) | `tool_execution` middleware **copies the files** a covered call will change into a local store, then lets it run | opt-in (`--checkpoints`, default off) **shadow-git snapshot** of the working directory, at most once per directory per turn |
| What runs | the rewritten twin, not the original | the original command, unchanged | the original command, unchanged |
| `git push --force` | **yes**: becomes `--force-with-lease --force-if-includes`, so it cannot overwrite unseen commits | no (it saves files; a remote is not a file) | no: can restore local files, cannot stop or undo a remote force-push |
| `git reset --hard` / `git clean` | **yes**: snapshot of tracked changes (reset, checkout, restore) or of the untracked files (clean); also `git stash drop/clear` and `git branch -D` | not part of its documented coverage | working-directory snapshot before `git reset`/`clean`/`checkout`; `git stash drop` is not a listed trigger |
| `mv` / `sed -i` / `write_file` | **no**, deliberately | yes (also `cp`, redirects, `tee`, `find -delete`, `patch`) | yes (also `rm`, `cp`, redirects, `patch`), when checkpoints are enabled |
| Restore path | `/unswap <id>`; or by hand from plain files and `refs/safe-swap/*` | `/undo-effects` | `/rollback <N>` |

Core `/rollback` checkpoints are not limited to the file tools: with checkpoints enabled they also
snapshot before destructive terminal commands. What a snapshot cannot do is stop damage outside the working
tree, such as a remote force-push, and it only helps if you enabled it first. `undo-effects` also sees
the command before it runs; what sets Safe-Swap apart is the **strategy** (make the command safe instead of
snapshotting after the fact) and the git rules nothing else covers: force-push to lease, and
reset / checkout / restore / clean / stash drop snapshots.

## Known gaps

- A quarantine on another filesystem than the data is a copy, not a rename (large trees are slower);
  the note says so. Detection compares inodes and stays silent when unsure.
- The blast radius is measured with `find`, `ls` and `awk`; the 2-second limit needs `timeout` on the
  machine (without it only the 50,000-entry cap applies). A path containing a newline can add a bogus
  line to the count, never hide a file.
- A path containing a newline or a tab cannot be recorded in the `rm` manifest and is not restorable
  that way. (`git clean` snapshots do not have this limit: file names never pass through the shell.)
- `git clean`: empty directories are not restored (git cannot store them); ignored trees hashed by
  `-x` stay in `.git/objects` until purge and garbage collection; if the quarantine directory is inside
  the repository the clean runs unprotected and says so.
- A dropped `stash@{n}` returns as `stash@{0}`, not at its old position.
- Background (`background: true`) commands are swapped, but their receipt is marked applied when they
  start, and their blast radius is not reported.
- `/unswap` for `git reset --hard` moves HEAD back only when the tree is clean and nobody committed
  since; otherwise it prints the old HEAD and leaves history alone.
- On container backends (docker, ssh) the quarantine lives in that container's `$HOME`: bind-mounted
  host files are copied there, and a recreated sandbox loses the quarantine.
- Tested with Hermes' default in-process plugin loading; `plugins.isolation: host` was not exercised.

## Development

```bash
python -m pytest -q          # unit tables, bash+dash behavioural tests, real-git scenarios
```

The integration test (`tests/test_integration.py`) loads the plugin through Hermes' real plugin
loader in an isolated `HERMES_HOME`/`HOME` and is skipped when `hermes_cli` is not importable.
The suite points `HERMES_HOME` at a throwaway directory for the whole run.
Layout: `shellscan.py` (tokenizer), `rules.py` (what to swap), `destructive.py` (does it look
destructive, for the notice), `shellgen.py` (POSIX text), `rewriter.py`, `undo.py` (restore/purge
scripts), `receipts.py` (SQLite), `runtime.py` (hooks and notes), `commands.py` (slash + CLI),
`config.py`.

MIT licensed.
