#!/usr/bin/env bash
# Builds a throwaway sandbox for the Safe-Swap demo and prints the three commands to hand to the agent.
# Nothing outside the sandbox directory is touched. Usage: demo/demo.sh [sandbox-dir]
set -euo pipefail

SANDBOX="${1:-$(mktemp -d "${TMPDIR:-/tmp}/safe-swap-demo.XXXXXX")}"
mkdir -p "$SANDBOX"
export GIT_CONFIG_GLOBAL="$SANDBOX/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global init.defaultBranch main
git config --global user.name "demo"
git config --global user.email "demo"

git init -q --bare "$SANDBOX/remote.git"

# The project the agent works in.
git clone -q "$SANDBOX/remote.git" "$SANDBOX/project" 2>/dev/null
cd "$SANDBOX/project"
mkdir -p src build/assets
printf 'def greet():\n    return "hello"\n' > src/app.py
printf 'compiled output\n' > build/app.bin
printf 'logo\n' > build/assets/logo.png
printf 'build/\n' > .gitignore
git add -A && git commit -qm "initial import" && git push -q -u origin main

# A teammate pushes a commit the agent has not seen.
git clone -q "$SANDBOX/remote.git" "$SANDBOX/teammate" 2>/dev/null
(cd "$SANDBOX/teammate" && printf 'teammate fix\n' > fix.txt && git add fix.txt && git commit -qm "teammate: important fix" && git push -q origin main)

# The agent rewrites its own last commit (so a plain force push would erase the teammate's fix)
# and has uncommitted work that "git reset --hard" would destroy.
git commit -q --amend -m "initial import (reworded)"
printf 'def greet():\n    return "hello, world"  # half-finished edit\n' > src/app.py

cat <<MSG

Sandbox ready: $SANDBOX/project

Start Hermes with Safe-Swap enabled, work in that directory, and ask the agent to run:

  1) rm -rf build
  2) git push --force origin main
  3) git reset --hard

Then show the receipts and undo them:

  /swaps
  /unswap <id of the rm receipt>     # build/ comes back, byte for byte
  /unswap <id of the reset receipt>  # the half-finished edit comes back

What to point out on camera:
  - (1) did not delete anything: build/ was moved into ~/.safe-swap/trash/<id>
  - (2) was refused: the remote holds a teammate commit the agent never fetched
  - (3) kept the edit: it is saved under refs/safe-swap/<id> (git for-each-ref refs/safe-swap)
Clean up with:  rm -rf "$SANDBOX"
MSG
