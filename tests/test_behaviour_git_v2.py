"""git clean and git stash drop/clear: run the rewritten commands for real and recover through the receipt."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from safe_swap import shellgen
from safe_swap.config import Config, quarantine_expr
from safe_swap.rewriter import rewrite
from safe_swap.undo import purge_script


def _commit(box, repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text)
    box.git("add", name, cwd=repo)
    box.git("commit", "-qm", msg, cwd=repo)


def tree_state(root: Path) -> dict:
    """Every non-.git entry: relative path -> (kind, content or link target, executable bit)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames + dirnames:
            p = Path(dirpath, name)
            rel = os.path.relpath(p, root)
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p), False)
            elif p.is_dir():
                out[rel] = ("dir", None, False)
            else:
                out[rel] = ("file", p.read_bytes(), bool(p.stat().st_mode & stat.S_IXUSR))
    return out


@pytest.fixture
def repo(box):
    r = box.init_repo(box.work / "repo")
    _commit(box, r, "tracked.txt", "tracked\n", "first")
    (r / ".gitignore").write_text("*.log\nbuild/\n")
    box.git("add", ".gitignore", cwd=r)
    box.git("commit", "-qm", "ignore", cwd=r)
    return r


def _mess(repo: Path) -> None:
    (repo / "new.txt").write_text("untracked file\n")
    (repo / "untracked_dir" / "deep").mkdir(parents=True)
    (repo / "untracked_dir" / "deep" / "f.bin").write_bytes(bytes(range(256)))
    script = repo / "tool.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    os.symlink("new.txt", repo / "link")
    os.symlink("/nonexistent/target", repo / "dangling")


# ------------------------------------------------------------------ git clean


def test_clean_fd_removes_like_the_original_and_undo_restores_everything(box, repo) -> None:
    _mess(repo)
    before = tree_state(repo)
    (repo / "tracked.txt").write_text("edited tracked file, must survive\n")
    before["tracked.txt"] = ("file", b"edited tracked file, must survive\n", False)
    swapped = box.swap("git clean -fd", cwd=repo)
    assert swapped.code == 0, swapped.err
    assert "Removing new.txt" in swapped.out and "Removing untracked_dir/" in swapped.out
    assert sorted(tree_state(repo)) == [".gitignore", "tracked.txt"]
    u = box.undo()
    assert u.code == 0, u.out
    assert "restored" in u.out
    assert tree_state(repo) == before
    assert box.git("for-each-ref", "refs/safe-swap", cwd=repo)


def test_clean_leaves_real_index_and_stash_stack_alone(box, repo) -> None:
    _mess(repo)
    (repo / "staged.txt").write_text("staged\n")
    box.git("add", "staged.txt", cwd=repo)
    status = box.git("status", "--porcelain", cwd=repo)
    stashes = box.git("stash", "list", cwd=repo)
    assert box.swap("git clean -fd", cwd=repo).code == 0
    assert box.undo().code == 0
    assert box.git("status", "--porcelain", cwd=repo) == status  # nothing got staged or unstaged by snapshot/restore
    assert box.git("stash", "list", cwd=repo) == stashes


def test_clean_without_d_keeps_untracked_dirs_and_does_not_snapshot_them(box, repo) -> None:
    _mess(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / "tracked_in_sub.txt").write_text("t")
    box.git("add", "sub/tracked_in_sub.txt", cwd=repo)
    box.git("commit", "-qm", "sub", cwd=repo)
    (repo / "sub" / "loose.txt").write_text("loose")
    assert box.swap("git clean -f", cwd=repo).code == 0
    assert (repo / "untracked_dir" / "deep" / "f.bin").exists()  # clean without -d never touches untracked dirs
    assert not (repo / "sub" / "loose.txt").exists() and not (repo / "new.txt").exists()
    snap = box.git("ls-tree", "-r", "--name-only", box.git("rev-parse", f"refs/safe-swap/{box.rid}", cwd=repo), cwd=repo)
    assert sorted(snap.splitlines()) == ["dangling", "link", "new.txt", "sub/loose.txt", "tool.sh"]
    assert box.undo().code == 0
    assert (repo / "sub" / "loose.txt").read_text() == "loose"


def test_clean_x_snapshots_ignored_files_and_plain_clean_does_not(box, repo) -> None:
    (repo / "debug.log").write_text("ignored log\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_bytes(b"\x00\x01")
    (repo / "keepme.txt").write_text("untracked\n")
    assert box.swap("git clean -fd", cwd=repo, rid="aaaaaa").code == 0
    assert (repo / "debug.log").exists() and (repo / "build" / "out.o").exists()  # ignored: left alone, as in git
    plain = box.git("ls-tree", "-r", "--name-only", box.git("rev-parse", "refs/safe-swap/aaaaaa", cwd=repo), cwd=repo)
    assert plain.splitlines() == ["keepme.txt"]
    assert box.swap("git clean -fdx", cwd=repo, rid="bbbbbb").code == 0
    assert not (repo / "debug.log").exists() and not (repo / "build").exists()
    assert box.undo(rid="bbbbbb").code == 0
    assert (repo / "debug.log").read_text() == "ignored log\n"
    assert (repo / "build" / "out.o").read_bytes() == b"\x00\x01"


def test_clean_capital_x_only_ignored(box, repo) -> None:
    (repo / "debug.log").write_text("ignored\n")
    (repo / "keepme.txt").write_text("untracked\n")
    assert box.swap("git clean -fX", cwd=repo).code == 0
    assert not (repo / "debug.log").exists() and (repo / "keepme.txt").exists()
    assert box.undo().code == 0
    assert (repo / "debug.log").read_text() == "ignored\n"


HOSTILE = [
    "with space.txt", "it's.txt", 'say "hi".txt', "-leading-dash.txt", "tab\tname.txt", "new\nline.txt",
    "trailing newline\n", "back\\slash.txt", "dollar$HOME.txt", "glob[ab]*?.txt", "café.txt", "semi;colon&amp|pipe.txt",
]


def test_clean_hostile_filenames_are_snapshotted_and_restored_byte_identical(box, repo) -> None:
    for i, name in enumerate(HOSTILE):
        (repo / name).write_bytes(f"payload {i}\n".encode())
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "new\nline in dir").write_text("nested")
    before = tree_state(repo)
    r = box.swap("git clean -fd", cwd=repo)
    assert r.code == 0, r.err
    assert sorted(tree_state(repo)) == [".gitignore", "tracked.txt"]
    u = box.undo()
    assert u.code == 0, u.out
    assert tree_state(repo) == before
    assert "NOT swapped" not in r.err


def test_clean_pathspec_and_subdirectory_cwd(box, repo) -> None:
    for d in ("a", "b"):
        (repo / d).mkdir()
        (repo / d / "x.txt").write_text(d)
    (repo / "top.txt").write_text("top")
    assert box.swap("git clean -fd a", cwd=repo).code == 0
    assert not (repo / "a").exists() and (repo / "b" / "x.txt").exists() and (repo / "top.txt").exists()
    assert box.undo().code == 0 and (repo / "a" / "x.txt").read_text() == "a"
    # from inside a subdirectory, clean (and so the snapshot) only covers that subdirectory
    assert box.swap("git clean -fd", cwd=repo / "b", rid="cccccc").code == 0
    assert not (repo / "b").exists() or not (repo / "b" / "x.txt").exists()
    assert (repo / "top.txt").exists() and (repo / "a" / "x.txt").exists()
    assert box.undo(rid="cccccc").code == 0
    assert (repo / "b" / "x.txt").read_text() == "b"


def test_clean_dry_run_is_untouched(box, repo) -> None:
    _mess(repo)
    for cmd in ("git clean -n", "git clean -nd", "git clean --dry-run -fd"):
        assert rewrite(cmd, Config(), box.rid) is None
    assert box.sh("git clean -nd", cwd=repo).code == 0
    assert (repo / "new.txt").exists()


def test_clean_exit_status_and_output_match_the_original(box, repo) -> None:
    _mess(repo)
    refused = box.swap("git clean", cwd=repo)  # no -f: git refuses (clean.requireForce), and so does the swap
    plain = box.sh("git clean", cwd=repo)
    assert refused.code == plain.code != 0
    assert (repo / "new.txt").exists()


def test_clean_undo_refuses_to_overwrite_unless_forced(box, repo) -> None:
    (repo / "new.txt").write_text("original\n")
    assert box.swap("git clean -f", cwd=repo).code == 0
    (repo / "new.txt").write_text("recreated by someone else\n")
    refused = box.undo()
    assert refused.code == 1 and "not restored" in refused.out
    assert (repo / "new.txt").read_text() == "recreated by someone else\n"
    forced = box.undo(force=True)
    assert forced.code == 0 and (repo / "new.txt").read_text() == "original\n"


def test_clean_undo_twice_says_nothing_left(box, repo) -> None:
    (repo / "new.txt").write_text("x")
    box.swap("git clean -f", cwd=repo)
    assert box.undo().code == 0
    assert "nothing to restore" in box.undo().out


def test_clean_clean_tree_is_harmless(box, repo) -> None:
    r = box.swap("git clean -fd", cwd=repo)
    assert r.code == 0 and r.err == ""
    assert not box.git("for-each-ref", "refs/safe-swap", cwd=repo)
    assert "nothing to restore" in box.undo().out


def test_clean_outside_a_repository_fails_open_like_the_original(box) -> None:
    plain = box.sh("git clean -fd", cwd=box.work)
    swapped = box.swap("git clean -fd", cwd=box.work)
    assert swapped.code == plain.code != 0
    assert "NOT swapped" not in swapped.err
    assert not (box.home / ".safe-swap").exists()


def test_clean_in_the_wrong_repository_snapshots_that_repository(box, repo) -> None:
    other = box.init_repo(box.work / "other")
    _commit(box, other, "o.txt", "o\n", "o")
    (other / "other_untracked.txt").write_text("keep me")
    (repo / "mine.txt").write_text("mine")
    assert box.swap(f"git -C {other} clean -fd", cwd=repo).code == 0
    assert (repo / "mine.txt").exists()  # the repo we are standing in is untouched
    assert box.undo().code == 0
    assert (other / "other_untracked.txt").read_text() == "keep me"


def test_clean_over_the_cap_runs_unprotected_and_says_so(box, repo, monkeypatch) -> None:
    monkeypatch.setattr(shellgen, "MAX_SNAPSHOT_FILES", 2)
    for i in range(3):
        (repo / f"f{i}.txt").write_text(str(i))
    r = box.swap("git clean -fd", cwd=repo)
    assert r.code == 0
    assert "NOT swapped (untracked files too many to snapshot)" in r.err
    assert not (repo / "f0.txt").exists()  # the original still ran


def test_clean_over_the_byte_cap_runs_unprotected_and_says_so(box, repo, monkeypatch) -> None:
    monkeypatch.setattr(shellgen, "MAX_SNAPSHOT_BYTES", 100)
    (repo / "big.bin").write_bytes(b"x" * 1000)
    r = box.swap("git clean -f", cwd=repo)
    assert r.code == 0 and "NOT swapped (untracked files too large to snapshot)" in r.err
    assert not (repo / "big.bin").exists()


def test_clean_ignored_over_the_cap_still_protects_untracked_files(box, repo, monkeypatch) -> None:
    monkeypatch.setattr(shellgen, "MAX_SNAPSHOT_FILES", 2)
    (repo / "keep_me.txt").write_text("precious")
    for i in range(3):
        (repo / f"noise{i}.log").write_text("ignored")
    r = box.swap("git clean -fdx", cwd=repo)
    assert "NOT swapped (ignored files too many to snapshot)" in r.err
    assert box.undo().code == 0
    assert (repo / "keep_me.txt").read_text() == "precious"
    assert not (repo / "noise0.log").exists()  # the ignored files were not protected, and the notice said so


def test_clean_quarantine_inside_the_repository_is_not_snapshotted(box, repo) -> None:
    from safe_swap.rewriter import rewrite as rw

    cfg = Config(quarantine_dir=str(repo / "q"))
    r = rw("git clean -fd", cfg, box.rid)
    (repo / "new.txt").write_text("x")
    p = box.sh(r.command, cwd=repo)
    assert "NOT swapped (quarantine is inside this repository, ran unprotected)" in p.err
    assert not (repo / "q").exists()


def test_clean_with_exclude_pattern_is_still_protected(box, repo) -> None:
    (repo / "a.tmp").write_text("a")
    (repo / "b.keep").write_text("b")
    assert box.swap("git clean -f -e '*.keep'", cwd=repo).code == 0
    assert (repo / "b.keep").exists() and not (repo / "a.tmp").exists()
    assert box.undo().code == 0 and (repo / "a.tmp").read_text() == "a"


def test_clean_variables_do_not_leak_into_the_shell(box, repo) -> None:
    (repo / "new.txt").write_text("x")
    r = box.swap('git clean -f; echo "[${SS_Q}${SS_TL}${SS_C}${SS_CM}${SS_RCPT}]"', cwd=repo)
    assert "[]" in r.out


def test_clean_purge_removes_ref_and_quarantine(box, repo) -> None:
    (repo / "new.txt").write_text("x")
    box.swap("git clean -f", cwd=repo)
    assert box.git("for-each-ref", "refs/safe-swap", cwd=repo)
    assert box.sh(purge_script(quarantine_expr(box.cfg.quarantine_dir), [box.rid])).code == 0
    assert box.git("for-each-ref", "refs/safe-swap", cwd=repo) == ""
    assert not (box.home / ".safe-swap" / "trash" / box.rid).exists()


def test_clean_and_rm_in_one_command_use_separate_slots(box, repo) -> None:
    (repo / "new.txt").write_text("x")
    (box.work / "gone.txt").write_text("g")
    assert box.swap("rm gone.txt && cd repo && git clean -f", cwd=box.work).code == 0
    assert box.undo().code == 0
    assert (box.work / "gone.txt").read_text() == "g" and (repo / "new.txt").read_text() == "x"


# ------------------------------------------------------------------ git stash drop / clear


def _stash(box, repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    box.git("add", name, cwd=repo)
    box.git("stash", "push", "-q", "-m", message, cwd=repo)
    return box.git("rev-parse", "stash@{0}", cwd=repo)


def stash_list(box, repo: Path) -> str:
    return box.git("stash", "list", "--format=%H %gs", cwd=repo)


@pytest.fixture
def stashes(box, repo):
    shas = [_stash(box, repo, f"s{i}.txt", f"stash {i}\n", f"entry {i}") for i in range(3)]
    return shas  # stash@{0} is the newest (entry 2)


def test_stash_drop_default_and_undo_recreates_the_entry(box, repo, stashes) -> None:
    before = stash_list(box, repo)
    r = box.swap("git stash drop", cwd=repo)
    assert r.code == 0 and "Dropped" in r.out
    assert stash_list(box, repo).count("\n") == 1
    u = box.undo()
    assert u.code == 0, u.out
    assert stash_list(box, repo) == before  # same shas, same messages, same order
    assert box.git("ls-tree", "-r", "--name-only", "stash@{0}", cwd=repo)  # the content is intact


def test_stash_drop_other_entry_comes_back_on_top(box, repo, stashes) -> None:
    assert box.swap("git stash drop stash@{1}", cwd=repo).code == 0
    remaining = box.git("stash", "list", "--format=%H", cwd=repo).splitlines()
    assert remaining == [stashes[2], stashes[0]]
    assert box.undo().code == 0
    top = box.git("stash", "list", "--format=%H", cwd=repo).splitlines()
    assert top[0] == stashes[1] and sorted(top) == sorted(stashes)  # documented: it returns as stash@{0}
    assert "entry 1" in box.git("stash", "list", "--format=%gs", cwd=repo).splitlines()[0]


def test_stash_drop_bare_number(box, repo, stashes) -> None:
    assert box.swap("git stash drop 2", cwd=repo).code == 0
    assert stashes[0] not in box.git("stash", "list", "--format=%H", cwd=repo)
    assert box.undo().code == 0
    assert stashes[0] in box.git("stash", "list", "--format=%H", cwd=repo)


def test_stash_clear_and_undo_restores_order(box, repo, stashes) -> None:
    before = stash_list(box, repo)
    assert box.swap("git stash clear", cwd=repo).code == 0
    assert box.git("stash", "list", cwd=repo) == ""
    for sha in stashes:  # the private ref keeps all of them reachable even though the stack is empty
        assert box.git("merge-base", "--is-ancestor", sha, f"refs/safe-swap/{box.rid}", cwd=repo, ) == ""
    u = box.undo()
    assert u.code == 0, u.out
    assert stash_list(box, repo) == before


def test_stash_survives_gc_after_clear(box, repo, stashes) -> None:
    box.swap("git stash clear", cwd=repo)
    box.git("reflog", "expire", "--expire=now", "--all", cwd=repo)
    box.git("gc", "-q", "--prune=now", cwd=repo)
    assert box.undo().code == 0
    assert box.git("stash", "list", "--format=%H", cwd=repo).splitlines() == list(reversed(stashes))


def test_stash_hostile_message_is_restored(box, repo) -> None:
    message = 'it\'s a "quoted" $HOME `tick` message; with | pipes & more'
    _stash(box, repo, "h.txt", "h", message)
    before = stash_list(box, repo)
    assert box.swap("git stash drop", cwd=repo).code == 0
    assert box.undo().code == 0
    assert stash_list(box, repo) == before and message in before


def test_stash_drop_without_entries_fails_like_the_original(box, repo) -> None:
    plain = box.sh("git stash drop", cwd=repo)
    swapped = box.swap("git stash drop", cwd=repo)
    assert swapped.code == plain.code != 0
    assert not box.git("for-each-ref", "refs/safe-swap", cwd=repo)
    assert "nothing to restore" in box.undo().out


def test_stash_drop_missing_index_fails_like_the_original(box, repo, stashes) -> None:
    plain = box.sh("git stash drop stash@{9}", cwd=repo)
    swapped = box.swap("git stash drop stash@{9}", cwd=repo)
    assert swapped.code == plain.code != 0
    assert stash_list(box, repo).count("\n") == 2  # all three still there


def test_stash_outside_a_repository_fails_open(box) -> None:
    for cmd in ("git stash drop", "git stash clear"):
        plain = box.sh(cmd, cwd=box.work)
        swapped = box.swap(cmd, cwd=box.work)
        assert swapped.code == plain.code != 0
        assert "NOT swapped" not in swapped.err
    assert not (box.home / ".safe-swap").exists()


def test_stash_in_the_wrong_repository_records_that_repository(box, repo, stashes) -> None:
    other = box.init_repo(box.work / "other")
    _commit(box, other, "o.txt", "o\n", "o")
    sha = _stash(box, other, "ox.txt", "ox", "other stash")
    assert box.swap(f"git -C {other} stash drop", cwd=repo).code == 0
    assert stash_list(box, repo).count("\n") == 2  # the repository we stand in is untouched
    assert box.undo().code == 0
    assert box.git("rev-parse", "stash@{0}", cwd=other) == sha


def test_stash_pop_and_apply_are_not_swapped(box, repo, stashes) -> None:
    for cmd in ("git stash pop", "git stash apply", "git stash"):
        assert rewrite(cmd, Config(), box.rid) is None


def test_stash_purge_removes_ref(box, repo, stashes) -> None:
    box.swap("git stash clear", cwd=repo)
    assert box.git("for-each-ref", "refs/safe-swap", cwd=repo)
    assert box.sh(purge_script(quarantine_expr(box.cfg.quarantine_dir), [box.rid])).code == 0
    assert box.git("for-each-ref", "refs/safe-swap", cwd=repo) == ""


def test_stash_variables_do_not_leak(box, repo, stashes) -> None:
    r = box.swap('git stash drop; echo "[${SS_Q}${SS_TL}${SS_L}${SS_P}${SS_K}${SS_RCPT}]"', cwd=repo)
    assert "[]" in r.out


def test_stash_drop_does_not_run_when_earlier_and_fails(box, repo, stashes) -> None:
    r = box.swap("false && git stash drop", cwd=repo)
    assert r.code == 1 and stash_list(box, repo).count("\n") == 2


def test_stash_and_clean_in_a_pipeline_group_keep_exit_status(box, repo, stashes) -> None:
    r = box.swap("git stash drop | cat", cwd=repo)
    assert r.code == 0 and "Dropped" in r.out
    assert box.undo().code == 0
