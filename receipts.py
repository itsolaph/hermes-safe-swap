"""Receipt store: one small SQLite file in the plugin's own data directory."""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = ["Receipt", "Store", "new_id"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY, ts REAL NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL, kind TEXT NOT NULL, original TEXT NOT NULL, rewritten TEXT NOT NULL, workdir TEXT NOT NULL,
    qdir TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER
)
"""
_COLUMNS = "id, ts, session_id, task_id, tool_call_id, kind, original, rewritten, workdir, qdir, status, exit_code"


@dataclass(frozen=True)
class Receipt:
    id: str
    ts: float
    session_id: str
    task_id: str
    tool_call_id: str
    kind: str
    original: str
    rewritten: str
    workdir: str
    qdir: str
    status: str  # pending | applied | failed | undone | purged
    exit_code: int | None

    @property
    def kinds(self) -> list[str]:
        return self.kind.split(",")


def new_id() -> str:
    return secrets.token_hex(3)


class Store:
    """Opens a short-lived connection per call, so it is safe across threads and profiles."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=1.0)
        # No fsync per hook call: the quarantine files are the source of truth, the row is an index.
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(_SCHEMA)
        return conn

    def _rows(self, sql: str, args: tuple = ()) -> Iterator[Receipt]:
        with closing(self._connect()) as conn:
            for row in conn.execute(sql, args).fetchall():
                yield Receipt(*row)

    def insert_pending(self, r: Receipt) -> None:
        """Commit the receipt before the rewrite is handed back; raises on id collision."""
        with closing(self._connect()) as conn, conn:
            conn.execute(f"INSERT INTO receipts ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                r.id, r.ts, r.session_id, r.task_id, r.tool_call_id, r.kind, r.original, r.rewritten,
                r.workdir, r.qdir, "pending", None,
            ))

    def get(self, rid: str) -> Receipt | None:
        return next(self._rows(f"SELECT {_COLUMNS} FROM receipts WHERE id = ?", (rid,)), None)

    def by_call(self, tool_call_id: str) -> Receipt | None:
        if not tool_call_id:
            return None
        return next(self._rows(
            f"SELECT {_COLUMNS} FROM receipts WHERE tool_call_id = ? ORDER BY ts DESC LIMIT 1", (tool_call_id,)), None)

    def recent(self, limit: int = 10) -> list[Receipt]:
        return list(self._rows(f"SELECT {_COLUMNS} FROM receipts ORDER BY ts DESC LIMIT ?", (limit,)))

    def last_undoable(self) -> Receipt | None:
        return next(self._rows(
            f"SELECT {_COLUMNS} FROM receipts WHERE status IN ('applied','failed','pending') "
            "AND kind != 'push' ORDER BY ts DESC LIMIT 1"), None)

    def older_than(self, seconds: float) -> list[Receipt]:
        cutoff = time.time() - seconds
        return list(self._rows(f"SELECT {_COLUMNS} FROM receipts WHERE ts < ? AND status != 'purged'", (cutoff,)))

    def set_status(self, rid: str, status: str, exit_code: int | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE receipts SET status = ?, exit_code = COALESCE(?, exit_code) WHERE id = ?",
                         (status, exit_code, rid))

    def settle(self, rid: str, status: str, exit_code: int | None) -> None:
        """post_tool_call: pending -> applied/failed; never overwrites undone/purged."""
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE receipts SET status = ?, exit_code = ? WHERE id = ? AND status = 'pending'",
                         (status, exit_code, rid))
