from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


def _normalize_abbreviation(value: str) -> str:
    return value.strip().casefold()


def _normalize_expansion(value: str) -> str:
    return " ".join(value.strip().split())


def is_valid_abbreviation(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9]{2,64}", value.strip(), re.ASCII) is not None


def init_abbreviation_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS abbreviation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                abbreviation TEXT NOT NULL,
                expansion TEXT NOT NULL,
                adder_uid INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (abbreviation, expansion)
            );

            CREATE INDEX IF NOT EXISTS idx_abbreviation_lookup
                ON abbreviation(abbreviation, id);
            """
        )


def add_abbreviation(
    abbreviation: str,
    expansion: str,
    adder_uid: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    key = _normalize_abbreviation(abbreviation)
    value = _normalize_expansion(expansion)
    if not is_valid_abbreviation(abbreviation):
        raise ValueError("abbreviation must contain 2-64 ASCII letters or digits")
    if not value:
        raise ValueError("expansion must not be empty")
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO abbreviation
                (abbreviation, expansion, adder_uid)
            VALUES (?, ?, ?)
            """,
            (key, value, int(adder_uid)),
        )
    return cursor.rowcount > 0


def list_abbreviations(
    abbreviation: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    key = _normalize_abbreviation(abbreviation)
    if not key:
        return []
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, abbreviation, expansion, adder_uid, created_at
            FROM abbreviation
            WHERE abbreviation = ?
            ORDER BY id ASC
            """,
            (key,),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "abbreviation": str(row[1]),
            "expansion": str(row[2]),
            "adder_uid": int(row[3]),
            "created_at": str(row[4]),
        }
        for row in rows
    ]


def remove_abbreviation(
    record_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM abbreviation WHERE id = ?",
            (int(record_id),),
        )
    return cursor.rowcount > 0


def remove_abbreviations(
    record_ids: list[int],
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    ids = sorted({int(record_id) for record_id in record_ids})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            f"DELETE FROM abbreviation WHERE id IN ({placeholders})",
            ids,
        )
    return cursor.rowcount
