"""Helpers: run rewritten commands for real, under bash and dash, in a throwaway HOME."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from safe_swap.config import Config, quarantine_expr
from safe_swap.rewriter import rewrite
from safe_swap.undo import restore_script

SHELLS = [s for s in ("bash", "dash") if shutil.which(s)]


@dataclass
class Result:
    code: int
    out: str
    err: str


class Sandbox:
    def __init__(self, root: Path, shell: str) -> None:
        self.root, self.shell = root, shell
        self.home = root / "home"
        self.work = root / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.home), "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        }
        self.cfg = Config()
        self.rid = "a1b2c3"

    def sh(self, script: str, cwd: Path | None = None) -> Result:
        p = subprocess.run([self.shell, "-c", script], cwd=cwd or self.work, env=self.env, capture_output=True, text=True)
        return Result(p.returncode, p.stdout, p.stderr)

    def swap(self, command: str, cwd: Path | None = None, rid: str | None = None) -> Result:
        rid = rid or self.rid
        r = rewrite(command, self.cfg, rid)
        assert r is not None, f"not rewritten: {command}"
        return self.sh(r.command, cwd)

    def undo(self, rid: str | None = None, force: bool = False) -> Result:
        return self.sh(restore_script(quarantine_expr(self.cfg.quarantine_dir), rid or self.rid, force=force))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.work, env=self.env, capture_output=True, text=True, check=True)
        return p.stdout.strip()

    def init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", cwd=path)
        self.git("config", "user.name", "t", cwd=path)
        self.git("config", "user.email", "t", cwd=path)
        return path
