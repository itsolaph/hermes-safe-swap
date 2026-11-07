"""Load Safe-Swap through Hermes' real plugin loader (isolated HOME/HERMES_HOME) and drive real dispatch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

hermes_cli = pytest.importorskip("hermes_cli", reason="needs a Hermes checkout on sys.path")
ROOT = Path(__file__).resolve().parent.parent
HERMES_SRC = Path(hermes_cli.__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("integration")
    home, work = base / "home", base / "work"
    hermes_home = home / ".hermes"
    plugin = hermes_home / "plugins" / "safe-swap"
    work.mkdir(parents=True)
    plugin.mkdir(parents=True)
    for f in ROOT.glob("*.py"):
        if f.name != "conftest.py":
            shutil.copy(f, plugin / f.name)
    shutil.copy(ROOT / "plugin.yaml", plugin / "plugin.yaml")
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n    - safe-swap\n")
    env = {
        **os.environ, "HOME": str(home), "HERMES_HOME": str(hermes_home), "TERMINAL_CWD": str(work),
        "SS_WORK": str(work), "SS_HERMES_SRC": str(HERMES_SRC), "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": str(base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
    }
    p = subprocess.run([sys.executable, str(Path(__file__).parent / "integration_driver.py")], env=env,
                       capture_output=True, text=True, timeout=300, cwd=work)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("SS_JSON:")]
    assert lines, f"driver failed:\n{p.stdout[-2000:]}\n{p.stderr[-3000:]}"
    data = json.loads(lines[-1][len("SS_JSON:"):])
    data["home"] = str(home)
    return data


def test_plugin_loads(out) -> None:
    assert out["loaded"] is True and out["error"] is None
    assert {"swaps", "unswap"} <= set(out["commands"])


def test_rm_is_swapped_executed_and_explained(out) -> None:
    rm = out["rm"]
    assert rm["exists_after"] is False
    assert rm["result"]["exit_code"] == 0
    assert "[safe-swap] `rm -rf build` was run as: move-to-quarantine" in rm["result"]["output"]
    assert "Undo: /unswap" in rm["result"]["output"]
    assert Path(out["home"], ".safe-swap", "trash").is_dir()


def test_swaps_and_unswap_round_trip(out) -> None:
    assert "applied" in out["swaps"] and "rm -rf build" in out["swaps"]
    assert out["unswap"].startswith("Undone")
    assert out["restored"] == "payload"
    assert "undone" in out["swaps_after"]


def test_git_calls_are_noted_and_effective(out) -> None:
    assert out["reset_content"] == "one\n"
    assert "preceded by a stash snapshot" in out["reset"]["output"]
    assert "--force-with-lease --force-if-includes" in out["push_note"]["output"]
    assert "Not undoable" in out["push_note"]["output"]


def test_modify_directive_is_applied_by_real_dispatch(out) -> None:
    assert out["modify_applied"] and "SS_RCPT=" in out["modify_applied"]["command"]
    assert out["noop_command"] is None


def test_fails_open_on_garbage_and_oversize(out) -> None:
    assert all(v is None for v in out["garbage"].values()), out["garbage"]


def test_fails_open_when_database_is_unwritable(out) -> None:
    assert out["db_unwritable"] is None


def test_latency_budget(out) -> None:
    for name, stats in out["latency"].items():
        assert stats["p95_ms"] < 20, (name, stats)
