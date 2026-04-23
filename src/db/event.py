from __future__ import annotations

from src.db.sqlite import connect_sqlite


def get_newest_live_event() -> dict[str, object] | None:
    live_cmds = ["LIVE", "PREPARING", "ROOM_CHANGE"]
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT id, cmd, room_id, title FROM event WHERE cmd IN (?, ?, ?) ORDER BY created_at DESC, id DESC LIMIT 1",
            tuple(live_cmds),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "cmd": str(row[1]),
        "room_id": int(row[2]),
        "title": str(row[3]) if row[3] is not None else None,
    }