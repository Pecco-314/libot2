from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "libot.db"
SQLITE_CACHE_KIB = 64 * 1024


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back a ``with`` block, then release its file handles."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_sqlite(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, factory=_ClosingConnection)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
    return conn


def execute_write(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Cursor:
    return conn.execute(sql, params)


@contextmanager
def write_transaction(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect_sqlite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
