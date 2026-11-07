"""Run the rewritten git commands for real and recover through the receipt."""

from __future__ import annotations

import subprocess

import pytest

from safe_swap.rewriter import rewrite


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.split()[2]
    return tuple(int(p) for p in out.split(".")[:2])


pytestmark = pytest.mark.skipif(_git_version() < (2, 30), reason="--force-if-includes needs git >= 2.30")


def _commit(box, repo, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text)
    box.git("add", name, cwd=repo)
    box.git("commit", "-qm", msg, cwd=repo)


@pytest.fixture
def repo(box):
    r = box.init_repo(box.work / "repo")
    _commit(box, r, "a.txt", "one\n", "first")
    _commit(box, r, "b.txt", "two\n", "second")
    return r


def test_reset_hard_snapshot_and_undo(box, repo) -> None:
    (repo / "a.txt").write_text("edited\n")
    (repo / "new.txt").write_text("staged new\n")
    box.git("add", "new.txt", cwd=repo)
    assert box.swap("git reset --hard", cwd=repo).code == 0
    assert (repo / "a.txt").read_text() == "one\n"
    assert not (repo / "new.txt").exists()
    u = box.undo()
    assert u.code == 0, u.out
    assert (repo / "a.txt").read_text() == "edited\n"
    assert (repo / "new.txt").read_text() == "staged new\n"
    assert "safe-swap" in box.git("stash", "list", cwd=repo)


def test_reset_hard_to_older_commit_undoes_fully(box, repo) -> None:
    old_head = box.git("rev-parse", "HEAD", cwd=repo)
    (repo / "b.txt").write_text("dirty\n")
    assert box.swap("git reset --hard HEAD~1", cwd=repo).code == 0
    u = box.undo()
    assert u.code == 0, u.out
    assert box.git("rev-parse", "HEAD", cwd=repo) == old_head
    assert (repo / "b.txt").read_text() == "dirty\n"


def test_undo_does_not_clobber_commits_made_after_the_reset(box, repo) -> None:
    old_head = box.git("rev-parse", "HEAD", cwd=repo)
    (repo / "a.txt").write_text("dirty\n")
    box.swap("git reset --hard HEAD~1", cwd=repo)
    _commit(box, repo, "later.txt", "later\n", "work after the reset")
    later = box.git("rev-parse", "HEAD", cwd=repo)
    u = box.undo()
    assert box.git("rev-parse", "HEAD", cwd=repo) == later
    assert f"git reset --hard {old_head}" in u.out


def test_reset_hard_clean_tree_is_harmless(box, repo) -> None:
    assert box.swap("git reset --hard", cwd=repo).code == 0
    u = box.undo()
    assert u.code == 0 and "clean" in u.out


def test_untracked_files_survive_reset(box, repo) -> None:
    (repo / "untracked.txt").write_text("u")
    (repo / "a.txt").write_text("edited\n")
    box.swap("git reset --hard", cwd=repo)
    assert (repo / "untracked.txt").read_text() == "u"


def test_checkout_dashdash_snapshot_and_undo(box, repo) -> None:
    (repo / "a.txt").write_text("precious edits\n")
    assert box.swap("git checkout -- a.txt", cwd=repo).code == 0
    assert (repo / "a.txt").read_text() == "one\n"
    assert box.undo().code == 0
    assert (repo / "a.txt").read_text() == "precious edits\n"


def test_restore_snapshot_and_undo(box, repo) -> None:
    (repo / "a.txt").write_text("precious edits\n")
    assert box.swap("git restore a.txt", cwd=repo).code == 0
    assert (repo / "a.txt").read_text() == "one\n"
    assert box.undo().code == 0
    assert (repo / "a.txt").read_text() == "precious edits\n"


def test_git_dash_C_variant(box, repo) -> None:
    (repo / "a.txt").write_text("edited\n")
    assert box.swap(f"git -C {repo} reset --hard", cwd=box.root).code == 0
    assert box.undo().code == 0
    assert (repo / "a.txt").read_text() == "edited\n"


def test_failing_original_status_is_preserved(box, repo) -> None:
    r = box.swap("git reset --hard no-such-rev", cwd=repo)
    assert r.code != 0


def test_snapshot_does_not_run_when_earlier_and_fails(box, repo) -> None:
    (repo / "a.txt").write_text("edited\n")
    r = box.swap("false && git reset --hard", cwd=repo)
    assert (repo / "a.txt").read_text() == "edited\n"
    assert r.code == 1


def test_branch_delete_and_undo(box, repo) -> None:
    box.git("branch", "feature", cwd=repo)
    _commit(box, repo, "c.txt", "three\n", "third")
    box.git("checkout", "-q", "feature", cwd=repo)
    _commit(box, repo, "f.txt", "feat\n", "feature work")
    tip = box.git("rev-parse", "feature", cwd=repo)
    box.git("checkout", "-q", "main", cwd=repo)
    assert box.swap("git branch -D feature", cwd=repo).code == 0
    assert "feature" not in box.git("branch", cwd=repo)
    u = box.undo()
    assert u.code == 0, u.out
    assert box.git("rev-parse", "feature", cwd=repo) == tip


def test_branch_restore_fails_cleanly_if_name_reused(box, repo) -> None:
    box.git("branch", "feature", cwd=repo)
    box.swap("git branch -D feature", cwd=repo)
    box.git("branch", "feature", cwd=repo)
    assert box.undo().code == 1


# ---------------------------------------------------------------- force push


@pytest.fixture
def remote(box, repo):
    bare = box.root / "remote.git"
    box.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=box.root)
    box.git("remote", "add", "origin", str(bare), cwd=repo)
    box.git("push", "-q", "-u", "origin", "main", cwd=repo)
    mate = box.root / "mate"
    box.git("clone", "-q", str(bare), str(mate), cwd=box.root)
    box.git("config", "user.name", "mate", cwd=mate)
    box.git("config", "user.email", "mate@example.invalid", cwd=mate)
    return bare, mate


def _teammate_pushes(box, mate) -> str:
    _commit(box, mate, "mate.txt", "teammate work\n", "teammate commit")
    box.git("push", "-q", "origin", "main", cwd=mate)
    return box.git("rev-parse", "HEAD", cwd=mate)


def _diverge(box, repo) -> None:
    box.git("commit", "-q", "--amend", "-m", "rewritten history", cwd=repo)


def _remote_head(box, bare) -> str:
    return box.git("rev-parse", "main", cwd=bare)


def test_stale_lease_is_refused_where_plain_force_clobbers(box, repo, remote) -> None:
    bare, mate = remote
    mate_head = _teammate_pushes(box, mate)
    _diverge(box, repo)  # local clone never fetched the teammate commit
    plain = box.sh("git push --force origin main", cwd=repo)
    assert plain.code == 0 and _remote_head(box, bare) != mate_head  # contrast: plain force destroys it
    # reset remote to the teammate's commit and try again through the swap
    box.git("push", "-q", "--force", str(bare), f"{mate_head}:refs/heads/main", cwd=mate)
    swapped = box.swap("git push --force origin main", cwd=repo)
    assert swapped.code != 0, swapped.err
    assert "stale info" in swapped.err or "rejected" in swapped.err
    assert _remote_head(box, bare) == mate_head


def test_fetch_without_integrating_is_refused_by_force_if_includes(box, repo, remote) -> None:
    bare, mate = remote
    mate_head = _teammate_pushes(box, mate)
    _diverge(box, repo)
    box.git("fetch", "-q", "origin", cwd=repo)  # tracking ref now matches remote, but not merged
    lease_only = box.sh("git push --force-with-lease origin main", cwd=repo)
    assert lease_only.code == 0 and _remote_head(box, bare) != mate_head  # contrast: lease alone clobbers
    box.git("push", "-q", "--force", str(bare), f"{mate_head}:refs/heads/main", cwd=mate)
    box.git("fetch", "-q", "origin", cwd=repo)
    swapped = box.swap("git push -f origin main", cwd=repo)
    assert swapped.code != 0, swapped.err
    assert _remote_head(box, bare) == mate_head


def test_own_amend_after_own_push_goes_through(box, repo, remote) -> None:
    bare, _ = remote
    _diverge(box, repo)
    assert box.swap("git push -f origin main", cwd=repo).code == 0
    assert _remote_head(box, bare) == box.git("rev-parse", "HEAD", cwd=repo)


def test_plus_refspec_is_protected_too(box, repo, remote) -> None:
    bare, mate = remote
    mate_head = _teammate_pushes(box, mate)
    _diverge(box, repo)
    assert box.swap("git push origin +main", cwd=repo).code != 0
    assert _remote_head(box, bare) == mate_head


def test_rewritten_push_has_no_unswapped_force(box) -> None:
    r = rewrite("git push -f origin main", box.cfg, box.rid)
    assert r is not None and " -f" not in r.command and "--force " not in r.command
