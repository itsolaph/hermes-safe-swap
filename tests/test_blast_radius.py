"""Blast radius of the rm swap: counted in the shell before the move, reported in the note and in /swaps."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from safe_swap import shellgen
from safe_swap.commands import Service, local_runner
from safe_swap.config import Config
from safe_swap.receipts import Receipt, Store
from safe_swap.rewriter import rewrite
from safe_swap.runtime import BlastRadius, Runtime, describe, human_bytes, receipt_size

STATS_PREFIX = "SS_STATS abc123 "


def stats_of(result) -> list[int]:
    lines = [ln for ln in result.err.splitlines() if ln.startswith("SS_STATS ")]
    assert len(lines) == 1, result.err
    return [int(x) for x in lines[0].split()[2:]]  # files bytes capped complete copied


def make_tree(root: Path, files: int = 5, size: int = 1000) -> None:
    (root / "sub").mkdir(parents=True)
    for i in range(files):
        (root / "sub" / f"f{i}").write_bytes(b"x" * size)
    os.symlink("sub", root / "lnk")


def test_stats_line_counts_files_bytes_and_links(box) -> None:
    make_tree(box.work / "build")
    r = box.swap("rm -r build")
    assert r.code == 0
    assert stats_of(r) == [6, 5000, 0, 1, 0]  # 5 files + 1 symlink, 5000 bytes, not capped, complete, not copied
    assert not (box.work / "build").exists()


def test_single_file_and_multiple_targets_add_up(box) -> None:
    (box.work / "a").write_bytes(b"1" * 10)
    (box.work / "b").mkdir()
    (box.work / "b" / "c").write_bytes(b"2" * 20)
    r = box.swap("rm a b")
    assert r.code == 1 and "Is a directory" in r.err  # b is a directory: refused like rm, only a moves...
    assert stats_of(r) == [2, 30, 0, 1, 0]  # ...but both were counted before anything moved
    assert not (box.work / "a").exists() and (box.work / "b" / "c").exists()
    r = box.swap("rm -r b", rid="d1d2d3")
    assert r.code == 0 and stats_of(r) == [1, 20, 0, 1, 0]


def test_two_rm_segments_report_one_line_each_and_sum_in_the_note(box) -> None:
    (box.work / "a").write_bytes(b"1" * 10)
    (box.work / "b").write_bytes(b"2" * 20)
    r = box.swap("rm a && rm b")
    lines = [ln for ln in r.err.splitlines() if ln.startswith("SS_STATS")]
    assert len(lines) == 2


def test_missing_target_prints_no_stats_and_stays_quiet(box) -> None:
    r = box.swap("rm -f missing.txt")
    assert (r.code, r.err) == (0, "")


def test_exit_status_and_output_are_unchanged_by_the_count(box) -> None:
    (box.work / "f").write_text("1")
    assert box.swap("rm -rf f && echo ok").out == "ok\n"
    assert box.swap("rm nothing || echo failed", rid="b1b2b3").out == "failed\n"


@pytest.mark.parametrize("redir", ["2>/dev/null", ">/dev/null 2>&1", "2>&1 >/dev/null"])
def test_stats_survive_the_users_redirections(box, redir) -> None:
    make_tree(box.work / "build", files=2)
    r = box.swap(f"rm -r build {redir}")
    assert r.code == 0
    assert stats_of(r) == [3, 2000, 0, 1, 0]  # printed outside the redirection: the hook can always read it


def test_quarantine_failure_line_survives_a_redirection(box) -> None:
    (box.work / "f").write_text("1")
    cmd = rewrite("rm -rf f 2>/dev/null", box.cfg, box.rid).command
    broken = box.sh(f"HOME=/proc/none; {cmd}")
    assert broken.code == 1 and "NOT swapped (quarantine creation failed, nothing was removed)" in broken.err
    assert (box.work / "f").exists()


def test_walk_cap_by_entries(box, monkeypatch) -> None:
    monkeypatch.setattr(shellgen, "MAX_WALK", 10)
    make_tree(box.work / "big", files=25, size=100)
    r = box.swap("rm -r big")
    files, size, capped, complete, _ = stats_of(r)
    assert (files, capped, complete) == (10, 1, 1) and size == 0  # no byte figure for a capped walk


def test_fifty_thousand_entry_cap_says_50000_plus(box) -> None:
    big = box.work / "big"
    big.mkdir()
    for d in range(10):
        (big / f"d{d}").mkdir()
        for i in range(5100):
            os.close(os.open(big / f"d{d}" / f"f{i}", os.O_CREAT | os.O_WRONLY))
    r = box.swap("rm -r big")
    assert r.code == 0
    files, _size, capped, complete, _ = stats_of(r)
    assert files == 50_000 and capped == 1 and complete == 1
    assert "50000+ files" in describe(_receipt(), blast=BlastRadius(files, 0, True))
    assert not big.exists()


def test_the_cap_stops_find_itself_not_just_the_counting(box, tmp_path) -> None:
    stub = tmp_path / "stub"
    stub.mkdir()
    endless = stub / "find"  # prints names forever: only a SIGPIPE from `head` can end it
    endless.write_text("#!/bin/sh\ncase \"$*\" in *-print*) while :; do echo name; done;; esac\n")
    endless.chmod(0o755)
    box.env["PATH"] = f"{stub}:{box.env['PATH']}"
    (box.work / "f").write_text("1")
    r = box.swap("rm f")
    assert r.code == 0
    files, size, capped, complete, _ = stats_of(r)
    assert (files, capped, complete) == (50_000, 1, 1) and size == 0


def test_time_limit_kill_keeps_the_partial_count(box, tmp_path) -> None:
    stub = tmp_path / "stub"
    stub.mkdir()
    fake = stub / "timeout"
    fake.write_text("#!/bin/sh\nprintf '7 700 0 0\\n'\nexit 124\n")  # what a killed walk leaves behind
    fake.chmod(0o755)
    box.env["PATH"] = f"{stub}:{box.env['PATH']}"
    (box.work / "f").write_text("1")
    r = box.swap("rm f")
    assert r.code == 0
    assert stats_of(r) == [7, 700, 0, 0, 0]  # complete=0 -> the note treats it as a lower bound ("7+ files")


def test_no_timeout_binary_means_entry_cap_only_and_still_works(box, tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (box.shell, "sh", "find", "ls", "head", "tail", "wc", "awk", "mkdir", "mv", "dirname", "basename"):
        found = shutil.which(tool)
        assert found, tool
        if not (bin_dir / tool).exists():
            os.symlink(found, bin_dir / tool)
    box.env["PATH"] = str(bin_dir)
    assert not shutil.which("timeout", path=str(bin_dir))
    make_tree(box.work / "build", files=3)
    r = box.swap("rm -r build")
    assert r.code == 0 and stats_of(r) == [4, 3000, 0, 1, 0]


def test_missing_tool_means_no_numbers_not_wrong_numbers(box, tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (box.shell, "sh", "ls", "head", "tail", "wc", "awk", "mkdir", "mv", "dirname", "basename"):  # no find
        os.symlink(shutil.which(tool), bin_dir / tool)
    box.env["PATH"] = str(bin_dir)
    (box.work / "f").write_text("1")
    r = box.swap("rm f")
    assert r.code == 0 and not (box.work / "f").exists()  # the swap itself still works
    assert "SS_STATS" not in r.err  # and it never claims "0 files" when it could not count


def test_hostile_names_are_counted_without_confusing_the_walk(box) -> None:
    root = box.work / "build"
    root.mkdir()
    for name in ("sp ace", "new\nline", "-dash", "$HOME", "q'uote", "!", "(paren)"):
        (root / name).write_bytes(b"1234567890")
    r = box.swap("rm -r build")
    files, size, capped, complete, _ = stats_of(r)
    assert r.code == 0 and capped == 0 and complete == 1
    assert files >= 7 and size >= 70  # a newline in a name can only add a bogus line, never hide a file
    assert box.undo().code == 0


def test_dash_leading_and_bang_targets_are_not_taken_for_find_options(box) -> None:
    (box.work / "-weird").write_bytes(b"123")
    (box.work / "!").write_bytes(b"45")
    r = box.swap("rm -- -weird '!'")
    assert r.code == 0 and stats_of(r) == [2, 5, 0, 1, 0]


# ------------------------------------------------------------------ copy across filesystems


def _other_filesystem() -> Path | None:
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK) and shm.stat().st_dev != Path(tempfile.gettempdir()).stat().st_dev:
        return shm
    return None


@pytest.mark.skipif(_other_filesystem() is None, reason="needs a second writable filesystem (/dev/shm)")
def test_cross_filesystem_move_is_flagged_and_still_restorable(box) -> None:
    home = Path(tempfile.mkdtemp(dir=_other_filesystem()))
    try:
        box.env["HOME"] = str(home)
        make_tree(box.work / "build", files=2)
        r = box.swap("rm -r build")
        assert r.code == 0
        assert stats_of(r) == [3, 2000, 0, 1, 1]  # copied = 1
        assert box.undo().code == 0
        assert (box.work / "build" / "sub" / "f0").read_bytes() == b"x" * 1000
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_same_filesystem_move_is_not_flagged(box) -> None:
    make_tree(box.work / "build", files=1)
    assert stats_of(box.swap("rm -r build"))[4] == 0


# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(("n", "text"), [
    (0, "0 B"), (1, "1 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"), (10 * 1024, "10 KB"),
    (14 * 1024 * 1024, "14 MB"), (int(1.5 * 1024**3), "1.5 GB"), (5000 * 1024**3, "5000 GB"),
    (312 * 47000, "14 MB"), (1048166, "1.0 MB"), (10239, "10 KB"), (4999, "4.9 KB"),
])
def test_human_bytes(n: int, text: str) -> None:
    assert human_bytes(n) == text


def _receipt(**kw) -> Receipt:
    base = dict(id="abc123", ts=0, session_id="", task_id="", tool_call_id="", kind="rm", original="rm -r build",
                rewritten="", workdir="", qdir="~/.safe-swap/trash", status="applied", exit_code=0)
    return Receipt(**{**base, **kw})


def test_note_format_matches_the_documented_example() -> None:
    line = describe(_receipt(), blast=BlastRadius(312, 14 * 1024 * 1024))
    assert line == ("[safe-swap] `rm -r build` was run as: move-to-quarantine (312 files, 14 MB, receipt abc123). "
                    "Undo: /unswap abc123")


def test_note_variants() -> None:
    assert "(1 file, 3 B, receipt abc123)" in describe(_receipt(), blast=BlastRadius(1, 3))
    assert "(7+ files, receipt abc123)" in describe(_receipt(), blast=BlastRadius(7, 700, capped=True))
    assert "(2 files, 5 B, copied across filesystems, receipt abc123)" in describe(
        _receipt(), blast=BlastRadius(2, 5, copied=True))
    assert "(receipt abc123)" in describe(_receipt())  # nothing measured: the old format
    assert "(receipt abc123)" in describe(_receipt(kind="reset"))


# ------------------------------------------------------------------ end to end through the hooks, /swaps, the store


@pytest.fixture
def rt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return Runtime(lambda key, default: default, lambda: tmp_path / "receipts.db")


def run_swapped(rt, command: str, cwd: Path, call_id: str = "c1") -> tuple[dict, str]:
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command=command), task_id="t", session_id="s", tool_call_id=call_id)
    p = subprocess.run(["sh", "-c", d["args"]["command"]], cwd=cwd, capture_output=True, text=True)
    raw = json.dumps(dict(output=p.stdout + p.stderr, exit_code=p.returncode, error=None))
    rt.post_tool_call(tool_name="terminal", args=d["args"], result=raw, tool_call_id=call_id)
    new = rt.transform_tool_result(tool_name="terminal", args=d["args"], result=raw, tool_call_id=call_id)
    return d, json.loads(new)["output"]


def test_hooks_strip_the_marker_report_numbers_and_persist_them(rt, tmp_path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    make_tree(work / "build")
    _d, out = run_swapped(rt, "rm -r build", work)
    assert "SS_STATS" not in out
    receipt = rt.store().by_call("c1")
    assert "[safe-swap] `rm -r build` was run as: move-to-quarantine (6 files, 4.9 KB, receipt " + receipt.id + "). " in out
    assert f"Undo: /unswap {receipt.id}" in out
    assert (receipt.files, receipt.bytes, receipt.capped, receipt.copied) == (6, 5000, 0, 0)


def test_swaps_table_shows_the_same_numbers(rt, tmp_path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    make_tree(work / "build")
    (work / "one.txt").write_bytes(b"1")
    run_swapped(rt, "rm -r build", work, "c1")
    run_swapped(rt, "rm one.txt", work, "c2")
    pre = rt.pre_tool_call(tool_name="terminal", args=dict(command="git push -f"), tool_call_id="c3")
    assert pre
    table = Service(rt.store, local_runner).swaps("")
    header, *rows = table.splitlines()
    assert header.split()[:5] == ["id", "age", "status", "kind", "size"]
    assert any("6 files, 4.9 KB" in r and "rm -r build" in r for r in rows)
    assert any("1 file, 1 B" in r and "rm one.txt" in r for r in rows)
    assert any(r.split()[3] == "push" and " - " in r for r in rows)  # not measured -> "-"


def test_two_segments_are_summed_in_the_note(rt, tmp_path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    (work / "a").write_bytes(b"1" * 10)
    (work / "b").write_bytes(b"2" * 20)
    _d, out = run_swapped(rt, "rm a && rm b", work)
    assert "(2 files, 30 B, receipt " in out


def test_time_capped_note_uses_plus(rt, tmp_path) -> None:
    stub = tmp_path / "stub"
    stub.mkdir()
    fake = stub / "timeout"
    fake.write_text("#!/bin/sh\nprintf '7 700 0 0\\n'\nexit 124\n")
    fake.chmod(0o755)
    os.environ["PATH"] = f"{stub}:{os.environ['PATH']}"
    try:
        work = tmp_path / "w"
        work.mkdir()
        (work / "f").write_text("1")
        _d, out = run_swapped(rt, "rm f", work)
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]
    assert "(7+ files, receipt " in out


def test_marker_line_is_removed_even_when_the_note_is_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    rt = Runtime(lambda k, d: False if k == "note_to_agent" else d, lambda: tmp_path / "r.db")
    work = tmp_path / "w"
    work.mkdir()
    (work / "f").write_text("1")
    d = rt.pre_tool_call(tool_name="terminal", args=dict(command="rm f"), tool_call_id="k")
    p = subprocess.run(["sh", "-c", d["args"]["command"]], cwd=work, capture_output=True, text=True)
    raw = json.dumps(dict(output=p.stdout + p.stderr, exit_code=0))
    out = json.loads(rt.transform_tool_result(tool_name="terminal", args=d["args"], result=raw, tool_call_id="k"))["output"]
    assert out == ""


# ------------------------------------------------------------------ receipts schema


def test_v1_database_is_upgraded_in_place_and_the_plugin_keeps_working(tmp_path, monkeypatch) -> None:
    db = tmp_path / "receipts.db"
    with sqlite3.connect(db) as conn:  # the exact v1 schema, with a row from before the upgrade
        conn.execute("""CREATE TABLE receipts (
            id TEXT PRIMARY KEY, ts REAL NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL, kind TEXT NOT NULL, original TEXT NOT NULL, rewritten TEXT NOT NULL,
            workdir TEXT NOT NULL, qdir TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER)""")
        conn.execute("INSERT INTO receipts VALUES ('0ld001', 1.0, '', '', 'old', 'rm', 'rm -rf old', '', '', '~/.safe-swap/trash', 'applied', 0)")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    rt = Runtime(lambda k, d: d, lambda: db)
    work = tmp_path / "w"
    work.mkdir()
    (work / "f").write_bytes(b"123")
    _d, out = run_swapped(rt, "rm f", work)
    assert "(1 file, 3 B, receipt " in out  # the new columns work...
    old = rt.store().get("0ld001")
    assert (old.files, old.bytes, old.capped, old.copied) == (None, None, 0, 0)  # ...and the old row reads back
    assert "0ld001" in Service(rt.store, local_runner).swaps("")
    again = Store(db)  # opening twice must not fail on an already-migrated file
    assert again.get("0ld001") is not None


def test_receipt_size_text() -> None:
    assert receipt_size(_receipt()) == "-"
    assert receipt_size(_receipt(files=312, bytes=14 * 1024 * 1024)) == "312 files, 14 MB"
    assert receipt_size(_receipt(files=50000, bytes=5, capped=1)) == "50000+ files"
    assert receipt_size(_receipt(files=1, bytes=1, copied=1)) == "1 file, 1 B, copied"


def test_executable_bit_is_irrelevant_to_the_count(box) -> None:
    script = box.work / "tool.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(stat.S_IRWXU)
    assert stats_of(box.swap("rm tool.sh"))[:2] == [1, 10]
