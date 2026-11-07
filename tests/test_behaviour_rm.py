"""Run the rewritten rm for real (bash and dash) and restore it through the undo script."""

from __future__ import annotations

import os

QUARANTINE = ".safe-swap/trash"


def test_file_moves_and_restores_byte_exact(box) -> None:
    payload = bytes(range(256)) * 10
    (box.work / "data.bin").write_bytes(payload)
    r = box.swap("rm data.bin")
    assert r.code == 0, r.err
    assert not (box.work / "data.bin").exists()
    assert (box.home / QUARANTINE / box.rid / "1").read_bytes() == payload
    u = box.undo()
    assert u.code == 0, u.out
    assert (box.work / "data.bin").read_bytes() == payload


def test_tree_with_symlinks_restores_intact(box) -> None:
    tree = box.work / "build"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a.txt").write_text("alpha")
    os.symlink("sub/a.txt", tree / "link")
    os.symlink("/nonexistent/target", tree / "dangling")
    assert box.swap("rm -rf build").code == 0
    assert not tree.exists()
    assert box.undo().code == 0
    assert (tree / "sub" / "a.txt").read_text() == "alpha"
    assert os.readlink(tree / "link") == "sub/a.txt"
    assert os.readlink(tree / "dangling") == "/nonexistent/target"


def test_names_with_spaces_quotes_and_leading_dash(box) -> None:
    names = ["my file.txt", "it's.txt", "-dash.txt", "tab\tname.txt", "dollar$name"]
    for n in names:
        (box.work / n).write_text(n)
    cmd = "rm -f -- 'my file.txt' \"it's.txt\" ./-dash.txt 'tab\tname.txt' 'dollar$name'"
    assert box.swap(cmd).code == 0
    assert [p.name for p in box.work.iterdir()] == []
    assert box.undo().code == 0
    for n in names:
        assert (box.work / n).read_text() == n


def test_glob_expansion(box) -> None:
    for n in ("a.log", "b.log", "keep.txt"):
        (box.work / n).write_text(n)
    assert box.swap("rm *.log").code == 0
    assert sorted(p.name for p in box.work.iterdir()) == ["keep.txt"]
    assert box.undo().code == 0
    assert sorted(p.name for p in box.work.iterdir()) == ["a.log", "b.log", "keep.txt"]


def test_rm_f_missing_exits_zero(box) -> None:
    r = box.swap("rm -f missing.txt")
    assert (r.code, r.err) == (0, "")


def test_rm_rf_unmatched_glob_exits_zero(box) -> None:
    assert box.swap("rm -rf nothing*").code == 0


def test_rm_missing_without_f_fails_like_rm(box) -> None:
    r = box.swap("rm missing.txt")
    assert r.code == 1
    assert "cannot remove 'missing.txt': No such file or directory" in r.err
    real = box.sh("rm missing.txt")
    assert real.code == r.code


def test_directory_without_recursive_is_refused(box) -> None:
    (box.work / "d").mkdir()
    r = box.swap("rm d")
    assert r.code == 1 and "Is a directory" in r.err
    assert (box.work / "d").is_dir()


def test_mixed_present_and_missing_moves_what_exists_and_fails(box) -> None:
    (box.work / "here").write_text("x")
    r = box.swap("rm here gone")
    assert r.code == 1
    assert not (box.work / "here").exists()
    assert box.undo().code == 0 and (box.work / "here").read_text() == "x"


def test_trailing_slash_directory(box) -> None:
    (box.work / "out").mkdir()
    (box.work / "out" / "f").write_text("1")
    assert box.swap("rm -rf out/").code == 0
    assert not (box.work / "out").exists()
    assert box.undo().code == 0 and (box.work / "out" / "f").read_text() == "1"


def test_symlink_to_directory_is_moved_not_followed(box) -> None:
    (box.work / "real").mkdir()
    (box.work / "real" / "f").write_text("keep")
    os.symlink("real", box.work / "lnk")
    assert box.swap("rm lnk").code == 0
    assert (box.work / "real" / "f").read_text() == "keep"
    assert not os.path.lexists(box.work / "lnk")


def test_exit_status_composes_with_and_or(box) -> None:
    (box.work / "x").write_text("1")
    r = box.swap("rm -rf x && echo ok-and || echo bad")
    assert "ok-and" in r.out and "bad" not in r.out
    r = box.swap("rm gone && echo ok-and || echo fallback", rid="b1b2b3")
    assert "fallback" in r.out and "ok-and" not in r.out


def test_redirects_are_preserved(box) -> None:
    (box.work / "x").write_text("1")
    r = box.swap("rm -rf x 2>/dev/null; rm nothing 2>/dev/null", rid="c1c2c3")
    # rm's own complaint is swallowed by the user's redirection; only safe-swap's stats marker (stripped by the hook) remains
    assert all(line.startswith("SS_STATS ") for line in r.err.splitlines()), r.err


def test_two_segments_use_separate_slots_and_both_restore(box) -> None:
    (box.work / "a").write_text("A")
    (box.work / "b").write_text("B")
    assert box.swap("rm -rf a && rm -rf b").code == 0
    assert box.undo().code == 0
    assert (box.work / "a").read_text() == "A" and (box.work / "b").read_text() == "B"


def test_undo_refuses_to_overwrite_unless_forced(box) -> None:
    (box.work / "f").write_text("old")
    assert box.swap("rm f").code == 0
    (box.work / "f").write_text("new")
    refused = box.undo()
    assert refused.code == 1 and "exists, not overwritten" in refused.out
    assert (box.work / "f").read_text() == "new"
    forced = box.undo(force=True)
    assert forced.code == 0
    assert (box.work / "f").read_text() == "old"
    assert (box.work / f"f.pre-unswap-{box.rid}").read_text() == "new"


def test_undo_twice_reports_nothing_left(box) -> None:
    (box.work / "f").write_text("1")
    box.swap("rm f")
    assert box.undo().code == 0
    again = box.undo()
    assert "nothing to restore" in again.out


def test_variables_do_not_leak_into_the_shell(box) -> None:
    (box.work / "f").write_text("1")
    r = box.swap("rm f; echo \"[${SS_Q}${SS_N}${SS_P}${SS_RC}]\"")
    assert "[]" in r.out


def test_unwritable_home_removes_nothing_and_fails(box) -> None:
    (box.work / "f").write_text("1")
    broken = box.sh(f'HOME=/proc/none; {box_cmd(box, "rm f")}')
    assert broken.code == 1 and "NOT swapped (quarantine creation failed, nothing was removed)" in broken.err
    assert (box.work / "f").read_text() == "1"


def box_cmd(box, command: str) -> str:
    from safe_swap.rewriter import rewrite

    r = rewrite(command, box.cfg, box.rid)
    assert r is not None
    return r.command


def test_cdpath_does_not_corrupt_the_manifest(box) -> None:
    (box.work / "sub").mkdir()
    (box.work / "sub" / "f").write_text("1")
    box.env["CDPATH"] = str(box.work)
    assert box.swap("rm -rf sub").code == 0
    assert box.undo().code == 0
    assert (box.work / "sub" / "f").read_text() == "1"
