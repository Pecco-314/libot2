from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_manager_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS manager (
                group_id INTEGER NOT NULL,
                manager_qq INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, manager_qq)
            )
            """,
        )


def ensure_initial_manager(group_id: int, initial_manager_qq: int) -> bool:
    with write_transaction() as conn:
        execute_write(
            conn,
            "INSERT OR IGNORE INTO manager (group_id, manager_qq) VALUES (?, ?)",
            (group_id, initial_manager_qq),
        )
    return True


def is_manager(group_id: int, user_id: int) -> bool:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT 1 FROM manager WHERE group_id = ? AND manager_qq = ?",
            (group_id, user_id),
        ).fetchone()
    return row is not None


def list_managers(group_id: int) -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT manager_qq FROM manager WHERE group_id = ? ORDER BY manager_qq ASC",
            (group_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def count_managers(group_id: int) -> int:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT COUNT(1) FROM manager WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def add_manager(group_id: int, user_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(
            conn,
            "INSERT OR IGNORE INTO manager (group_id, manager_qq) VALUES (?, ?)",
            (group_id, user_id),
        )
    return cur.rowcount > 0


def remove_manager(group_id: int, user_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(
            conn,
            "DELETE FROM manager WHERE group_id = ? AND manager_qq = ?",
            (group_id, user_id),
        )
    return cur.rowcount > 0
