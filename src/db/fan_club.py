from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


def init_fan_club_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fan_club_target (
                uid INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fan_club_target_alias (
                streamer_uid INTEGER NOT NULL,
                alias TEXT NOT NULL COLLATE NOCASE,
                alias_kind TEXT NOT NULL,
                PRIMARY KEY (streamer_uid, alias),
                FOREIGN KEY (streamer_uid) REFERENCES fan_club_target(uid)
                    ON DELETE CASCADE
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_fan_club_target_alias_lookup
                ON fan_club_target_alias(alias COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS fan_club_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL UNIQUE,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS fan_club_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                streamer_uid INTEGER NOT NULL,
                snapshot_ts INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                reported_count INTEGER,
                member_count INTEGER,
                page_size INTEGER NOT NULL DEFAULT 30,
                expected_pages INTEGER,
                fetched_pages INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                UNIQUE (run_id, streamer_uid),
                FOREIGN KEY (run_id) REFERENCES fan_club_run(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (streamer_uid) REFERENCES fan_club_target(uid)
            );

            CREATE INDEX IF NOT EXISTS idx_fan_club_snapshot_target_complete
                ON fan_club_snapshot(streamer_uid, status, finished_at DESC);

            CREATE TABLE IF NOT EXISTS fan_club_member (
                snapshot_id INTEGER NOT NULL,
                member_uid INTEGER NOT NULL,
                uname TEXT NOT NULL,
                level INTEGER NOT NULL,
                guard_level INTEGER NOT NULL DEFAULT 0,
                user_rank INTEGER NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (snapshot_id, member_uid),
                FOREIGN KEY (snapshot_id) REFERENCES fan_club_snapshot(id)
                    ON DELETE CASCADE
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_fan_club_member_snapshot_rank
                ON fan_club_member(snapshot_id, user_rank, member_uid);

            CREATE TABLE IF NOT EXISTS fan_club_name_history (
                member_uid INTEGER NOT NULL,
                uname TEXT NOT NULL COLLATE NOCASE,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                observation_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (member_uid, uname)
            ) WITHOUT ROWID;

            CREATE INDEX IF NOT EXISTS idx_fan_club_name_history_name
                ON fan_club_name_history(uname COLLATE NOCASE, member_uid);
            """
        )


def sync_targets(
    targets: Iterable[dict[str, Any]],
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    rows = list(targets)
    uids = [int(row["uid"]) for row in rows]
    if len(uids) != len(set(uids)):
        duplicates = sorted({uid for uid in uids if uids.count(uid) > 1})
        raise ValueError(f"duplicate fan-club target UIDs: {duplicates}")

    now = int(time.time())
    with write_transaction(db_path) as conn:
        for row in rows:
            uid = int(row["uid"])
            full_name = str(row["full_name"]).strip()
            short_name = str(row["short_name"]).strip()
            if uid <= 0 or not full_name or not short_name:
                raise ValueError(f"invalid fan-club target: {row!r}")
            conn.execute(
                """
                INSERT INTO fan_club_target
                    (uid, full_name, short_name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    full_name = excluded.full_name,
                    short_name = excluded.short_name,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (uid, full_name, short_name, now, now),
            )
            conn.execute(
                "DELETE FROM fan_club_target_alias WHERE streamer_uid = ?",
                (uid,),
            )
            aliases = [(full_name, "full"), (short_name, "short")]
            aliases.extend(
                (str(alias).strip(), "alias")
                for alias in row.get("aliases", [])
                if str(alias).strip()
            )
            seen_aliases: set[str] = set()
            for alias, kind in aliases:
                key = alias.casefold()
                if key in seen_aliases:
                    continue
                seen_aliases.add(key)
                conn.execute(
                    """
                    INSERT INTO fan_club_target_alias
                        (streamer_uid, alias, alias_kind)
                    VALUES (?, ?, ?)
                    """,
                    (uid, alias, kind),
                )
        if uids:
            placeholders = ",".join("?" for _ in uids)
            conn.execute(
                f"UPDATE fan_club_target SET enabled = 0, updated_at = ? "
                f"WHERE uid NOT IN ({placeholders})",
                (now, *uids),
            )


def list_targets(db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT uid, full_name, short_name
            FROM fan_club_target
            WHERE enabled = 1
            ORDER BY uid
            """
        ).fetchall()
    return [
        {"uid": int(row[0]), "full_name": str(row[1]), "short_name": str(row[2])}
        for row in rows
    ]


def resolve_target(
    query: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    value = query.strip()
    if not value:
        return []
    with connect_sqlite(db_path) as conn:
        if value.isdigit():
            rows = conn.execute(
                """
                SELECT uid, full_name, short_name
                FROM fan_club_target
                WHERE uid = ? AND enabled = 1
                """,
                (int(value),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT t.uid, t.full_name, t.short_name
                FROM fan_club_target AS t
                JOIN fan_club_target_alias AS a ON a.streamer_uid = t.uid
                WHERE a.alias = ? COLLATE NOCASE AND t.enabled = 1
                ORDER BY t.uid
                """,
                (value,),
            ).fetchall()
    return [
        {"uid": int(row[0]), "full_name": str(row[1]), "short_name": str(row[2])}
        for row in rows
    ]


def create_or_resume_run(
    snapshot_date: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    day = snapshot_date or date.today().isoformat()
    now = int(time.time())
    with write_transaction(db_path) as conn:
        target_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM fan_club_target WHERE enabled = 1"
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO fan_club_run
                (snapshot_date, started_at, status, target_count)
            VALUES (?, ?, 'running', ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                status = 'running',
                target_count = excluded.target_count,
                error = NULL
            """,
            (day, now, target_count),
        )
        row = conn.execute(
            """
            SELECT id, snapshot_date, started_at, status, target_count,
                   success_count, failure_count, request_count
            FROM fan_club_run WHERE snapshot_date = ?
            """,
            (day,),
        ).fetchone()
    assert row is not None
    return {
        "id": int(row[0]),
        "snapshot_date": str(row[1]),
        "started_at": int(row[2]),
        "status": str(row[3]),
        "target_count": int(row[4]),
        "success_count": int(row[5]),
        "failure_count": int(row[6]),
        "request_count": int(row[7]),
    }


def completed_target_uids(
    run_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> set[int]:
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT streamer_uid FROM fan_club_snapshot
            WHERE run_id = ? AND status = 'complete'
            """,
            (run_id,),
        ).fetchall()
    return {int(row[0]) for row in rows}


def begin_snapshot(
    run_id: int,
    streamer_uid: int,
    snapshot_ts: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    now = int(time.time())
    with write_transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO fan_club_snapshot
                (run_id, streamer_uid, snapshot_ts, started_at, status)
            VALUES (?, ?, ?, ?, 'running')
            ON CONFLICT(run_id, streamer_uid) DO UPDATE SET
                snapshot_ts = excluded.snapshot_ts,
                started_at = excluded.started_at,
                finished_at = NULL,
                status = 'running',
                reported_count = NULL,
                member_count = NULL,
                expected_pages = NULL,
                fetched_pages = 0,
                request_count = 0,
                error = NULL
            """,
            (run_id, streamer_uid, snapshot_ts, now),
        )
        row = conn.execute(
            "SELECT id FROM fan_club_snapshot WHERE run_id = ? AND streamer_uid = ?",
            (run_id, streamer_uid),
        ).fetchone()
        assert row is not None
        snapshot_id = int(row[0])
        conn.execute("DELETE FROM fan_club_member WHERE snapshot_id = ?", (snapshot_id,))
    return snapshot_id


def save_complete_snapshot(
    snapshot_id: int,
    *,
    reported_count: int,
    expected_pages: int,
    fetched_pages: int,
    request_count: int,
    members: Iterable[dict[str, Any]],
    observed_at: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    member_rows = list(members)
    with write_transaction(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO fan_club_member
                (snapshot_id, member_uid, uname, level, guard_level,
                 user_rank, score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    int(row["uid"]),
                    str(row["uname"]),
                    int(row["level"]),
                    int(row.get("guard_level") or 0),
                    int(row["user_rank"]),
                    int(row.get("score") or 0),
                )
                for row in member_rows
            ],
        )
        conn.executemany(
            """
            INSERT INTO fan_club_name_history
                (member_uid, uname, first_seen, last_seen, observation_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(member_uid, uname) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen),
                observation_count = observation_count + 1
            """,
            [
                (int(row["uid"]), str(row["uname"]), observed_at, observed_at)
                for row in member_rows
            ],
        )
        conn.execute(
            """
            UPDATE fan_club_snapshot SET
                finished_at = ?, status = 'complete', reported_count = ?,
                member_count = ?, expected_pages = ?, fetched_pages = ?,
                request_count = ?, error = NULL
            WHERE id = ?
            """,
            (
                int(time.time()),
                reported_count,
                len(member_rows),
                expected_pages,
                fetched_pages,
                request_count,
                snapshot_id,
            ),
        )


def save_failed_snapshot(
    snapshot_id: int,
    *,
    reported_count: int | None,
    expected_pages: int | None,
    fetched_pages: int,
    request_count: int,
    error: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with write_transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE fan_club_snapshot SET
                finished_at = ?, status = 'failed', reported_count = ?,
                member_count = NULL, expected_pages = ?, fetched_pages = ?,
                request_count = ?, error = ?
            WHERE id = ?
            """,
            (
                int(time.time()),
                reported_count,
                expected_pages,
                fetched_pages,
                request_count,
                error[:2000],
                snapshot_id,
            ),
        )


def finish_run(
    run_id: int,
    *,
    request_count: int,
    error: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, int | str]:
    with write_transaction(db_path) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) FROM fan_club_snapshot
            WHERE run_id = ? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        target_count = int(
            conn.execute(
                "SELECT target_count FROM fan_club_run WHERE id = ?", (run_id,)
            ).fetchone()[0]
        )
        success_count = counts.get("complete", 0)
        failure_count = target_count - success_count
        status = "complete" if success_count == target_count else "partial"
        actual_request_count = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(request_count), 0)
                FROM fan_club_snapshot WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE fan_club_run SET
                finished_at = ?, status = ?, success_count = ?,
                failure_count = ?, request_count = ?, error = ?
            WHERE id = ?
            """,
            (
                int(time.time()),
                status,
                success_count,
                failure_count,
                max(request_count, actual_request_count),
                error[:2000] if error else None,
                run_id,
            ),
        )
    return {
        "status": status,
        "target_count": target_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "request_count": max(request_count, actual_request_count),
    }


def latest_complete_snapshot(
    streamer_uid: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.id, s.streamer_uid, t.full_name, t.short_name,
                   r.snapshot_date, s.snapshot_ts, s.finished_at,
                   s.reported_count, s.member_count
            FROM fan_club_snapshot AS s
            JOIN fan_club_run AS r ON r.id = s.run_id
            JOIN fan_club_target AS t ON t.uid = s.streamer_uid
            WHERE s.streamer_uid = ? AND s.status = 'complete'
            ORDER BY r.snapshot_date DESC, s.finished_at DESC, s.id DESC
            LIMIT 1
            """,
            (streamer_uid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "streamer_uid": int(row[1]),
        "full_name": str(row[2]),
        "short_name": str(row[3]),
        "snapshot_date": str(row[4]),
        "snapshot_ts": int(row[5]),
        "finished_at": int(row[6]),
        "reported_count": int(row[7]),
        "member_count": int(row[8]),
    }


def complete_snapshot_for_date(
    streamer_uid: int,
    snapshot_date: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    day = snapshot_date or date.today().isoformat()
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.id, s.streamer_uid, t.full_name, t.short_name,
                   r.snapshot_date, s.snapshot_ts, s.finished_at,
                   s.reported_count, s.member_count
            FROM fan_club_snapshot AS s
            JOIN fan_club_run AS r ON r.id = s.run_id
            JOIN fan_club_target AS t ON t.uid = s.streamer_uid
            WHERE s.streamer_uid = ? AND r.snapshot_date = ?
              AND s.status = 'complete'
            ORDER BY s.finished_at DESC, s.id DESC
            LIMIT 1
            """,
            (streamer_uid, day),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "streamer_uid": int(row[1]),
        "full_name": str(row[2]),
        "short_name": str(row[3]),
        "snapshot_date": str(row[4]),
        "snapshot_ts": int(row[5]),
        "finished_at": int(row[6]),
        "reported_count": int(row[7]),
        "member_count": int(row[8]),
    }


def snapshot_state_for_date(
    streamer_uid: int,
    snapshot_date: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    day = snapshot_date or date.today().isoformat()
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.status, s.started_at, s.finished_at, s.error,
                   s.fetched_pages, s.expected_pages
            FROM fan_club_snapshot AS s
            JOIN fan_club_run AS r ON r.id = s.run_id
            WHERE s.streamer_uid = ? AND r.snapshot_date = ?
            ORDER BY s.id DESC LIMIT 1
            """,
            (streamer_uid, day),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": str(row[0]),
        "started_at": int(row[1]),
        "finished_at": int(row[2]) if row[2] is not None else None,
        "error": str(row[3]) if row[3] is not None else None,
        "fetched_pages": int(row[4]),
        "expected_pages": int(row[5]) if row[5] is not None else None,
    }


def list_snapshot_members(
    snapshot_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT member_uid, uname, level, guard_level, user_rank
            FROM fan_club_member
            WHERE snapshot_id = ?
            ORDER BY user_rank ASC, member_uid ASC
            """,
            (snapshot_id,),
        ).fetchall()
    return [
        {
            "uid": int(row[0]),
            "uname": str(row[1]),
            "level": int(row[2]),
            "guard_level": int(row[3]),
            "user_rank": int(row[4]),
        }
        for row in rows
    ]


def list_common_snapshot_members(
    snapshot_ids: list[int],
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    ids = [int(snapshot_id) for snapshot_id in snapshot_ids]
    if not 2 <= len(ids) <= 5 or len(ids) != len(set(ids)):
        raise ValueError("snapshot_ids must contain 2 to 5 unique IDs")

    placeholders = ",".join("?" for _ in ids)
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            WITH common AS (
                SELECT member_uid, SUM(user_rank) AS rank_sum
                FROM fan_club_member
                WHERE snapshot_id IN ({placeholders})
                GROUP BY member_uid
                HAVING COUNT(*) = ?
            )
            SELECT m.snapshot_id, m.member_uid, m.uname, m.level,
                   m.guard_level, m.user_rank, common.rank_sum
            FROM fan_club_member AS m
            JOIN common ON common.member_uid = m.member_uid
            WHERE m.snapshot_id IN ({placeholders})
            ORDER BY common.rank_sum ASC, m.member_uid ASC
            """,
            (*ids, len(ids), *ids),
        ).fetchall()

    by_uid: dict[int, dict[str, Any]] = {}
    for row in rows:
        member_uid = int(row[1])
        item = by_uid.setdefault(
            member_uid,
            {
                "uid": member_uid,
                "rank_sum": int(row[6]),
                "by_snapshot": {},
            },
        )
        item["by_snapshot"][int(row[0])] = {
            "uid": member_uid,
            "uname": str(row[2]),
            "level": int(row[3]),
            "guard_level": int(row[4]),
            "user_rank": int(row[5]),
        }

    result: list[dict[str, Any]] = []
    for item in by_uid.values():
        memberships = [item["by_snapshot"][snapshot_id] for snapshot_id in ids]
        result.append(
            {
                "uid": item["uid"],
                "uname": memberships[0]["uname"],
                "memberships": memberships,
                "rank_sum": item["rank_sum"],
            }
        )
    return result


def list_latest_member_medals(
    member_uid: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """列出用户在每个抓取对象最新完整快照中的牌子。"""
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            WITH latest_snapshot AS (
                SELECT s.id, s.streamer_uid, r.snapshot_date,
                       ROW_NUMBER() OVER (
                           PARTITION BY s.streamer_uid
                           ORDER BY r.snapshot_date DESC,
                                    s.finished_at DESC,
                                    s.id DESC
                       ) AS row_num
                FROM fan_club_snapshot AS s
                JOIN fan_club_run AS r ON r.id = s.run_id
                WHERE s.status = 'complete'
            )
            SELECT latest_snapshot.streamer_uid,
                   target.full_name, target.short_name,
                   member.level, member.guard_level,
                   latest_snapshot.snapshot_date
            FROM latest_snapshot
            JOIN fan_club_member AS member
              ON member.snapshot_id = latest_snapshot.id
            JOIN fan_club_target AS target
              ON target.uid = latest_snapshot.streamer_uid
            WHERE latest_snapshot.row_num = 1
              AND member.member_uid = ?
            ORDER BY member.level DESC, latest_snapshot.streamer_uid ASC
            """,
            (int(member_uid),),
        ).fetchall()
    return [
        {
            "target_uid": int(row[0]),
            "target_name": str(row[1]),
            "full_name": str(row[1]),
            "level": int(row[3]),
            "guard_level": int(row[4]),
            "snapshot_date": str(row[5]),
        }
        for row in rows
    ]


def run_status(
    snapshot_date: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    day = snapshot_date or date.today().isoformat()
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, snapshot_date, started_at, finished_at, status,
                   target_count, success_count, failure_count, request_count, error
            FROM fan_club_run WHERE snapshot_date = ?
            """,
            (day,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "snapshot_date": str(row[1]),
        "started_at": int(row[2]),
        "finished_at": int(row[3]) if row[3] is not None else None,
        "status": str(row[4]),
        "target_count": int(row[5]),
        "success_count": int(row[6]),
        "failure_count": int(row[7]),
        "request_count": int(row[8]),
        "error": row[9],
    }
