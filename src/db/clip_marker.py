from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


def init_clip_marker_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS clip_marker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            session_start INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            UNIQUE(group_id, message_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clip_marker_session ON clip_marker(room_id, session_start, timestamp, id)")


def add_clip_marker(room_id: int, session_start: int, timestamp: int, user_id: int,
                    group_id: int, message_id: int, note: str,
                    db_path: Path = DEFAULT_DB_PATH) -> None:
    if timestamp < session_start:
        raise ValueError("消息时间早于本场开播时间")
    init_clip_marker_db(db_path)
    with write_transaction(db_path) as conn:
        conn.execute("""INSERT INTO clip_marker
            (room_id,session_start,timestamp,user_id,group_id,message_id,note)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO NOTHING""",
            (room_id, session_start, timestamp, user_id, group_id, message_id, note))


def list_clip_markers(room_id: int, session_start: int,
                      db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    init_clip_marker_db(db_path)
    with connect_sqlite(db_path) as conn:
        rows = conn.execute("""SELECT timestamp,note FROM clip_marker
            WHERE room_id=? AND session_start=? ORDER BY timestamp,id""",
            (room_id, session_start)).fetchall()
    return [{"timestamp": row[0], "note": row[1]} for row in rows]


def format_clip_marker(timestamp: int, session_start: int, note: str) -> str:
    clock = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
    seconds = max(0, timestamp - session_start)
    elapsed = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"[{clock}] ({elapsed}) {' '.join(note.split())}".rstrip()
