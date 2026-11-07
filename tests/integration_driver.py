"""Runs INSIDE a subprocess whose HOME and HERMES_HOME point at a scratch dir (see test_integration).

Loads Safe-Swap through Hermes' real plugin loader, drives the real tool dispatch and prints one
JSON document with everything the test asserts on.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

work = Path(os.environ["SS_WORK"])
sys.path.insert(0, os.environ["SS_HERMES_SRC"])

from hermes_cli import plugins  # noqa: E402

plugins.discover_plugins()
manager = plugins.get_plugin_manager()
loaded = manager._plugins["safe-swap"]
out: dict = {"loaded": loaded.enabled, "error": loaded.error}

import model_tools  # noqa: E402


def call(command: str, call_id: str) -> dict:
    return json.loads(model_tools.handle_function_call("terminal", {"command": command}, task_id="t1", tool_call_id=call_id, session_id="s1"))


def directive(command):
    return plugins._get_pre_tool_call_directive_details("terminal", {"command": command}, tool_call_id="x", session_id="s", task_id="t").modified_args


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


# 1. rm through the real dispatch: modified, executed, noted, receipt settled
(work / "build").mkdir()
(work / "build" / "f.txt").write_text("payload")
res = call("rm -rf build", "call_rm")
out["rm"] = {"result": res, "exists_after": (work / "build").exists()}

# 2. commands the plugin registered
cmds = manager._plugin_commands
out["commands"] = sorted(cmds)
out["swaps"] = cmds["swaps"]["handler"]("")
rid = out["swaps"].splitlines()[1].split()[0]
out["unswap"] = cmds["unswap"]["handler"](rid)
out["restored"] = (work / "build" / "f.txt").read_text() if (work / "build" / "f.txt").exists() else None
out["swaps_after"] = cmds["swaps"]["handler"]("")

# 3. git: reset --hard snapshot + force push lease, correlated by tool_call_id / marker
repo = work / "repo"
repo.mkdir()
git("init", "-q", "-b", "main", cwd=repo)
git("config", "user.name", "t", cwd=repo)
git("config", "user.email", "t@example.invalid", cwd=repo)
(repo / "a.txt").write_text("one\n")
git("add", "a.txt", cwd=repo)
git("commit", "-qm", "first", cwd=repo)
(repo / "a.txt").write_text("edited\n")
out["reset"] = call(f"cd {repo} && git reset --hard", "call_reset")
out["reset_content"] = (repo / "a.txt").read_text()
out["push_note"] = call(f"cd {repo} && git push -f origin main", "call_push")

# 4. fail-open on garbage
huge = "rm -rf x " + "a" * 200_000
out["garbage"] = {repr(g)[:30]: directive(g) for g in (None, 5, b"rm -rf x", ["rm"], {"a": 1}, "", huge)}
out["noop_command"] = directive("ls -la")
out["modify_applied"] = directive("rm -rf somewhere")

# 5. unwritable DB: make the plugin data dir a plain file
data = Path(os.environ["HERMES_HOME"]) / "plugin-data"
for child in data.iterdir():
    for sub in child.iterdir():
        sub.unlink()
    child.rmdir()
(data / "safe-swap").write_text("not a directory")
out["db_unwritable"] = directive("rm -rf somewhere")
(data / "safe-swap").unlink()

# 6. latency (hook only: dispatch of pre_tool_call through the real directive path)
def timed(command: str, n: int = 300) -> dict:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        directive(command)
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return {"p50_ms": round(statistics.median(samples), 3), "p95_ms": round(samples[int(n * 0.95)], 3), "max_ms": round(samples[-1], 3)}


out["latency"] = {
    "no_match (ls -la)": timed("ls -la"),
    "keyword_no_rule (git status)": timed("git status"),
    "rewrite rm -rf build": timed("rm -rf build"),
    "rewrite git push -f": timed("git push -f origin main"),
    "compound": timed("cd src && rm -rf build dist && git push --force origin main | tee out.log"),
}
print("SS_JSON:" + json.dumps(out))
