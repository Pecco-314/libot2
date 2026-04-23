from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction


def init_liver_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS liver (
                room_id INTEGER PRIMARY KEY,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                nickname TEXT
            )
            """,
        )


def _pick_name(uname: str | None, nickname: str | None) -> str | None:
    if nickname is not None and nickname.strip():
        return str(nickname)
    if uname is not None and uname.strip():
        return str(uname)
    return None


def get_name_by_uid(uid: int) -> str | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT uname, nickname FROM liver WHERE uid = ?",
            (uid,),
        ).fetchone()
    if row is None:
        return None
    return _pick_name(row[0], row[1])


def get_name_by_roomid(room_id: int) -> str | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT uname, nickname FROM liver WHERE room_id = ?",
            (room_id,),
        ).fetchone()
    if row is None:
        return None
    return _pick_name(row[0], row[1])


def get_roomid_by_uid(uid: int) -> int | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id FROM liver WHERE uid = ?",
            (uid,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def get_uid_by_roomid(room_id: int) -> int | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT uid FROM liver WHERE room_id = ?",
            (room_id,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def upsert_liver(room_id: int, uid: int | None, uname: str | None, nickname: str | None) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO liver (room_id, uid, uname, nickname)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_id)
            DO UPDATE SET
                uid = COALESCE(excluded.uid, liver.uid),
                uname = COALESCE(excluded.uname, liver.uname),
                nickname = COALESCE(excluded.nickname, liver.nickname)
            """,
            (room_id, uid, uname, nickname),
        )