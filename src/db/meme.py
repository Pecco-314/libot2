from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
DAILY_MEME_NAMES = ("老婆", "老公")


class MemePoolExhausted(LookupError):
    """Raised when every approved item has been assigned for the date."""


def _normalize_name(value: str) -> str:
    return value.strip().casefold()


def _clean_content(value: str) -> str:
    return value.strip()


def is_valid_meme_name(value: str) -> bool:
    return re.fullmatch(r"\S{1,32}", value.strip()) is not None


def init_meme_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meme_group (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meme_alias (
                alias_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                meme_group_id INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (meme_group_id) REFERENCES meme_group(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_meme_alias_group
                ON meme_alias(meme_group_id);

            CREATE TABLE IF NOT EXISTS meme_entry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meme_group_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                adder_uid INTEGER NOT NULL,
                entry_type TEXT NOT NULL DEFAULT 'text'
                    CHECK (entry_type IN ('text', 'image')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                reviewed_at TIMESTAMP,
                reviewed_by TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (meme_group_id) REFERENCES meme_group(id) ON DELETE CASCADE,
                UNIQUE (meme_group_id, content)
            );

            CREATE INDEX IF NOT EXISTS idx_meme_entry_group
                ON meme_entry(meme_group_id, id);

            CREATE TABLE IF NOT EXISTS meme_daily_draw (
                user_uid INTEGER NOT NULL,
                draw_date TEXT NOT NULL,
                draw_key TEXT NOT NULL,
                meme_entry_id INTEGER,
                content TEXT NOT NULL,
                entry_type TEXT NOT NULL
                    CHECK (entry_type IN ('text', 'image')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_uid, draw_date, draw_key),
                FOREIGN KEY (meme_entry_id) REFERENCES meme_entry(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_meme_daily_draw_date
                ON meme_daily_draw(draw_date, draw_key);

            """
        )
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(meme_entry)")
        }
        if "status" not in columns:
            conn.execute(
                """
                ALTER TABLE meme_entry
                ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected'))
                """
            )
        if "reviewed_at" not in columns:
            conn.execute("ALTER TABLE meme_entry ADD COLUMN reviewed_at TIMESTAMP")
        if "reviewed_by" not in columns:
            conn.execute("ALTER TABLE meme_entry ADD COLUMN reviewed_by TEXT")
        if "entry_type" not in columns:
            conn.execute(
                """
                ALTER TABLE meme_entry
                ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'text'
                    CHECK (entry_type IN ('text', 'image'))
                """
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_meme_entry_status
            ON meme_entry(status, created_at, id)
            """
        )


def _find_group_id(conn, name: str) -> int | None:
    row = conn.execute(
        "SELECT meme_group_id FROM meme_alias WHERE alias_key = ?",
        (_normalize_name(name),),
    ).fetchone()
    return int(row[0]) if row else None


def _create_group(conn, name: str) -> int:
    display_name = name.strip()
    cursor = conn.execute(
        "INSERT INTO meme_group (display_name) VALUES (?)",
        (display_name,),
    )
    group_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO meme_alias (alias_key, display_name, meme_group_id)
        VALUES (?, ?, ?)
        """,
        (_normalize_name(display_name), display_name, group_id),
    )
    return group_id


def add_meme(
    name: str,
    content: str,
    adder_uid: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    if not is_valid_meme_name(name):
        raise ValueError("meme name must contain 1-32 non-whitespace characters")
    cleaned_content = _clean_content(content)
    if not cleaned_content:
        raise ValueError("meme content must not be empty")

    with write_transaction(db_path) as conn:
        group_id = _find_group_id(conn, name)
        if group_id is None:
            group_id = _create_group(conn, name)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO meme_entry
                (meme_group_id, content, adder_uid, entry_type, status)
            VALUES (?, ?, ?, 'text', 'pending')
            """,
            (group_id, cleaned_content, int(adder_uid)),
        )
    return cursor.rowcount > 0


def add_meme_image(
    name: str,
    image_path: str,
    adder_uid: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    if not is_valid_meme_name(name):
        raise ValueError("meme name must contain 1-32 non-whitespace characters")
    cleaned_path = image_path.strip()
    if not cleaned_path:
        raise ValueError("meme image path must not be empty")

    with write_transaction(db_path) as conn:
        group_id = _find_group_id(conn, name)
        if group_id is None:
            group_id = _create_group(conn, name)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO meme_entry
                (meme_group_id, content, adder_uid, entry_type, status)
            VALUES (?, ?, ?, 'image', 'pending')
            """,
            (group_id, cleaned_path, int(adder_uid)),
        )
    return cursor.rowcount > 0


def add_meme_alias(
    first_name: str,
    second_name: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if not is_valid_meme_name(first_name) or not is_valid_meme_name(second_name):
        raise ValueError("meme names must contain 1-32 non-whitespace characters")

    first_key = _normalize_name(first_name)
    second_key = _normalize_name(second_name)
    if first_key == second_key:
        return {"changed": False, "merged": False, "display_name": first_name.strip()}

    with write_transaction(db_path) as conn:
        first_group_id = _find_group_id(conn, first_name)
        second_group_id = _find_group_id(conn, second_name)

        if first_group_id is None and second_group_id is None:
            target_group_id = _create_group(conn, first_name)
            conn.execute(
                """
                INSERT INTO meme_alias (alias_key, display_name, meme_group_id)
                VALUES (?, ?, ?)
                """,
                (second_key, second_name.strip(), target_group_id),
            )
            merged = False
        elif first_group_id is None:
            target_group_id = second_group_id
            conn.execute(
                """
                INSERT INTO meme_alias (alias_key, display_name, meme_group_id)
                VALUES (?, ?, ?)
                """,
                (first_key, first_name.strip(), target_group_id),
            )
            merged = False
        elif second_group_id is None:
            target_group_id = first_group_id
            conn.execute(
                """
                INSERT INTO meme_alias (alias_key, display_name, meme_group_id)
                VALUES (?, ?, ?)
                """,
                (second_key, second_name.strip(), target_group_id),
            )
            merged = False
        elif first_group_id == second_group_id:
            target_group_id = first_group_id
            merged = False
            changed = False
            row = conn.execute(
                "SELECT display_name FROM meme_group WHERE id = ?",
                (target_group_id,),
            ).fetchone()
            return {
                "changed": changed,
                "merged": merged,
                "display_name": str(row[0]) if row else first_name.strip(),
            }
        else:
            target_group_id = first_group_id
            conn.execute(
                """
                INSERT OR IGNORE INTO meme_entry
                    (meme_group_id, content, adder_uid, entry_type, status,
                     reviewed_at, reviewed_by, created_at)
                SELECT ?, content, adder_uid, entry_type, status,
                       reviewed_at, reviewed_by, created_at
                FROM meme_entry
                WHERE meme_group_id = ?
                """,
                (target_group_id, second_group_id),
            )
            conn.execute(
                "UPDATE meme_alias SET meme_group_id = ? WHERE meme_group_id = ?",
                (target_group_id, second_group_id),
            )
            conn.execute("DELETE FROM meme_group WHERE id = ?", (second_group_id,))
            merged = True

        row = conn.execute(
            "SELECT display_name FROM meme_group WHERE id = ?",
            (target_group_id,),
        ).fetchone()
        return {
            "changed": True,
            "merged": merged,
            "display_name": str(row[0]) if row else first_name.strip(),
        }


def get_random_meme(
    name: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    key = _normalize_name(name)
    if not key:
        return None
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT e.id, e.content, g.display_name, e.entry_type
            FROM meme_alias AS a
            JOIN meme_group AS g ON g.id = a.meme_group_id
            JOIN meme_entry AS e ON e.meme_group_id = a.meme_group_id
            WHERE a.alias_key = ? AND e.status = 'approved'
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "content": str(row[1]),
        "display_name": str(row[2]),
        "entry_type": str(row[3]),
    }


def list_meme_title_rank(
    limit: int = 10,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Rank canonical meme titles by their number of approved entries."""
    safe_limit = max(1, min(int(limit), 100))
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT g.display_name, COUNT(e.id) AS entry_count
            FROM meme_group AS g
            JOIN meme_entry AS e ON e.meme_group_id = g.id
            WHERE e.status = 'approved'
            GROUP BY g.id, g.display_name
            ORDER BY entry_count DESC, g.display_name COLLATE NOCASE, g.id
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        {"name": str(row[0]), "count": int(row[1])}
        for row in rows
    ]


def resolve_daily_meme_name(
    name: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> str | None:
    """Resolve any alias in a daily-draw group to its canonical draw name."""
    key = _normalize_name(name)
    if not key:
        return None
    candidate_by_key = {
        _normalize_name(candidate): candidate for candidate in DAILY_MEME_NAMES
    }
    placeholders = ",".join("?" for _ in candidate_by_key)
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT canonical.alias_key
            FROM meme_alias AS requested
            JOIN meme_alias AS canonical
              ON canonical.meme_group_id = requested.meme_group_id
            WHERE requested.alias_key = ?
              AND canonical.alias_key IN ({placeholders})
            """,
            (key, *candidate_by_key),
        ).fetchall()
    matched_keys = {str(row[0]) for row in rows}
    for candidate_key, candidate_name in candidate_by_key.items():
        if candidate_key in matched_keys:
            return candidate_name
    return None


def get_daily_meme(
    name: str,
    user_uid: int,
    *,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Draw once per Beijing calendar day and return the same approved entry."""
    draw_key = _normalize_name(name)
    if not draw_key:
        return None
    current = now or datetime.now(BEIJING_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TIMEZONE)
    draw_date = current.astimezone(BEIJING_TIMEZONE).date().isoformat()

    with write_transaction(db_path) as conn:
        existing = conn.execute(
            """
            SELECT d.meme_entry_id, d.content, d.entry_type, g.display_name
            FROM meme_daily_draw AS d
            LEFT JOIN meme_entry AS e ON e.id = d.meme_entry_id
            LEFT JOIN meme_group AS g ON g.id = e.meme_group_id
            WHERE d.user_uid = ? AND d.draw_date = ? AND d.draw_key = ?
              AND e.status = 'approved'
            """,
            (int(user_uid), draw_date, draw_key),
        ).fetchone()
        if existing is not None:
            return {
                "id": int(existing[0]),
                "content": str(existing[1]),
                "entry_type": str(existing[2]),
                "display_name": str(existing[3] or name),
                "draw_date": draw_date,
            }

        row = conn.execute(
            """
            SELECT e.id, e.content, e.entry_type, g.display_name
            FROM meme_alias AS a
            JOIN meme_group AS g ON g.id = a.meme_group_id
            JOIN meme_entry AS e ON e.meme_group_id = a.meme_group_id
            WHERE a.alias_key = ? AND e.status = 'approved'
              AND NOT EXISTS (
                  SELECT 1
                  FROM meme_daily_draw AS previous
                  WHERE previous.draw_date = ?
                    AND previous.draw_key = ?
                    AND (
                        previous.meme_entry_id = e.id
                        OR previous.content = e.content
                    )
              )
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (draw_key, draw_date, draw_key),
        ).fetchone()
        if row is None:
            raise MemePoolExhausted(draw_key)

        conn.execute(
            """
            INSERT INTO meme_daily_draw
                (user_uid, draw_date, draw_key, meme_entry_id, content, entry_type)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_uid, draw_date, draw_key) DO UPDATE SET
                meme_entry_id = excluded.meme_entry_id,
                content = excluded.content,
                entry_type = excluded.entry_type,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                int(user_uid),
                draw_date,
                draw_key,
                int(row[0]),
                str(row[1]),
                str(row[2]),
            ),
        )
        return {
            "id": int(row[0]),
            "content": str(row[1]),
            "entry_type": str(row[2]),
            "display_name": str(row[3]),
            "draw_date": draw_date,
        }


def list_pending_memes(
    limit: int = 500,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 2000))
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.id, g.display_name, e.content, e.adder_uid,
                   e.created_at, e.entry_type
            FROM meme_entry AS e
            JOIN meme_group AS g ON g.id = e.meme_group_id
            WHERE e.status = 'pending'
            ORDER BY e.created_at ASC, e.id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "content": str(row[2]),
            "adder_uid": int(row[3]),
            "created_at": str(row[4]),
            "entry_type": str(row[5]),
        }
        for row in rows
    ]


def list_memes(
    limit: int = 2000,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 5000))
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.id, g.display_name, e.content, e.adder_uid,
                   e.status, e.created_at, e.reviewed_at, e.reviewed_by,
                   e.entry_type
            FROM meme_entry AS e
            JOIN meme_group AS g ON g.id = e.meme_group_id
            ORDER BY
                CASE e.status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    ELSE 2
                END,
                e.created_at DESC,
                e.id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "content": str(row[2]),
            "adder_uid": int(row[3]),
            "status": str(row[4]),
            "created_at": str(row[5]),
            "reviewed_at": str(row[6]) if row[6] is not None else None,
            "reviewed_by": str(row[7]) if row[7] is not None else None,
            "entry_type": str(row[8]),
        }
        for row in rows
    ]


def get_meme_page(
    page: int = 1,
    status: str = "pending",
    page_size: int = 50,
    db_path: Path = DEFAULT_DB_PATH,
    query: str = "",
) -> dict[str, Any]:
    """Read totals and one bounded page from the same database snapshot."""
    if status not in {"pending", "all"}:
        raise ValueError("invalid meme filter")
    page_size = max(1, min(int(page_size), 200))
    query = query.strip()[:200]
    conditions = ["e.status = 'pending'"] if status == "pending" else []
    params: list[Any] = []
    if query:
        # Literal substring matching: '%' and '_' are not wildcards.
        conditions.append("""(
            instr(lower(g.display_name), lower(?)) > 0
            OR (e.entry_type = 'text' AND instr(lower(e.content), lower(?)) > 0)
            OR CAST(e.adder_uid AS TEXT) = ?
            OR EXISTS (
                SELECT 1 FROM meme_alias a WHERE a.meme_group_id = e.meme_group_id
                AND instr(lower(a.display_name), lower(?)) > 0
            )
        )""")
        params.extend([query] * 4)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with connect_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        counts = conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(status = 'pending'), 0) AS pending FROM meme_entry"
        ).fetchone()
        total, pending = int(counts["total"]), int(counts["pending"])
        filtered_total = int(conn.execute(
            f"SELECT COUNT(*) FROM meme_entry e JOIN meme_group g "
            f"ON g.id = e.meme_group_id {where}", params,
        ).fetchone()[0])
        pages = max(1, (filtered_total + page_size - 1) // page_size)
        page = min(max(1, int(page)), pages)
        rows = conn.execute(
            f"""
            SELECT e.id, g.display_name AS name, e.content, e.adder_uid,
                   e.status, e.created_at, e.reviewed_at, e.reviewed_by,
                   e.entry_type
            FROM meme_entry AS e
            JOIN meme_group AS g ON g.id = e.meme_group_id
            {where}
            ORDER BY CASE e.status WHEN 'pending' THEN 0
                         WHEN 'approved' THEN 1 ELSE 2 END,
                     e.created_at DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "status": status,
        "total": total,
        "pending": pending,
        "filtered_total": filtered_total,
        "query": query,
    }


def count_pending_memes(db_path: Path = DEFAULT_DB_PATH) -> int:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM meme_entry WHERE status = 'pending'"
        ).fetchone()
    return int(row[0]) if row else 0


def get_meme_by_id(
    record_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT e.id, g.display_name, e.content, e.adder_uid,
                   e.status, e.entry_type, e.created_at
            FROM meme_entry AS e
            JOIN meme_group AS g ON g.id = e.meme_group_id
            WHERE e.id = ?
            """,
            (int(record_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "name": str(row[1]),
        "content": str(row[2]),
        "adder_uid": int(row[3]),
        "status": str(row[4]),
        "entry_type": str(row[5]),
        "created_at": str(row[6]),
    }


def review_meme(
    record_id: int,
    action: str,
    reviewer: str = "web",
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    if action not in {"pending", "approved", "rejected"}:
        raise ValueError("invalid meme review action")
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE meme_entry
            SET status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ?
            WHERE id = ?
            """,
            (action, reviewer, int(record_id)),
        )
    return cursor.rowcount > 0


def find_meme(
    name: str,
    content: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    key = _normalize_name(name)
    cleaned_content = _clean_content(content)
    if not key or not cleaned_content:
        return None

    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT e.id, e.content, e.adder_uid, g.display_name
            FROM meme_alias AS a
            JOIN meme_group AS g ON g.id = a.meme_group_id
            JOIN meme_entry AS e ON e.meme_group_id = a.meme_group_id
            WHERE a.alias_key = ? AND e.content = ? AND e.entry_type = 'text'
            LIMIT 1
            """,
            (key, cleaned_content),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "content": str(row[1]),
        "adder_uid": int(row[2]),
        "display_name": str(row[3]),
    }


def remove_meme(
    record_id: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    with write_transaction(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM meme_entry WHERE id = ?",
            (int(record_id),),
        )
    return cursor.rowcount > 0


init_meme_db()
