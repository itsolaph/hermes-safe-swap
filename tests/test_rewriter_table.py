"""Table-driven tests for the rewriter: command in, rewrite (or None) out. No Hermes import."""

from __future__ import annotations

import pytest

from safe_swap.config import Config
from safe_swap.rewriter import MAX_COMMAND_LEN, extract_receipt_id, rewrite

RID = "abc123"
LEASE = "--force-with-lease --force-if-includes"

# (command, config overrides) that must pass through untouched.
UNTOUCHED = [
    "echo hi",
    "ls -la",
    "rm",
    "rm -rf",
    "rm -rf /",
    "rm -rf //",
    "rm -rf ~",
    "rm -rf ~/",
    "rm -rf $HOME",
    "rm -rf ${HOME}/*",
    "rm -rf /*",
    "rm -rf .",
    "rm -rf ..",
    "rm -rf ./",
    "rm -rf build/..",
    "rm -rf /tmp/x",
    "rm -rf /var/tmp/a /tmp/b",
    "rm -rf $TMPDIR/foo",
    "rm -rf /etc/nginx",
    "rm -rf /usr",
    "rm -rf /home/someone",
    "rm -rf ~/.safe-swap/trash/abc123",
    "sudo rm -rf build",
    "sudo git push --force",
    "rm -rfi build",
    "rm -i file",
    "rm --interactive=always x",
    "rm --no-preserve-root -rf x",
    "rm -d emptydir",
    "rm --one-file-system -rf x",
    "bash -c 'rm -rf x'",
    "sh -c \"rm -rf x\"",
    "bash -lc 'rm -rf x'",
    "eval 'rm -rf x'",
    "ls | xargs rm -rf",
    "find . -name '*.o' -exec rm {} +",
    "find . -name '*.o' -delete",
    "source ./cleanup.sh; rm -rf build",
    "rm -rf a\nrm -rf b",
    "cat <<EOF\nrm -rf x\nEOF",
    "cat <<< 'x'; rm -rf y",
    "rm -rf `pwd`/x",
    "rm -rf $(pwd)/x",
    "rm -rf \"$(pwd)/x\"",
    "(rm -rf x)",
    "{ rm -rf x; }",
    "rm -rf x &",
    "diff <(rm -rf x) y",
    "rm -rf 'unterminated",
    "rm -rf x &&",
    "rm -rf x ;; echo",
    "# rm -rf x",
    "rm -rf x # trailing comment",
    "echo \"rm -rf x\"",
    "grep rm file.txt",
    "env rm -rf x",
    "rm -rf x\\\ny",
    "git push",
    "git push origin main",
    "git push --force-with-lease origin main",
    "git push --force-with-lease=main:abc origin main",
    "git push --force --force-with-lease origin main",
    "git push -f --force-with-lease origin main",
    "git push --force-if-includes origin main",
    "git push --no-force-with-lease -f origin main",
    "git push -f origin :old-branch",
    "git push --mirror -f",
    "git push --delete -f origin x",
    "git push --force=yes origin main",
    "git reset --soft HEAD~1",
    "git reset HEAD file",
    "git reset --mixed",
    "git checkout main",
    "git checkout -b feature",
    "git restore --staged file",
    "git restore",
    "git branch",
    "git branch -d merged",
    "git branch -D",
    "git branch -D $NAME",
    "git branch -a -D x",
    "git branch -m old new",
    "git clean -fd",
    "git status",
    "git log --oneline | head",
    "git $SUB push -f",
    "git -x push -f",
    "git stash drop",
]

CONFIG_OFF = [
    ("rm -rf build", {"rule_rm": False}),
    ("git push -f", {"rule_git_push": False}),
    ("git reset --hard", {"rule_git_reset": False}),
    ("git checkout -- .", {"rule_git_restore": False}),
    ("git restore f", {"rule_git_restore": False}),
    ("git branch -D x", {"rule_git_branch": False}),
    ("rm file.txt", {"swap_single_rm": False}),
    ("git push -f origin wip/topic", {"trusted_force_branches": ("wip/*",)}),
    ("git push origin +wip/a:wip/a", {"trusted_force_branches": ("wip/*",)}),
    ("git push -f origin main HEAD:scratch", {"trusted_force_branches": ("main", "scratch")}),
]


@pytest.mark.parametrize("command", UNTOUCHED)
def test_untouched(command: str) -> None:
    assert rewrite(command, Config(), RID) is None


@pytest.mark.parametrize(("command", "overrides"), CONFIG_OFF)
def test_config_switches(command: str, overrides: dict) -> None:
    assert rewrite(command, Config(**overrides), RID) is None


PUSH = [
    ("git push -f", f"git push {LEASE}"),
    ("git push --force", f"git push {LEASE}"),
    ("git push --force origin main", f"git push {LEASE} origin main"),
    ("git push -fu origin x", f"git push -u {LEASE} origin x"),
    ("git push -uf origin x", f"git push -u {LEASE} origin x"),
    ("git push -v --force origin x", f"git push -v {LEASE} origin x"),
    ("git push origin +main", f"git push {LEASE} origin main"),
    ("git push origin +HEAD:main", f"git push {LEASE} origin HEAD:main"),
    ("git push origin main +dev:dev", f"git push {LEASE} origin main dev:dev"),
    ("git push --force-with-lease origin +main", "git push --force-with-lease origin main"),
    ("git -C repo push -f", f"git -C repo push {LEASE}"),
    ("git -c core.sshCommand=ssh push -f", f"git -c core.sshCommand=ssh push {LEASE}"),
    ("FOO=1 git push -f", f"FOO=1 git push {LEASE}"),
    ("/usr/bin/git push --force", f"/usr/bin/git push {LEASE}"),
    ("git push -f && echo ok", f"git push {LEASE} && echo ok"),
    ("cd repo && git push -f origin main | tee log", f"cd repo && git push {LEASE} origin main | tee log"),
    ("git push -o ci.skip -f", f"git push -o ci.skip {LEASE}"),
    ("git push -f $REMOTE $BRANCH", f"git push {LEASE} $REMOTE $BRANCH"),
]


@pytest.mark.parametrize(("command", "expected"), PUSH)
def test_push(command: str, expected: str) -> None:
    result = rewrite(command, Config(), RID)
    assert result is not None
    assert result.command == expected
    assert result.kinds == ("push",)


# (command, kind, fragments that must appear in the rewritten command)
SWAPS = [
    ("rm -rf build", "rm", ["SS_RCPT=abc123", "for SS_P in build;"]),
    ("rm -fr build", "rm", ["for SS_P in build;"]),
    ("rm -r -f build", "rm", ["for SS_P in build;"]),
    ("rm -Rf build", "rm", ["for SS_P in build;"]),
    ("rm --recursive --force build", "rm", ["for SS_P in build;"]),
    ("rm -rv build", "rm", ["for SS_P in build;"]),
    ("/bin/rm -rf build", "rm", ["for SS_P in build;"]),
    ("/usr/bin/rm -rf build", "rm", ["for SS_P in build;"]),
    ("\\rm -rf build", "rm", ["for SS_P in build;"]),
    ("FOO=1 rm -rf build", "rm", ["for SS_P in build;"]),
    ('rm -rf "my dir"', "rm", ['for SS_P in "my dir";']),
    ("rm -rf 'it'\"'\"'s'", "rm", ["for SS_P in 'it'\"'\"'s';"]),
    ("rm -rf -- -weird", "rm", ["for SS_P in -weird;"]),
    ("rm -rf $BUILD_DIR", "rm", ["for SS_P in $BUILD_DIR;"]),
    ('rm -rf "$BUILD_DIR/out"', "rm", ['for SS_P in "$BUILD_DIR/out";']),
    ("rm -rf build/*", "rm", ["for SS_P in build/*;"]),
    ("rm *.log", "rm", ["for SS_P in *.log;", "Is a directory"]),
    ("rm file.txt", "rm", ["for SS_P in file.txt;", "No such file or directory"]),
    ("rm -f file.txt", "rm", ["for SS_P in file.txt;", "else :; fi"]),
    ("rm -rf a b c", "rm", ["for SS_P in a b c;"]),
    ("rm -rf do", "rm", ["for SS_P in ./do;"]),
    ("rm -rf ~/proj/build", "rm", ["for SS_P in ~/proj/build;"]),
    ("rm -rf /var/tmp/x ../build", "rm", ["for SS_P in /var/tmp/x ../build;"]),
    ("cd x && rm -rf y | tee log", "rm", ["cd x && { SS_RCPT=", "for SS_P in y;", "} | tee log"]),
    ("rm -rf build 2>/dev/null", "rm", ["} 2>/dev/null"]),
    ("rm -rf build >/dev/null 2>&1; echo done", "rm", ["} >/dev/null 2>&1; echo done"]),
    ("rm -rf build || true", "rm", ["} || true"]),
    ("git reset --hard", "reset", ["git stash create", "git reset --hard; }"]),
    ("git reset --hard HEAD~3", "reset", ["git reset --hard HEAD~3; }"]),
    ("git reset -q --hard origin/main", "reset", ["git reset -q --hard origin/main; }"]),
    ("git -C d reset --hard", "reset", ["git -C d stash create", "git -C d reset --hard; }"]),
    ("git checkout -- .", "checkout", ["git checkout -- .; }"]),
    ("git checkout -- a b", "checkout", ["git checkout -- a b; }"]),
    ("git checkout HEAD -- f", "checkout", ["git checkout HEAD -- f; }"]),
    ("git checkout .", "checkout", ["git checkout .; }"]),
    ("git restore f", "restore", ["git restore f; }"]),
    ("git restore --source=HEAD~1 f", "restore", ["git restore --source=HEAD~1 f; }"]),
    ("git restore --staged --worktree f", "restore", ["stash create"]),
    ("git restore -SW f", "restore", ["stash create"]),
    ("git branch -D foo", "branch", ["for SS_B in foo;", "git branch -D foo; }"]),
    ("git branch -D a b", "branch", ["for SS_B in a b;"]),
    ("git branch --delete --force foo", "branch", ["for SS_B in foo;"]),
    ("git branch -df foo", "branch", ["for SS_B in foo;"]),
]


@pytest.mark.parametrize(("command", "kind", "fragments"), SWAPS)
def test_swaps(command: str, kind: str, fragments: list[str]) -> None:
    result = rewrite(command, Config(), RID)
    assert result is not None, command
    assert result.kinds == (kind,)
    assert result.original == command
    for fragment in fragments:
        assert fragment in result.command, (fragment, result.command)
    assert "\n" not in result.command


@pytest.mark.parametrize("command", [c for c, *_ in SWAPS] + [c for c, _ in PUSH])
def test_idempotent(command: str) -> None:
    first = rewrite(command, Config(), RID)
    assert first is not None
    assert rewrite(first.command, Config(), "def456") is None


def test_two_rm_segments_use_separate_slots() -> None:
    result = rewrite("rm -rf a && rm -rf b", Config(), RID)
    assert result is not None
    assert 'trash/abc123"; SS_N=0' in result.command
    assert 'trash/abc123.1"; SS_N=0' in result.command


def test_mixed_kinds_are_all_swapped() -> None:
    result = rewrite("rm -rf out; git reset --hard; git push -f", Config(), RID)
    assert result is not None
    assert result.kinds == ("rm", "reset", "push")
    assert LEASE in result.command


def test_custom_quarantine_dir() -> None:
    result = rewrite("rm -rf x", Config(quarantine_dir="/var/q"), RID)
    assert result is not None and 'SS_Q="/var/q/abc123"' in result.command


@pytest.mark.parametrize("bad", [None, 5, b"rm -rf x", ["rm"], {"a": 1}, "", "   "])
def test_garbage_input(bad: object) -> None:
    assert rewrite(bad, Config(), RID) is None


def test_size_cap() -> None:
    big = "rm -rf " + "a" * MAX_COMMAND_LEN
    assert rewrite(big, Config(), RID) is None
    assert rewrite("rm -rf " + "a" * 1000, Config(), RID) is not None


def test_extract_receipt_id() -> None:
    result = rewrite("rm -rf x", Config(), RID)
    assert result is not None
    assert extract_receipt_id(result.command) == RID
    assert extract_receipt_id("rm -rf x") is None
    assert extract_receipt_id(None) is None
