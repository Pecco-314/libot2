from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_subscription_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS subscription (
                group_id INTEGER PRIMARY KEY,
                room_id INTEGER NOT NULL,
                dev INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )


def set_subscription(group_id: int, room_id: int) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO subscription (group_id, room_id, dev)
            VALUES (?, ?, 0)
            ON CONFLICT(group_id)
            DO UPDATE SET room_id = excluded.room_id, updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, room_id),
        )


def set_subscription_dev(group_id: int, enabled: bool) -> bool:
    with write_transaction() as conn:
        cur = execute_write(
            conn,
            """
            UPDATE subscription
            SET dev = ?, updated_at = CURRENT_TIMESTAMP
            WHERE group_id = ?
            """,
            (1 if enabled else 0, group_id),
        )
    return cur.rowcount > 0


def get_subscription(group_id: int) -> int | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id FROM subscription WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def is_subscription_dev_enabled(group_id: int) -> bool:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT dev FROM subscription WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    if row is None:
        return False
    return bool(int(row[0]))


def remove_subscription(group_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(conn, "DELETE FROM subscription WHERE group_id = ?", (group_id,))
    return cur.rowcount > 0


def list_subscribed_room_ids() -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute("SELECT DISTINCT room_id FROM subscription ORDER BY room_id ASC").fetchall()
    return [int(row[0]) for row in rows]


def list_subscribed_group_ids(room_id: int) -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT group_id FROM subscription WHERE room_id = ? ORDER BY group_id ASC",
            (room_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]