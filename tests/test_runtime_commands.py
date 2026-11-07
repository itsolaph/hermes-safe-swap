"""Hooks and slash-command logic against a real store and the real shell (no Hermes needed)."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from safe_swap.commands import Service, cli_handler, cli_setup, dispatch_runner, local_runner
from safe_swap.receipts import Receipt, Store
from safe_swap.runtime import Runtime, describe


@pytest.fixture
def rt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return Runtime(lambda key, default: default, lambda: tmp_path / "receipts.db")


def pre(rt, command, call_id="c1"):
    return rt.pre_tool_call(tool_name="terminal", args={"command": command}, task_id="t", session_id="s", tool_call_id=call_id)


def test_pre_returns_modify_and_stores_pending_receipt(rt) -> None:
    d = pre(rt, "rm -rf build")
    assert d["action"] == "modify" and "SS_RCPT=" in d["args"]["command"]
    (r,) = rt.store().recent()
    assert (r.status, r.kind, r.original, r.tool_call_id) == ("pending", "rm", "rm -rf build", "c1")


@pytest.mark.parametrize("name,args", [("read_file", {"command": "rm -rf x"}), ("terminal", None), ("terminal", {}),
                                       ("terminal", {"command": 3}), ("terminal", {"command": "ls"})])
def test_pre_ignores_everything_else(rt, name, args) -> None:
    assert rt.pre_tool_call(tool_name=name, args=args) is None
    assert rt.store().recent() == []


def test_post_settles_applied_failed_and_background(rt) -> None:
    for call, result, status, expected in [
        ("a", json.dumps({"exit_code": 0, "output": ""}), "ok", "applied"),
        ("b", json.dumps({"exit_code": 1, "output": ""}), "ok", "failed"),
        ("c", json.dumps({"exit_code": None, "status": "yielded_to_background"}), "ok", "applied"),
        ("d", "tool blocked", "blocked", "failed"),
    ]:
        d = pre(rt, "rm -rf x", call)
        rt.post_tool_call(tool_name="terminal", args=d["args"], result=result, tool_call_id=call, status=status)
        assert rt.store().by_call(call).status == expected, call


def test_post_correlates_by_call_id_when_args_are_the_original(rt) -> None:
    pre(rt, "git push -f", "p1")
    rt.post_tool_call(tool_name="terminal", args={"command": "git push -f"}, result=json.dumps({"exit_code": 0}), tool_call_id="p1")
    assert rt.store().by_call("p1").status == "applied"


def test_transform_appends_to_json_output_and_keeps_it_valid(rt) -> None:
    d = pre(rt, "rm -rf build")
    raw = json.dumps({"output": "gone", "exit_code": 0, "error": None})
    new = rt.transform_tool_result(tool_name="terminal", args=d["args"], result=raw, tool_call_id="c1")
    data = json.loads(new)
    assert data["output"].startswith("gone\n[safe-swap] `rm -rf build` was run as: move-to-quarantine")
    assert data["exit_code"] == 0


def test_transform_handles_plain_text_and_untracked_calls(rt) -> None:
    d = pre(rt, "rm -rf build")
    assert rt.transform_tool_result(tool_name="terminal", args=d["args"], result="plain", tool_call_id="c1").startswith("plain\n[safe-swap]")
    assert rt.transform_tool_result(tool_name="terminal", args={"command": "ls"}, result="x", tool_call_id="zzz") is None
    assert rt.transform_tool_result(tool_name="terminal", args=d["args"], result=None, tool_call_id="c1") is None
    assert rt.transform_tool_result(tool_name="web_search", args=d["args"], result="x", tool_call_id="c1") is None


def test_note_can_be_switched_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rt = Runtime(lambda k, d: False if k == "note_to_agent" else d, lambda: tmp_path / "r.db")
    d = pre(rt, "rm -rf build")
    assert rt.transform_tool_result(tool_name="terminal", args=d["args"], result="x", tool_call_id="c1") is None


def test_pre_fails_open_when_store_breaks(tmp_path, monkeypatch) -> None:
    (tmp_path / "blocker").write_text("file")
    rt = Runtime(lambda k, d: d, lambda: tmp_path / "blocker" / "x.db")
    assert pre(rt, "rm -rf build") is None


def test_pre_fails_open_when_config_raises(tmp_path) -> None:
    def boom(key, default):
        raise RuntimeError("config exploded")

    assert pre(Runtime(boom, lambda: tmp_path / "r.db"), "rm -rf build") is None


def test_ids_are_unique_across_calls(rt) -> None:
    ids = {rt.store().get(pre(rt, "rm -rf x", f"c{i}")["args"]["command"].split("SS_RCPT=")[1][:6]).id for i in range(30)}
    assert len(ids) == 30


# ------------------------------------------------------------------ commands


@pytest.fixture
def svc(rt):
    return Service(rt.store, local_runner)


def run_swapped(rt, command, call_id="c1"):
    d = pre(rt, command, call_id)
    subprocess.run(["sh", "-c", d["args"]["command"]], capture_output=True, check=False)
    rt.post_tool_call(tool_name="terminal", args=d["args"], result=json.dumps({"exit_code": 0}), tool_call_id=call_id)
    return d["args"]["command"].split("SS_RCPT=")[1][:6]


def test_swaps_table_and_empty_state(rt, svc) -> None:
    assert svc.swaps("").startswith("No swaps yet")
    (rt.store().path.parent / "gone").write_text("x")
    run_swapped(rt, "rm gone")
    table = svc.swaps("")
    assert "applied" in table and "rm " in table and "/unswap" in table


def test_unswap_restores_and_marks_undone(rt, svc, tmp_path) -> None:
    f = tmp_path / "keep.txt"
    f.write_text("data")
    rid = run_swapped(rt, f"rm {f.name}")
    assert not f.exists()
    assert svc.unswap(rid).startswith(f"Undone {rid}")
    assert f.read_text() == "data"
    assert rt.store().get(rid).status == "undone"
    assert "already undone" in svc.unswap(rid)


def test_unswap_last_skips_pushes(rt, svc, tmp_path) -> None:
    f = tmp_path / "keep.txt"
    f.write_text("data")
    run_swapped(rt, "rm keep.txt", "c1")
    pre(rt, "git push -f", "c2")
    assert svc.unswap("last").startswith("Undone")
    assert f.exists()


def test_unswap_push_explains_instead_of_running(rt, svc) -> None:
    pre(rt, "git push -f", "c2")
    rid = rt.store().by_call("c2").id
    assert "cannot be undone" in svc.unswap(rid)


@pytest.mark.parametrize("arg", ["", "zzzzzz", "nothex", "abc", "a b c", "../../etc", "$(id)", "last"])
def test_unswap_bad_input_never_runs_anything(svc, arg) -> None:
    out = svc.unswap(arg)
    assert out.startswith("Usage") or "No receipt" in out


def test_purge_only_after_explicit_command_and_respects_age(rt, svc, tmp_path) -> None:
    f = tmp_path / "old.txt"
    f.write_text("x")
    rid = run_swapped(rt, f"rm {f.name}")
    g = tmp_path / "new.txt"
    g.write_text("y")
    rid2 = run_swapped(rt, f"rm {g.name}", "c2")
    store = rt.store()
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE receipts SET ts = ? WHERE id = ?", (time.time() - 10 * 86400, rid))
    quarantine = tmp_path / "home" / ".safe-swap" / "trash"
    assert (quarantine / rid).exists() and (quarantine / rid2).exists()
    assert svc.purge(["--older", "7d"]) == "Purged 1 receipt(s)."
    assert not (quarantine / rid).exists() and (quarantine / rid2).exists()
    assert store.get(rid).status == "purged"
    assert "purged" in svc.unswap(rid)
    assert svc.swaps("purge --older nonsense").startswith("Usage")
    assert svc.swaps("purge").startswith("Purged 1")
    assert not (quarantine / rid2).exists()


def test_describe_is_one_honest_line() -> None:
    r = Receipt("abc123", 0, "", "", "", "rm,push", "rm out `x`\nmore", "", "", "~/.safe-swap/trash", "applied", 0)
    line = describe(r)
    assert "\n" not in line and "`rm out 'x' more`" in line and "abc123" in line


def test_cli_handler_roundtrip(rt, svc, tmp_path, capsys) -> None:
    import argparse
    parser = argparse.ArgumentParser()
    cli_setup(parser)
    f = tmp_path / "c.txt"
    f.write_text("1")
    rid = run_swapped(rt, f"rm {f.name}")
    assert cli_handler(svc, parser.parse_args(["list"])) == 0
    assert rid in capsys.readouterr().out
    cli_handler(svc, parser.parse_args(["undo", rid]))
    assert f.read_text() == "1"
    cli_handler(svc, parser.parse_args(["purge", "--older", "1d"]))
    assert "Purged 0" in capsys.readouterr().out.splitlines()[-1]


@pytest.mark.parametrize("payload", [
    {"output": "", "exit_code": None, "status": "pending_approval", "description": "recursive delete"},
    {"output": "", "error": "BLOCKED: denied", "status": "blocked"},
    {"approval_pending": True, "output": ""},
])
def test_dispatch_runner_explains_approval_gate_instead_of_hanging(payload) -> None:
    code, text = dispatch_runner(lambda tool, args, **kw: json.dumps(payload))("true", "t")
    assert code == 1 and "approval gate" in text


def test_dispatch_runner_passes_task_id_and_parses_output() -> None:
    seen = {}

    def fake(tool, args, **kw):
        seen.update(tool=tool, args=args, kw=kw)
        return json.dumps({"output": "restored: x\n", "exit_code": 0})

    assert dispatch_runner(fake)("script", "task-9") == (0, "restored: x")
    assert seen["tool"] == "terminal" and seen["kw"] == {"task_id": "task-9"} and seen["args"]["command"] == "script"
    assert dispatch_runner(lambda *a, **k: "not json")("s", "") == (1, "not json")


def test_post_marks_cancelled_as_failed(rt) -> None:
    d = pre(rt, "rm -rf x", "k1")
    rt.post_tool_call(tool_name="terminal", args=d["args"], result="cancelled", tool_call_id="k1", status="cancelled")
    assert rt.store().by_call("k1").status == "failed"
