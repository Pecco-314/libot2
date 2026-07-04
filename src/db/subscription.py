# --- START OF FILE subscription.py ---
from __future__ import annotations

from src.db.sqlite import connect_sqlite, execute_write, write_transaction

# 功能列白名单，防止 SQL 注入。新增功能只需要在这里加一个字段名。
VALID_FEATURES = {
    "dev",
    "mention_all",
    "leave_notice",
    "join_notice",
    "enable_asr"
}

def init_subscription_db() -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS subscription (
                group_id INTEGER PRIMARY KEY,
                room_id INTEGER NOT NULL,
                dev INTEGER NOT NULL DEFAULT 0,
                mention_all INTEGER NOT NULL DEFAULT 0,
                leave_notice INTEGER NOT NULL DEFAULT 1,
                join_notice INTEGER NOT NULL DEFAULT 1,
                enable_asr INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )

        # 如果是老表没有 join_notice 列，尝试 ALTER TABLE 添加该列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(subscription)").fetchall()]
        if "join_notice" not in cols:
            execute_write(
                conn,
                "ALTER TABLE subscription ADD COLUMN join_notice INTEGER NOT NULL DEFAULT 1",
            )


def set_subscription(group_id: int, room_id: int) -> None:
    with write_transaction() as conn:
        execute_write(
            conn,
            """
            INSERT INTO subscription (group_id, room_id)
            VALUES (?, ?)
            ON CONFLICT(group_id)
            DO UPDATE SET room_id = excluded.room_id, updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, room_id),
        )


def set_subscription_feature(group_id: int, feature_col: str, enabled: bool) -> bool:
    """通用方法：设置任意功能开关"""
    if feature_col not in VALID_FEATURES:
        raise ValueError(f"Invalid feature column: {feature_col}")

    with write_transaction() as conn:
        cur = execute_write(
            conn,
            f"""
            UPDATE subscription
            SET {feature_col} = ?, updated_at = CURRENT_TIMESTAMP
            WHERE group_id = ?
            """,
            (1 if enabled else 0, group_id),
        )
    # 如果 rowcount > 0 说明该群有订阅记录并更新成功
    return cur.rowcount > 0


def get_subscription_feature(group_id: int, feature_col: str) -> bool:
    """通用方法：获取任意功能开关状态"""
    if feature_col not in VALID_FEATURES:
        raise ValueError(f"Invalid feature column: {feature_col}")

    with connect_sqlite() as conn:
        row = conn.execute(
            f"SELECT {feature_col} FROM subscription WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        
    if row is None:
        # 如果群还没有订阅，退群通知依然当作默认开启，其他为关闭
        if feature_col == "leave_notice":
            return True
        return False
        
    return bool(int(row[0]))


def get_subscription(group_id: int) -> int | None:
    with connect_sqlite() as conn:
        row = conn.execute(
            "SELECT room_id FROM subscription WHERE group_id = ?",
            (group_id,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0])


def remove_subscription(group_id: int) -> bool:
    with write_transaction() as conn:
        cur = execute_write(conn, "DELETE FROM subscription WHERE group_id = ?", (group_id,))
    return cur.rowcount > 0


def list_subscribed_room_ids() -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute("SELECT DISTINCT room_id FROM subscription ORDER BY room_id ASC").fetchall()
    return [int(row[0]) for row in rows]


def list_asr_enabled_room_ids() -> list[int]:
    """
    核心聚合查询：获取至少有一个群开启了 ASR 的直播间 room_id 列表。
    节约系统性能，按需开启听歌识曲。
    """
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT DISTINCT room_id FROM subscription WHERE enable_asr = 1 ORDER BY room_id ASC"
        ).fetchall()
    return [int(row[0]) for row in rows]


def list_subscribed_group_ids(room_id: int) -> list[int]:
    with connect_sqlite() as conn:
        rows = conn.execute(
            "SELECT group_id FROM subscription WHERE room_id = ? ORDER BY group_id ASC",
            (room_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]