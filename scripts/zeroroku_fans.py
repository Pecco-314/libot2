#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.sqlite import connect_sqlite, write_transaction

BASE_URL = "https://zeroroku.com/bilibili/author/{uid}"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "libot.db"


@dataclass(frozen=True, slots=True)
class FansSnapshot:
    created_at: str
    fans_num: int


class _NuxtDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attributes = dict(attrs)
        if attributes.get("id") == "__NUXT_DATA__":
            self._capturing = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def payload(self) -> str:
        return "".join(self._parts).strip()


def _deref(flat: list[Any], value: Any) -> Any:
    if isinstance(value, int) and 0 <= value < len(flat):
        return flat[value]
    return value


def _utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_author_page(html: str, uid: int) -> tuple[str, int | None, list[FansSnapshot]]:
    parser = _NuxtDataParser()
    parser.feed(html)
    payload = parser.payload()
    if not payload:
        raise ValueError("页面中没有找到 __NUXT_DATA__")

    flat = json.loads(payload)
    if not isinstance(flat, list):
        raise ValueError("__NUXT_DATA__ 不是数组")

    uid_text = str(uid)
    uname = ""
    room_id: int | None = None
    snapshots: dict[str, FansSnapshot] = {}

    for item in flat:
        if not isinstance(item, dict):
            continue

        if {"mid", "name", "liveRoomId"}.issubset(item):
            item_uid = str(_deref(flat, item.get("mid")))
            if item_uid == uid_text:
                uname = str(_deref(flat, item.get("name")) or "")
                raw_room_id = _deref(flat, item.get("liveRoomId"))
                if str(raw_room_id).isdigit():
                    room_id = int(raw_room_id)

        if not {"mid", "fans", "createdAt"}.issubset(item):
            continue
        item_uid = str(_deref(flat, item.get("mid")))
        if item_uid != uid_text:
            continue

        raw_fans = _deref(flat, item.get("fans"))
        raw_created_at = _deref(flat, item.get("createdAt"))
        if not isinstance(raw_fans, int) or not isinstance(raw_created_at, str):
            continue
        created_at = _utc_timestamp(raw_created_at)
        snapshots[created_at] = FansSnapshot(created_at=created_at, fans_num=raw_fans)

    if not uname:
        raise ValueError(f"页面中没有找到 UID {uid} 的作者信息")
    rows = sorted(snapshots.values(), key=lambda row: row.created_at)
    if not rows:
        raise ValueError(f"页面中没有找到 UID {uid} 的粉丝历史")
    return uname, room_id, rows


def fetch_author_page(uid: int, timeout: float) -> str:
    transport = httpx.HTTPTransport(retries=3, local_address="0.0.0.0")
    with httpx.Client(
        headers={"User-Agent": "libot2-zeroroku-fans/1.0"},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
        trust_env=False,
    ) as client:
        response = client.get(BASE_URL.format(uid=uid))
        response.raise_for_status()
        return response.text


def _resolve_room_id(db_path: Path, uid: int, page_room_id: int | None) -> int:
    if page_room_id is not None:
        return page_room_id
    with connect_sqlite(db_path) as conn:
        row = conn.execute("SELECT room_id FROM liver WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise ValueError(f"无法从页面或 liver 表确定 UID {uid} 的直播间")
    return int(row[0])


def import_snapshots(
    db_path: Path,
    room_id: int,
    uid: int,
    uname: str,
    snapshots: list[FansSnapshot],
    *,
    include_overlap: bool,
    dry_run: bool,
) -> tuple[int, int, str | None]:
    with connect_sqlite(db_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stats'"
        ).fetchone()
        if table is None:
            raise ValueError(f"数据库中不存在 stats 表: {db_path}")
        row = conn.execute(
            """
            SELECT MIN(created_at)
            FROM stats
            WHERE room_id = ? AND fans_num != -1
              AND (guard_num != -1 OR fan_club_num != -1)
            """,
            (room_id,),
        ).fetchone()
        existing_start = str(row[0]) if row and row[0] is not None else None
        existing_times = {
            str(item[0])
            for item in conn.execute(
                "SELECT created_at FROM stats WHERE room_id = ?",
                (room_id,),
            ).fetchall()
        }

    eligible = [
        snapshot
        for snapshot in snapshots
        if (include_overlap or existing_start is None or snapshot.created_at < existing_start)
        and snapshot.created_at not in existing_times
    ]
    skipped = len(snapshots) - len(eligible)
    if dry_run or not eligible:
        return 0, skipped, existing_start

    inserted = 0
    with write_transaction(db_path) as conn:
        for snapshot in eligible:
            cursor = conn.execute(
                """
                INSERT INTO stats (
                    room_id, uid, uname, fans_num, guard_num, fan_club_num, created_at
                )
                SELECT ?, ?, ?, ?, -1, -1, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM stats WHERE room_id = ? AND created_at = ?
                )
                """,
                (
                    room_id,
                    uid,
                    uname,
                    snapshot.fans_num,
                    snapshot.created_at,
                    room_id,
                    snapshot.created_at,
                ),
            )
            inserted += max(0, cursor.rowcount)
    return inserted, skipped, existing_start


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 zeroroku 作者页补充历史粉丝数；未知舰长/粉丝团写为 -1"
    )
    parser.add_argument("uid", type=int, help="Bilibili UID")
    parser.add_argument("--room-id", type=int, default=None, help="覆盖页面中的直播间号")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH, help="SQLite 数据库路径")
    parser.add_argument("--html", type=Path, default=None, help="使用已保存的 HTML，不访问网络")
    parser.add_argument("--timeout", type=float, default=30.0, help="网络超时秒数")
    parser.add_argument(
        "--include-overlap",
        action="store_true",
        help="也导入本地起始日期之后的数据；默认仅补更早历史",
    )
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不写数据库")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    html = (
        args.html.read_text(encoding="utf-8")
        if args.html is not None
        else fetch_author_page(args.uid, args.timeout)
    )
    uname, page_room_id, snapshots = parse_author_page(html, args.uid)
    room_id = args.room_id or _resolve_room_id(args.database, args.uid, page_room_id)
    inserted, skipped, existing_start = import_snapshots(
        args.database,
        room_id,
        args.uid,
        uname,
        snapshots,
        include_overlap=args.include_overlap,
        dry_run=args.dry_run,
    )

    print(f"UID: {args.uid} ({uname})")
    print(f"room_id: {room_id}")
    print(f"源数据: {len(snapshots)} 条，{snapshots[0].created_at} 至 {snapshots[-1].created_at} UTC")
    print(f"本地完整指标起点: {existing_start or '无'} UTC")
    if args.dry_run:
        print(f"dry-run: 可新增 {len(snapshots) - skipped} 条，跳过 {skipped} 条")
    else:
        print(f"已新增 {inserted} 条，跳过 {skipped} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
