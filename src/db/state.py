from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_state_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )


def get_state(key: str) -> str | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def set_state(key: str, value: str) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
