"""流式读取区间互动，内存仅保留每个用户的计数和最新非空名字。"""
from pathlib import Path

from src.common.danmaku_rank import build_danmaku_rank_items
from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite


def query_danmaku_rank(room_id: int, start_ts: int, end_ts: int, limit: int = 20,
                       db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """查询[start_ts, end_ts)，沿用包含礼物等有UID互动的榜单口径。"""
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT uid, uname, content FROM event INDEXED BY idx_event_room_time
            WHERE room_id = ? AND timestamp >= ? AND timestamp < ?
              AND uid IS NOT NULL AND uid != 0
            ORDER BY timestamp ASC, id ASC
            """, (room_id, start_ts, end_ts),
        )
        return build_danmaku_rank_items(
            ({"uid": row[0], "uname": row[1], "content": row[2]} for row in rows), limit,
        )
