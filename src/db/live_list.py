from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_live_list_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS global_live_list (
                room_id INTEGER PRIMARY KEY
            )
            """,
        )
        # 兼容旧版本数据库，追加列
        try:
            conn.execute("ALTER TABLE global_live_list ADD COLUMN uname TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE global_live_list ADD COLUMN adder_uid INTEGER")
        except Exception:
            pass


def add_live_list(room_id: int, uname: str | None, adder_uid: int) -> bool:
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is not None:
            return False
        execute_write(
            conn,
            "INSERT INTO global_live_list (room_id, uname, adder_uid) VALUES (?, ?, ?)",
            (room_id, uname, adder_uid),
        )
        return True


def remove_live_list(room_id: int) -> bool:
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return False
        execute_write(
            conn,
            "DELETE FROM global_live_list WHERE room_id = ?",
            (room_id,),
        )
        return True


def get_live_list_info(room_id: int) -> dict | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id, uname, adder_uid FROM global_live_list WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "room_id": int(row[0]),
            "uname": row[1],
            "adder_uid": int(row[2]) if row[2] else 0,
        }


def get_live_list() -> list[dict]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT room_id, uname, adder_uid FROM global_live_list",
        ).fetchall()
    return [
        {
            "room_id": int(r[0]),
            "uname": r[1],
            "adder_uid": int(r[2]) if r[2] else 0,
        }
        for r in rows
    ]

def update_live_list_uname(room_id: int, uname: str) -> None:
    """
    用于补全历史数据的空白 uname
    """
    with write_transaction() as conn:
        execute_write(
            conn,
            "UPDATE global_live_list SET uname = ? WHERE room_id = ?",
            (uname, room_id),
        )

# 初始化表结构
init_live_list_db()