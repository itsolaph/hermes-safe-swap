"""The visible "NOT swapped" notice: when it appears, what it says, and when it must stay silent."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from safe_swap.config import Config, load_config
from safe_swap.destructive import argv_destructive, text_destructive
from safe_swap.rewriter import analyze
from safe_swap.runtime import Runtime, not_swapped_line

RID = "abc123"

# (command, fragment of the first skip reason). One row per reason class.
REASONS = [
    # protected targets (hardline): / and home and system directories
    ("rm -rf /", "protected path"),
    ("rm -rf ~", "protected path"),
    ("rm -rf $HOME", "protected path"),
    ("rm -rf /*", "protected path"),
    ("rm -rf /etc/nginx", "protected path"),
    ("rm -rf .", "protected path"),
    ("rm -rf ~/.safe-swap/trash/abc123", "protected path"),
    # temp paths are deliberately not quarantined
    ("rm -rf /tmp/x", "temp path"),
    # wrappers
    ("sudo rm -rf build", "wrapped in sudo"),
    ("env rm -rf x", "wrapped in env"),
    ("nohup rm -r x", "wrapped in nohup"),
    ("sudo git push --force", "wrapped in sudo"),
    # unsupported quoting: command strings, eval, xargs, find -exec
    ("bash -c 'rm -rf x'", "bash -c command string"),
    ("sh -c \"git reset --hard\"", "sh -c command string"),
    ("eval 'rm -rf x'", "eval wrapper"),
    ("ls | xargs rm -rf", "xargs wrapper"),
    ("find . -name '*.o' -exec rm -r {} +", "find -exec"),
    # parse doubt: syntax the scanner refuses
    ("rm -rf $(pwd)/x", "unsupported syntax: command substitution"),
    ("rm -rf `pwd`/x", "unsupported syntax: backtick"),
    ("rm -rf a\nrm -rf b", "unsupported syntax: newline"),
    ("rm -rf x &", "unsupported syntax: background job"),
    ("(rm -rf x)", "unsupported syntax: subshell"),
    ("rm -rf x # why", "unsupported syntax: comment"),
    ("bash <<EOF\nrm -rf x\nEOF", "unsupported syntax: heredoc"),
    ("git reset --hard $(git rev-parse HEAD~1)", "unsupported syntax: command substitution"),
    ("git push -f origin \"$(cat branch)\"", "unsupported syntax: command substitution"),
    ("rm -rf 'unterminated", "unsupported syntax: unterminated single quote"),
    # unsupported options and forms
    ("rm -rfi build", "unsupported rm option -i"),
    ("rm --no-preserve-root -rf x", "unsupported rm option --no-preserve-root"),
    ("git push -f origin :old-branch", "push deletes a ref"),
    ("git push --force --force-with-lease origin main", "push mixes --force with a lease"),
    ("git push --mirror -f", "push uses --mirror"),
    ("git branch -D $NAME", "git branch -D"),
    ("git branch -a -D x", "git branch -D"),
    ("git clean -fd -i", "interactive git clean"),
    ("git clean -ffd", "git clean -ff"),
    ("git clean -fd --bogus", "unsupported git clean option --bogus"),
    ("git stash drop a b", "unsupported git stash drop form"),
    ("git stash clear x", "unsupported git stash clear form"),
    ("git -x push -f", "unsupported git option -x"),
]

# Destructive-looking only to a careless matcher, or configured away: never a notice.
HARMLESS = [
    "echo hi", "ls -la", "git status", "rm -f /etc/hosts", "rm /*", "make clean",
    "echo \"rm -rf x\"", "echo 'git reset --hard'", "grep -r 'rm -rf' docs", "git log --grep 'reset --hard'",
    "git commit -m 'rm -rf the docs'", "man rm", "cat <<EOF\nrm -rf x\nEOF", "echo $(date) rm -rf x",
    "git push", "git push --force-with-lease origin main", "git push origin main", "git reset --soft HEAD~1",
    "git checkout main", "git checkout -b feature", "git restore --staged f", "git branch -d merged",
    "git clean -n", "git clean -nd", "git clean --dry-run -fd", "git stash", "git stash pop", "git stash list",
    "git stash push -m x", "rm", "rm -i file", "find . -name '*.o'", "find . -exec ls {} +", "git $SUB push -f",
    "bash -c 'echo hi'", "bash ./script.sh", "ls | xargs echo", "eval 'echo hi'", "sudo ls /root", "rm -rf",
]

# Left alone because the user configured it that way: silent.
CONFIGURED = [
    ("rm -rf build", dict(rule_rm=False)),
    ("rm file.txt", dict(swap_single_rm=False)),
    ("git push -f origin wip/topic", dict(trusted_force_branches=("wip/*",))),
    ("git push -f", dict(rule_git_push=False)),
    ("git reset --hard", dict(rule_git_reset=False)),
    ("git checkout -- .", dict(rule_git_restore=False)),
    ("git branch -D x", dict(rule_git_branch=False)),
    ("git clean -fd", dict(rule_git_clean=False)),
    ("git stash drop", dict(rule_git_stash=False)),
]


@pytest.mark.parametrize(("command", "fragment"), REASONS)
def test_destructive_command_left_untouched_says_why(command: str, fragment: str) -> None:
    a = analyze(command, Config(), RID)
    assert a.rewrite is None, command
    assert a.skips and fragment in a.skips[0], (command, a.skips)


@pytest.mark.parametrize("command", HARMLESS)
def test_harmless_commands_never_produce_a_notice(command: str) -> None:
    a = analyze(command, Config(), RID)
    assert a.skips == () and a.rewrite is None, command


@pytest.mark.parametrize(("command", "overrides"), CONFIGURED)
def test_configured_exclusions_are_silent(command: str, overrides: dict) -> None:
    a = analyze(command, Config(**overrides), RID)
    assert a.rewrite is None and a.skips == ()


def test_swapped_commands_have_no_skips() -> None:
    for command in ("rm -rf build", "git reset --hard", "git push -f", "git clean -fd", "git stash drop"):
        a = analyze(command, Config(), RID)
        assert a.rewrite is not None and a.skips == (), command


def test_mixed_command_reports_the_skipped_part_and_still_swaps_the_rest() -> None:
    a = analyze("rm -r build && rm -rf / ; git push -f", Config(), RID)
    assert a.rewrite is not None and a.rewrite.kinds == ("rm", "push")
    assert len(a.skips) == 1 and "protected path" in a.skips[0]
    assert "rm -rf / ;" in a.rewrite.command  # the protected segment is still there, untouched


def test_already_rewritten_and_own_scripts_are_silent() -> None:
    from safe_swap.undo import purge_script, restore_script

    rewritten = analyze("rm -rf build", Config(), RID).rewrite.command
    assert analyze(rewritten, Config(), "def456") == analyze("ls", Config(), RID)
    for script in (restore_script('"$HOME/.safe-swap/trash"', "abc123"), purge_script('"$HOME/.safe-swap/trash"', ["abc123"])):
        a = analyze(script, Config(), RID)
        assert a.skips == () and a.rewrite is None


def test_oversized_destructive_command_is_reported() -> None:
    from safe_swap.rewriter import MAX_COMMAND_LEN

    a = analyze("rm -rf x " + "a" * MAX_COMMAND_LEN, Config(), RID)
    assert a.skips == ("command too long",)
    assert analyze("echo " + "a" * MAX_COMMAND_LEN, Config(), RID).skips == ()


@pytest.mark.parametrize("bad", [None, 5, b"rm -rf /", ["rm"], {"a": 1}, "", "   "])
def test_analyze_never_raises(bad: object) -> None:
    assert analyze(bad, Config(), RID).skips == ()


# ------------------------------------------------------------------ the classifier on its own


@pytest.mark.parametrize("argv", [
    ["rm", "-r", "x"], ["rm", "-Rf", "x"], ["/bin/rm", "--recursive", "x"], ["git", "reset", "--hard"],
    ["git", "-C", "d", "push", "-f"], ["git", "push", "origin", "+main"], ["git", "clean", "-fd"],
    ["git", "checkout", "--", "f"], ["git", "restore", "f"], ["git", "branch", "-D", "b"], ["git", "stash", "drop"],
    ["git", "stash", "clear"], ["sudo", "-u", "root", "rm", "-r", "x"], ["xargs", "rm", "-rf"],
    ["find", ".", "-exec", "rm", "-r", "{}", "+"],
])
def test_argv_destructive_true(argv: list[str]) -> None:
    assert argv_destructive(argv)


@pytest.mark.parametrize("argv", [
    ["rm", "x"], ["rm", "--", "-r"], ["git", "log"], ["git", "reset", "--soft"], ["git", "clean", "-n"],
    ["git", "restore", "--staged", "f"], ["git", "stash", "pop"], ["git", "push", "--force-with-lease"],
    ["echo", "rm", "-rf", "x"], ["find", ".", "-exec", "ls", "{}", "+"], [],
])
def test_argv_destructive_false(argv: list[str]) -> None:
    assert not argv_destructive(argv)


def test_text_destructive_heredoc_bodies() -> None:
    assert not text_destructive("cat <<EOF\nrm -rf x\nEOF")
    assert not text_destructive("cat <<'EOF'\ngit reset --hard\nEOF")
    assert text_destructive("bash <<EOF\nrm -rf x\nEOF")
    assert text_destructive("cat <<EOF\nhello\nEOF\nrm -rf x")  # the command after the heredoc still counts


def test_text_destructive_substitutions() -> None:
    assert not text_destructive("echo $(date) rm -rf x")
    assert not text_destructive("echo `date` git reset --hard")
    assert text_destructive("rm -rf $(pwd)/x")
    assert text_destructive("echo $(rm -rf x)")  # the inner command really runs
    assert text_destructive("echo $(echo $(git clean -fd))")


# ------------------------------------------------------------------ the agent-facing line


def test_line_format_flattens_whitespace_and_cuts_at_80_chars() -> None:
    line = not_swapped_line("protected path", "rm  -rf\n\t/ " + "x" * 200)
    assert line.startswith("[safe-swap] NOT swapped (protected path): rm -rf / xxx")
    assert line.split("): ", 1)[1] == ("rm -rf / " + "x" * 200)[:80]
    assert "\n" not in line


def test_config_key() -> None:
    assert Config().notify_unswapped is True
    assert load_config(lambda k, d: False if k == "notify_unswapped" else d).notify_unswapped is False
    assert load_config(lambda k, d: "no" if k == "notify_unswapped" else d).notify_unswapped is True  # junk -> default


# ------------------------------------------------------------------ through the hooks


@pytest.fixture
def rt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Runtime(lambda key, default: default, lambda: tmp_path / "receipts.db")


def _result(output: str = "", code: int = 0) -> str:
    return json.dumps(dict(output=output, exit_code=code, error=None))


def transform(rt, command: str, result: str | None = None, call_id: str = "c1", args: dict | None = None):
    return rt.transform_tool_result(tool_name="terminal", args=args or dict(command=command),
                                    result=result if result is not None else _result("x\n"), tool_call_id=call_id)


@pytest.mark.parametrize(("command", "fragment"), [
    ("rm -rf /", "protected path"), ("sudo rm -rf build", "wrapped in sudo"), ("bash -c 'rm -rf x'", "bash -c"),
    ("rm -rf $(pwd)/x", "command substitution"), ("rm -rfi build", "unsupported rm option -i"),
    ("git push -f origin :old", "push deletes a ref"), ("git clean -ffd", "git clean -ff"),
])
def test_hook_appends_one_notice_line_for_an_untouched_destructive_command(rt, command, fragment) -> None:
    out = json.loads(transform(rt, command))["output"]
    lines = out.splitlines()
    assert lines[0] == "x" and len(lines) == 2
    assert lines[1].startswith(f"[safe-swap] NOT swapped (") and fragment in lines[1]
    assert lines[1].endswith(": " + " ".join(command.split())[:80])


def test_hook_is_silent_for_harmless_commands(rt) -> None:
    for command in HARMLESS:
        assert transform(rt, command) is None, command


def test_hook_notice_for_plain_text_results(rt) -> None:
    out = transform(rt, "rm -rf /", result="plain output")
    assert out.startswith("plain output\n[safe-swap] NOT swapped (protected path")


def test_notify_unswapped_switch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    off = Runtime(lambda k, d: False if k == "notify_unswapped" else d, lambda: tmp_path / "r.db")
    assert transform(off, "rm -rf /") is None
    # note_to_agent and notify_unswapped are independent switches
    only_notice = Runtime(lambda k, d: False if k == "note_to_agent" else d, lambda: tmp_path / "r2.db")
    assert "NOT swapped" in transform(only_notice, "rm -rf /")
    d = only_notice.pre_tool_call(tool_name="terminal", args=dict(command="rm -rf build"), tool_call_id="k")
    assert transform(only_notice, "x", args=d["args"], call_id="k") is None  # no receipt note, no notice: all quiet


def test_swapped_and_skipped_in_one_command_gets_both_lines(rt) -> None:
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command="rm -r build && rm -rf /"), tool_call_id="m1")
    lines = json.loads(transform(rt, "", args=d["args"], call_id="m1"))["output"].splitlines()
    assert "was run as: move-to-quarantine" in lines[1]
    assert lines[2].startswith("[safe-swap] NOT swapped (protected path")
    assert lines[2].endswith(": rm -r build && rm -rf /")


def test_receipt_store_failure_is_reported_as_not_swapped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "blocker").write_text("file")
    rt = Runtime(lambda k, d: d, lambda: tmp_path / "blocker" / "x.db")
    assert rt.pre_tool_call(tool_name="terminal", args=dict(command="rm -rf build")) is None  # failed open
    out = json.loads(transform(rt, "rm -rf build"))["output"]
    assert "NOT swapped (could not record a receipt, ran as written): rm -rf build" in out


def _run(command: str, home, cwd) -> subprocess.CompletedProcess:
    env = dict(PATH=os.environ["PATH"], HOME=str(home), GIT_CONFIG_GLOBAL=str(home / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["sh", "-c", command], cwd=cwd, env=env, capture_output=True, text=True)


def test_snapshot_failure_replaces_the_receipt_claim(rt, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "t"), ("config", "user.email", "t")):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    (repo / "a.txt").write_text("dirty\n")
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command="git reset --hard"), tool_call_id="s1")
    # quarantine cannot be created (HOME is a file): the snapshot fails, the original still runs
    blocker = tmp_path / "homefile"
    blocker.write_text("x")
    p = _run(d["args"]["command"], blocker, repo)
    assert p.returncode == 0 and (repo / "a.txt").read_text() == "one\n"
    assert "NOT swapped (snapshot failed, ran unprotected)" in p.stderr
    out = json.loads(transform(rt, "", result=_result(p.stdout + p.stderr), args=d["args"], call_id="s1"))["output"]
    assert "was run as" not in out and "Undo: /unswap" not in out  # no false claim of protection
    assert out.splitlines()[-1].endswith("): git reset --hard") and "snapshot failed" in out


def test_quarantine_failure_says_nothing_was_removed(rt, tmp_path) -> None:
    (tmp_path / "f").write_text("1")
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command="rm f"), tool_call_id="q1")
    blocker = tmp_path / "homefile"
    blocker.write_text("x")
    p = _run(d["args"]["command"], blocker, tmp_path)
    assert p.returncode == 1 and (tmp_path / "f").exists()
    out = json.loads(transform(rt, "", result=_result(p.stdout + p.stderr, 1), args=d["args"], call_id="q1"))["output"]
    assert "NOT swapped (quarantine creation failed, nothing was removed): rm f" in out
    assert "was run as" not in out


def test_runtime_failure_lines_are_removed_when_notices_are_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rt = Runtime(lambda k, d: False if k == "notify_unswapped" else d, lambda: tmp_path / "r.db")
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command="rm f"), tool_call_id="q2")
    raw = _result("[safe-swap] NOT swapped (quarantine creation failed, nothing was removed)\n", 1)
    out = json.loads(transform(rt, "", result=raw, args=d["args"], call_id="q2"))["output"]
    assert "NOT swapped" not in out


def test_swaps_command_does_not_pollute_its_own_output(rt) -> None:
    from safe_swap.commands import Service, dispatch_runner
    from safe_swap.undo import restore_script

    seen = []

    def fake(tool, args, **kw):
        seen.append(args["command"])
        return _result("restored: x\n")

    Service(rt.store, dispatch_runner(fake)).unswap("last")  # no receipt: nothing dispatched, just proves no crash
    script = restore_script('"$HOME/.safe-swap/trash"', "abc123")
    assert transform(rt, script, result=_result("restored: x\n")) is None


def test_lookalike_line_in_unrelated_output_is_left_alone(rt) -> None:
    raw = _result("[safe-swap] NOT swapped (quarantine creation failed)\nSS_STATS abc123 1 2 0 1 0\n")
    out = transform(rt, "cat notes.txt", result=raw)  # no receipt: not one of our rewritten commands
    assert out is not None  # the marker line is always removed...
    assert json.loads(out)["output"] == "[safe-swap] NOT swapped (quarantine creation failed)\n"  # ...the lookalike stays
