"""用户跨直播间记录与逐群投递队列；与直播全量 event 统计隔离。"""
from __future__ import annotations

import json
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction
from src.common.push_features import OWN_ROOM_ACTIVITY, OTHER_ROOM_ACTIVITY, room_activity_feature

NOTIFY_COMMANDS = {"DANMU_MSG", "SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE"}


def init_user_activity_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_activity (
                event_key TEXT PRIMARY KEY, uid INTEGER NOT NULL, room_id INTEGER NOT NULL,
                timestamp_ms INTEGER NOT NULL, cmd TEXT NOT NULL, event_json TEXT NOT NULL,
                first_seen INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_activity_user_time ON user_activity(uid, timestamp_ms);
            CREATE TABLE IF NOT EXISTS user_activity_poll (
                uid INTEGER PRIMARY KEY, initialized_at INTEGER NOT NULL,
                last_poll INTEGER NOT NULL, last_full_poll INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_activity_delivery (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL REFERENCES user_activity(event_key),
                group_id INTEGER NOT NULL, subscription_room INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL,
                expires_at INTEGER NOT NULL, sent_at INTEGER, last_error TEXT,
                UNIQUE(event_key, group_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_activity_delivery_due
                ON user_activity_delivery(next_attempt) WHERE sent_at IS NULL;
        """)


def get_poll_state(uid: int, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    with connect_sqlite(db_path) as conn:
        row = conn.execute("SELECT initialized_at,last_poll,last_full_poll FROM user_activity_poll WHERE uid=?", (uid,)).fetchone()
    return dict(zip(("initialized_at", "last_poll", "last_full_poll"), row)) if row else None


def known_event_keys(keys: list[str], db_path: Path = DEFAULT_DB_PATH) -> set[str]:
    known: set[str] = set()
    with connect_sqlite(db_path) as conn:
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            known.update(row[0] for row in conn.execute(
                f"SELECT event_key FROM user_activity WHERE event_key IN ({marks})", chunk))
    return known


def store_user_activity(events: list[dict], uid: int, own_room: int, now: int, *,
                        full: bool, notify_max_age: int = 86400,
                        db_path: Path = DEFAULT_DB_PATH) -> dict:
    inserted = queued = 0
    if any(event["uid"] != uid for event in events):
        raise ValueError("用户不匹配")
    existing = known_event_keys([event["event_key"] for event in events], db_path)
    events = [event for event in events if event["event_key"] not in existing]
    previous = get_poll_state(uid, db_path)
    # 无增量的高频轮询只读；采集状态至多每分钟写一次。
    if not events and previous is not None and not full and now - previous["last_poll"] < 60:
        return {"inserted": 0, "queued": 0, "baseline": False}
    with write_transaction(db_path) as conn:
        state = conn.execute("SELECT initialized_at FROM user_activity_poll WHERE uid=?", (uid,)).fetchone()
        groups = [r[0] for r in conn.execute("SELECT group_id FROM subscription WHERE room_id=?", (own_room,))]
        disabled_features = set()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='command_config'").fetchone():
            disabled_features = set(conn.execute(
                "SELECT group_id,command_name FROM command_config WHERE command_name IN (?,?) AND enabled=0",
                (OWN_ROOM_ACTIVITY, OTHER_ROOM_ACTIVITY)))
        for event in events:
            if event["uid"] != uid:
                raise ValueError("用户不匹配")
            cur = conn.execute("""INSERT OR IGNORE INTO user_activity
                (event_key,uid,room_id,timestamp_ms,cmd,event_json,first_seen) VALUES (?,?,?,?,?,?,?)""",
                (event["event_key"], uid, event["room_id"], event["timestamp_ms"], event["cmd"],
                 json.dumps(event, ensure_ascii=False), now))
            if not cur.rowcount:
                continue
            inserted += 1
            timestamp = event["timestamp_ms"] // 1000
            if (state is None or timestamp < state[0] or event["cmd"] not in NOTIFY_COMMANDS
                    or (event["room_id"] == own_room and event["cmd"] == "DANMU_MSG")
                    or not now - notify_max_age <= timestamp <= now + 60):
                continue
            for group in groups:
                if (group, room_activity_feature(event["room_id"], own_room)) in disabled_features:
                    continue
                conn.execute("""INSERT OR IGNORE INTO user_activity_delivery
                    (event_key,group_id,subscription_room,next_attempt,expires_at) VALUES (?,?,?,?,?)""",
                    (event["event_key"], group, own_room, now, timestamp + notify_max_age))
                queued += 1
        conn.execute("""INSERT INTO user_activity_poll(uid,initialized_at,last_poll,last_full_poll)
            VALUES (?,?,?,?) ON CONFLICT(uid) DO UPDATE SET last_poll=MAX(user_activity_poll.last_poll,excluded.last_poll),
            last_full_poll=CASE WHEN ? THEN MAX(user_activity_poll.last_full_poll,excluded.last_full_poll)
                ELSE user_activity_poll.last_full_poll END""",
            (uid, now, now, now if full else 0, full))
    return {"inserted": inserted, "queued": queued, "baseline": state is None}


def list_pending_deliveries(now: int, limit: int = 20, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with connect_sqlite(db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_activity_delivery'").fetchone():
            return []
        rows = conn.execute("""SELECT d.id,d.group_id,a.event_json,d.subscription_room FROM user_activity_delivery d
            JOIN user_activity a ON a.event_key=d.event_key
            JOIN subscription s ON s.group_id=d.group_id AND s.room_id=d.subscription_room
            WHERE d.sent_at IS NULL AND d.next_attempt<=? AND d.expires_at>=?
            ORDER BY a.timestamp_ms,d.id LIMIT ?""", (now, now, limit)).fetchall()
    return [{"id": r[0], "group_id": r[1], "event": json.loads(r[2]), "subscription_room": r[3]} for r in rows]


def skip_delivery(delivery_id: int, now: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    """关闭功能时将未发送通知过期，重新开启后不补发这条通知。"""
    with write_transaction(db_path) as conn:
        conn.execute("""UPDATE user_activity_delivery SET expires_at=MIN(expires_at,?),
            last_error='互动转发已关闭' WHERE id=? AND sent_at IS NULL""", (now-1, delivery_id))


def finish_delivery(delivery_id: int, now: int, error: str | None = None,
                    db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        if error is None:
            conn.execute("UPDATE user_activity_delivery SET sent_at=?,last_error=NULL WHERE id=?", (now, delivery_id))
        else:
            conn.execute("""UPDATE user_activity_delivery SET attempts=attempts+1,
                next_attempt=? + MIN(3600,30 * (1 << MIN(attempts,7))),last_error=? WHERE id=? AND sent_at IS NULL""",
                (now, error[:500], delivery_id))
