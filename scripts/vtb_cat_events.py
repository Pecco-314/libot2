#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.vtb_cat_danmaku import (
    DEFAULT_BACKUP_ROOT,
    DEFAULT_CACHE_ROOT,
    DEFAULT_DB_PATH,
    PAGE_LIMIT,
    VtbCatClient,
    _atomic_write_json,
    _candidate_lives,
    _parse_timestamp,
    _resolve_room_id,
    _safe_int,
    _sanitize_text,
)
from src.db.sqlite import connect_sqlite

SUPPORTED_TYPES = ("msg", "gift", "guard", "sc")
TYPE_TO_CMD = {
    "msg": "DANMU_MSG",
    "gift": "SEND_GIFT",
    "guard": "GUARD_BUY",
    "sc": "SUPER_CHAT_MESSAGE",
}
MATCH_WINDOW_SECONDS = 3


@dataclass(frozen=True, slots=True)
class SourceEvent:
    source_id: int
    live_id: int
    room_id: int
    cmd: str
    uid: int
    uname: str
    content: str | None
    gift_name: str | None
    gift_num: int | None
    total_coin: int | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class ExistingEvent:
    event_id: int
    cmd: str
    uid: int
    content: str | None
    gift_name: str | None
    gift_num: int | None
    total_coin: int | None
    timestamp: int


class VtbCatEventsClient(VtbCatClient):
    def event_page(self, live_id: int, event_type: str, page: int) -> dict[str, Any]:
        if event_type not in SUPPORTED_TYPES:
            raise ValueError(f"不支持的事件类型: {event_type}")
        cache_path = self.cache_dir / "lives" / str(live_id) / f"{event_type}_{page}.json"
        payload = self._load_or_fetch(
            cache_path,
            f"/live/{live_id}/",
            {
                "page": page,
                "limit": PAGE_LIMIT,
                "order": "Time",
                "mid": 0,
                "type": event_type,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError(
                f"直播 {live_id} 类型 {event_type} 第 {page} 页响应缺少 records"
            )
        return payload


def _nullable_number(record: dict[str, Any], field: str, number: str) -> int | None:
    value = record.get(field)
    if not isinstance(value, dict) or value.get("Valid") is False:
        return None
    raw = value.get(number)
    if isinstance(raw, (int, float)):
        return int(round(float(raw)))
    return None


def _parse_record(
    record: dict[str, Any],
    *,
    event_type: str,
    live_id: int,
    room_id: int,
) -> SourceEvent | None:
    if record.get("ActionName") != event_type:
        return None
    if _safe_int(record.get("ActionType")) != SUPPORTED_TYPES.index(event_type) + 1:
        return None
    if _safe_int(record.get("Live")) != live_id:
        raise ValueError(f"直播 {live_id} 返回其他 live_id 的记录")
    if _safe_int(record.get("LiveRoom")) != room_id:
        raise ValueError(f"直播 {live_id} 返回其他房间的记录")

    source_id = _safe_int(record.get("ID"))
    uid = _safe_int(record.get("FromId"))
    timestamp = _parse_timestamp(record.get("CreatedAt"))
    if source_id is None or uid is None or timestamp is None:
        return None

    uname = _sanitize_text(record.get("FromName"))
    content: str | None = None
    gift_name: str | None = None
    gift_num: int | None = None
    total_coin: int | None = None
    cmd = TYPE_TO_CMD[event_type]

    if event_type == "msg":
        content = _sanitize_text(record.get("Extra"))
        if not content:
            return None
    elif event_type == "gift":
        gift_name = _sanitize_text(record.get("GiftName"))
        gift_num = _nullable_number(record, "GiftAmount", "Int16") or 1
        price = record.get("GiftPrice")
        raw_price = price.get("Float64") if isinstance(price, dict) else None
        if not gift_name or not isinstance(raw_price, (int, float)):
            return None
        total_coin = int(round(float(raw_price) * 1000))
    elif event_type == "guard":
        gift_name = _sanitize_text(record.get("GiftName")) or "舰长"
        gift_num = _nullable_number(record, "GiftAmount", "Int16") or 1
        price = record.get("GiftPrice")
        raw_price = price.get("Float64") if isinstance(price, dict) else None
        if not isinstance(raw_price, (int, float)):
            return None
        total_coin = int(round(float(raw_price) * 1000))
    elif event_type == "sc":
        content = _sanitize_text(record.get("Extra"))
        price = record.get("GiftPrice")
        raw_price = price.get("Float64") if isinstance(price, dict) else None
        if not content or not isinstance(raw_price, (int, float)):
            return None
        gift_name = "醒目留言"
        gift_num = 1
        total_coin = int(round(float(raw_price)))

    return SourceEvent(
        source_id=source_id,
        live_id=live_id,
        room_id=room_id,
        cmd=cmd,
        uid=uid,
        uname=uname,
        content=content,
        gift_name=gift_name,
        gift_num=gift_num,
        total_coin=total_coin,
        timestamp=timestamp,
    )


def fetch_live_events(
    client: VtbCatEventsClient,
    live: dict[str, Any],
    room_id: int,
    event_types: tuple[str, ...],
) -> tuple[list[SourceEvent], Counter[str]]:
    live_id = int(live["ID"])
    rows: list[SourceEvent] = []
    counts: Counter[str] = Counter()
    for event_type in event_types:
        page = 1
        while True:
            payload = client.event_page(live_id, event_type, page)
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                row = _parse_record(
                    record,
                    event_type=event_type,
                    live_id=live_id,
                    room_id=room_id,
                )
                if row is not None:
                    rows.append(row)
                    counts[row.cmd] += 1
            total_pages = _safe_int(payload.get("totalPages")) or 1
            if page >= total_pages:
                break
            page += 1
    rows.sort(key=lambda row: (row.timestamp, row.source_id))
    return rows, counts


def _fill_unames(
    rows: list[SourceEvent],
    source_unames: dict[int, str],
    db_path: Path,
) -> tuple[list[SourceEvent], int]:
    for row in rows:
        if row.uname:
            source_unames[row.uid] = row.uname

    missing_uids = {
        row.uid for row in rows if not row.uname and row.uid not in source_unames
    }
    if missing_uids:
        with connect_sqlite(db_path) as conn:
            for uid in missing_uids:
                found = conn.execute(
                    """
                    SELECT uname FROM event
                    WHERE uid = ? AND uname IS NOT NULL AND uname != ''
                    ORDER BY timestamp DESC, id DESC LIMIT 1
                    """,
                    (uid,),
                ).fetchone()
                if found is not None:
                    source_unames[uid] = str(found[0])

    filled = 0
    result: list[SourceEvent] = []
    for row in rows:
        if not row.uname and source_unames.get(row.uid):
            row = replace(row, uname=source_unames[row.uid])
            filled += 1
        result.append(row)
    return result, filled


def _dedupe_paid_source_signals(
    rows: list[SourceEvent],
) -> tuple[list[SourceEvent], Counter[str]]:
    result: list[SourceEvent] = []
    last_index: dict[tuple[Any, ...], int] = {}
    removed: Counter[str] = Counter()

    for row in sorted(rows, key=lambda item: (item.timestamp, item.source_id)):
        if row.cmd == "SUPER_CHAT_MESSAGE":
            key: tuple[Any, ...] | None = (
                row.cmd,
                row.uid,
                row.content,
                row.total_coin,
            )
        elif row.cmd == "GUARD_BUY":
            key = (row.cmd, row.uid, row.gift_name, row.gift_num)
        else:
            key = None

        previous_index = last_index.get(key) if key is not None else None
        if previous_index is not None:
            previous = result[previous_index]
            if row.timestamp - previous.timestamp <= MATCH_WINDOW_SECONDS:
                result[previous_index] = row
                removed[row.cmd] += 1
                continue

        result.append(row)
        if key is not None:
            last_index[key] = len(result) - 1

    return result, removed


def _load_existing(
    db_path: Path,
    room_id: int,
    min_timestamp: int,
    max_timestamp: int,
    commands: Iterable[str],
) -> list[ExistingEvent]:
    command_list = sorted(set(commands))
    placeholders = ",".join("?" for _ in command_list)
    params: list[Any] = [
        room_id,
        min_timestamp - MATCH_WINDOW_SECONDS,
        max_timestamp + MATCH_WINDOW_SECONDS,
        *command_list,
    ]
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, cmd, uid, content, gift_name, gift_num, total_coin, timestamp
            FROM event
            WHERE room_id = ? AND timestamp BETWEEN ? AND ?
              AND cmd IN ({placeholders}) AND uid IS NOT NULL
            ORDER BY timestamp, id
            """,
            params,
        ).fetchall()
    return [
        ExistingEvent(
            event_id=int(row[0]),
            cmd=str(row[1]),
            uid=int(row[2]),
            content=_sanitize_text(row[3]) if row[3] is not None else None,
            gift_name=_sanitize_text(row[4]) if row[4] is not None else None,
            gift_num=int(row[5]) if row[5] is not None else None,
            total_coin=int(row[6]) if row[6] is not None else None,
            timestamp=int(row[7]),
        )
        for row in rows
    ]


def _event_key(row: SourceEvent | ExistingEvent) -> tuple[Any, ...]:
    if row.cmd == "DANMU_MSG":
        return row.cmd, row.uid, row.content
    if row.cmd == "SUPER_CHAT_MESSAGE":
        return row.cmd, row.uid, row.content, row.total_coin
    if row.cmd == "GUARD_BUY":
        return row.cmd, row.uid, row.gift_name
    raise ValueError(f"不支持普通事件键: {row.cmd}")


def _match_regular_events(
    source_rows: list[SourceEvent],
    existing_rows: list[ExistingEvent],
    consumed_existing_ids: set[int],
) -> tuple[list[SourceEvent], Counter[str]]:
    source_groups: dict[tuple[Any, ...], list[SourceEvent]] = defaultdict(list)
    existing_groups: dict[tuple[Any, ...], list[ExistingEvent]] = defaultdict(list)
    for row in source_rows:
        if row.cmd != "SEND_GIFT":
            source_groups[_event_key(row)].append(row)
    for row in existing_rows:
        if row.cmd != "SEND_GIFT" and row.event_id not in consumed_existing_ids:
            existing_groups[_event_key(row)].append(row)

    remaining: list[SourceEvent] = []
    matched: Counter[str] = Counter()
    for key, rows in source_groups.items():
        candidates = existing_groups.get(key, [])
        by_timestamp: dict[int, list[ExistingEvent]] = defaultdict(list)
        for candidate in candidates:
            by_timestamp[candidate.timestamp].append(candidate)

        after_exact: list[SourceEvent] = []
        for row in rows:
            exact = next(
                (
                    candidate
                    for candidate in by_timestamp.get(row.timestamp, [])
                    if candidate.event_id not in consumed_existing_ids
                ),
                None,
            )
            if exact is None:
                after_exact.append(row)
            else:
                consumed_existing_ids.add(exact.event_id)
                matched[row.cmd] += 1

        for row in after_exact:
            nearby = [
                candidate
                for candidate in candidates
                if candidate.event_id not in consumed_existing_ids
                and abs(candidate.timestamp - row.timestamp) <= MATCH_WINDOW_SECONDS
            ]
            if not nearby:
                remaining.append(row)
                continue
            nearby.sort(
                key=lambda candidate: (
                    abs(candidate.timestamp - row.timestamp),
                    candidate.timestamp,
                    candidate.event_id,
                )
            )
            consumed_existing_ids.add(nearby[0].event_id)
            matched[row.cmd] += 1
    return remaining, matched


def _match_gifts(
    source_rows: list[SourceEvent],
    existing_rows: list[ExistingEvent],
    gift_capacity: dict[int, int],
) -> tuple[list[SourceEvent], int, int]:
    source_groups: dict[tuple[int, str | None], list[SourceEvent]] = defaultdict(list)
    existing_groups: dict[tuple[int, str | None], list[ExistingEvent]] = defaultdict(list)
    for row in source_rows:
        if row.cmd == "SEND_GIFT":
            source_groups[(row.uid, row.gift_name)].append(row)
    for row in existing_rows:
        if row.cmd == "SEND_GIFT":
            existing_groups[(row.uid, row.gift_name)].append(row)
            gift_capacity.setdefault(row.event_id, max(1, row.gift_num or 1))

    remaining_rows: list[SourceEvent] = []
    matched_events = 0
    matched_units = 0
    for key, rows in source_groups.items():
        rows.sort(key=lambda row: (row.timestamp, row.source_id))
        candidates = sorted(
            existing_groups.get(key, []),
            key=lambda row: (row.timestamp, row.event_id),
        )
        active: deque[ExistingEvent] = deque()
        candidate_index = 0
        for row in rows:
            while (
                candidate_index < len(candidates)
                and candidates[candidate_index].timestamp
                <= row.timestamp + MATCH_WINDOW_SECONDS
            ):
                active.append(candidates[candidate_index])
                candidate_index += 1
            while (
                active
                and active[0].timestamp < row.timestamp - MATCH_WINDOW_SECONDS
            ):
                active.popleft()

            original_units = max(1, row.gift_num or 1)
            remaining_units = original_units
            while remaining_units and active:
                candidate = active[0]
                available = gift_capacity.get(candidate.event_id, 0)
                if available <= 0:
                    active.popleft()
                    continue
                consumed = min(remaining_units, available)
                gift_capacity[candidate.event_id] = available - consumed
                remaining_units -= consumed
                matched_units += consumed
                if gift_capacity[candidate.event_id] == 0:
                    active.popleft()

            if remaining_units == 0:
                matched_events += 1
                continue
            if remaining_units != original_units:
                total_coin = row.total_coin
                if total_coin is not None:
                    total_coin = int(round(total_coin * remaining_units / original_units))
                row = replace(row, gift_num=remaining_units, total_coin=total_coin)
            remaining_rows.append(row)
    return remaining_rows, matched_events, matched_units


def plan_live_import(
    db_path: Path,
    room_id: int,
    source_rows: list[SourceEvent],
    consumed_existing_ids: set[int],
    gift_capacity: dict[int, int],
) -> tuple[list[SourceEvent], Counter[str], int]:
    if not source_rows:
        return [], Counter(), 0
    existing_rows = _load_existing(
        db_path,
        room_id,
        min(row.timestamp for row in source_rows),
        max(row.timestamp for row in source_rows),
        (row.cmd for row in source_rows),
    )
    regular_remaining, matched = _match_regular_events(
        source_rows,
        existing_rows,
        consumed_existing_ids,
    )
    gift_remaining, matched_gift_events, _matched_gift_units = _match_gifts(
        source_rows,
        existing_rows,
        gift_capacity,
    )
    matched["SEND_GIFT"] += matched_gift_events
    result = regular_remaining + gift_remaining
    result.sort(key=lambda row: (row.timestamp, row.source_id))
    return result, matched, _matched_gift_units


def build_import_plan(
    client: VtbCatEventsClient,
    db_path: Path,
    room_id: int,
    lives: list[dict[str, Any]],
    event_types: tuple[str, ...],
    cutoff: int,
) -> tuple[list[SourceEvent], dict[str, Counter[str] | int]]:
    insert_rows: list[SourceEvent] = []
    gift_source_rows: list[SourceEvent] = []
    source_counts: Counter[str] = Counter()
    matched_counts: Counter[str] = Counter()
    insert_counts: Counter[str] = Counter()
    seen_source_ids: set[int] = set()
    consumed_existing_ids: set[int] = set()
    source_unames: dict[int, str] = {}
    duplicate_source_ids = 0
    outside_range = 0
    filled_unames = 0
    matched_gift_units = 0
    duplicate_semantic_signals: Counter[str] = Counter()

    for index, live in enumerate(lives, start=1):
        live_id = int(live["ID"])
        title = _sanitize_text(live.get("Title"))
        rows, live_source_counts = fetch_live_events(
            client,
            live,
            room_id,
            event_types,
        )
        unique_rows: list[SourceEvent] = []
        for row in rows:
            if row.source_id in seen_source_ids:
                duplicate_source_ids += 1
                continue
            seen_source_ids.add(row.source_id)
            if row.timestamp >= cutoff:
                outside_range += 1
                continue
            unique_rows.append(row)
        unique_rows, live_semantic_duplicates = _dedupe_paid_source_signals(
            unique_rows
        )
        duplicate_semantic_signals.update(live_semantic_duplicates)
        unique_rows, live_filled = _fill_unames(unique_rows, source_unames, db_path)
        filled_unames += live_filled
        gift_source_rows.extend(row for row in unique_rows if row.cmd == "SEND_GIFT")
        regular_rows = [row for row in unique_rows if row.cmd != "SEND_GIFT"]
        planned, live_matched, _ = plan_live_import(
            db_path,
            room_id,
            regular_rows,
            consumed_existing_ids,
            {},
        )
        source_counts.update(row.cmd for row in unique_rows)
        matched_counts.update(live_matched)
        insert_counts.update(row.cmd for row in planned)
        insert_rows.extend(planned)
        source_text = ",".join(
            f"{cmd}={live_source_counts.get(cmd, 0)}"
            for cmd in TYPE_TO_CMD.values()
            if cmd in live_source_counts
        )
        insert_text = ",".join(
            f"{cmd}={sum(1 for row in planned if row.cmd == cmd)}"
            for cmd in TYPE_TO_CMD.values()
            if any(row.cmd == cmd for row in planned)
        )
        print(
            f"[{index}/{len(lives)}] live={live_id} "
            f"源[{source_text or '0'}] 新增(非礼物)[{insert_text or '0'}] {title}",
            flush=True,
        )

    if gift_source_rows:
        existing_gifts = _load_existing(
            db_path,
            room_id,
            min(row.timestamp for row in gift_source_rows),
            max(row.timestamp for row in gift_source_rows),
            ("SEND_GIFT",),
        )
        gift_remaining, matched_gift_events, matched_gift_units = _match_gifts(
            gift_source_rows,
            existing_gifts,
            {},
        )
        matched_counts["SEND_GIFT"] = matched_gift_events
        insert_counts["SEND_GIFT"] = len(gift_remaining)
        insert_rows.extend(gift_remaining)
        print(
            f"礼物全局匹配: 已匹配事件={matched_gift_events}, "
            f"已匹配数量单位={matched_gift_units}, 新增事件={len(gift_remaining)}"
        )

    insert_rows.sort(key=lambda row: (row.timestamp, row.source_id))
    stats: dict[str, Counter[str] | int] = {
        "source": source_counts,
        "matched": matched_counts,
        "insert": insert_counts,
        "duplicate_source_ids": duplicate_source_ids,
        "outside_range": outside_range,
        "filled_unames": filled_unames,
        "matched_gift_units": matched_gift_units,
        "duplicate_semantic_signals": duplicate_semantic_signals,
    }
    return insert_rows, stats


def _write_recovery_plan(
    backup_root: Path,
    uid: int,
    room_id: int,
    cutoff: int,
    rows: list[SourceEvent],
) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    path = backup_root / f"vtb_cat_events_{uid}_{room_id}_{stamp}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "kind": "libot2-vtb-cat-events-recovery-plan",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "uid": uid,
                    "room_id": room_id,
                    "cutoff": cutoff,
                    "row_count": len(rows),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for row in rows:
            output.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    return path


def apply_import(
    db_path: Path,
    uid: int,
    room_id: int,
    cutoff: int,
    rows: list[SourceEvent],
    backup_root: Path,
) -> tuple[int, int, int, Path]:
    recovery_plan = _write_recovery_plan(backup_root, uid, room_id, cutoff, rows)
    conn = connect_sqlite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before_max = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0])
        conn.executemany(
            """
            INSERT INTO event (
                room_id, cmd, uid, uname, content, gift_name, gift_num,
                total_coin, title, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            [
                (
                    row.room_id,
                    row.cmd,
                    row.uid,
                    row.uname,
                    row.content,
                    row.gift_name,
                    row.gift_num,
                    row.total_coin,
                    row.timestamp,
                )
                for row in rows
            ],
        )
        after_max = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0])
        inserted = int(
            conn.execute(
                "SELECT COUNT(*) FROM event WHERE id > ? AND id <= ?",
                (before_max, after_max),
            ).fetchone()[0]
        )
        if inserted != len(rows):
            raise RuntimeError(f"插入数量异常: 计划 {len(rows)}，事务新增 {inserted}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    manifest = recovery_plan.with_suffix("").with_suffix(".manifest.json")
    _atomic_write_json(
        manifest,
        {
            "kind": "libot2-vtb-cat-events-import-manifest",
            "database": str(db_path.resolve()),
            "recovery_plan": str(recovery_plan.resolve()),
            "inserted": inserted,
            "first_event_id": before_max + 1 if inserted else None,
            "last_event_id": after_max if inserted else None,
        },
    )
    return inserted, before_max + 1, after_max, manifest


def _format_counts(counts: Counter[str]) -> str:
    return ", ".join(
        f"{cmd}={counts.get(cmd, 0)}"
        for cmd in TYPE_TO_CMD.values()
    )


def _parse_event_types(value: str) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    invalid = [item for item in result if item not in SUPPORTED_TYPES]
    if invalid or not result:
        raise argparse.ArgumentTypeError(
            f"事件类型必须来自 {','.join(SUPPORTED_TYPES)}: {','.join(invalid)}"
        )
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 api.vtb.cat 补充弹幕、礼物、舰长和 SC；默认 dry-run"
    )
    parser.add_argument("uid", type=int, help="Bilibili UID")
    parser.add_argument("--room-id", type=int, default=None, help="直播间号")
    parser.add_argument("--before", required=True, help="截止时间，ISO 8601，建议带时区")
    parser.add_argument(
        "--types",
        type=_parse_event_types,
        default=SUPPORTED_TYPES,
        help="逗号分隔，默认 msg,gift,guard,sc",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true", help="实际写入数据库")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cutoff = _parse_timestamp(args.before)
    if cutoff is None:
        raise ValueError(f"无法解析 --before: {args.before}")
    db_path = args.database.resolve()
    cache_dir = (
        args.cache_dir.resolve()
        if args.cache_dir is not None
        else (DEFAULT_CACHE_ROOT / str(args.uid)).resolve()
    )
    with VtbCatEventsClient(
        cache_dir=cache_dir,
        timeout=args.timeout,
        delay=args.delay,
        offline=args.offline,
        refresh=args.refresh,
    ) as client:
        space = client.liver_space(args.uid)
        room_id = _resolve_room_id(space, args.room_id)
        lives = _candidate_lives(
            space,
            room_id,
            cutoff=cutoff,
            include_overlap=False,
        )
        print(f"UID: {args.uid} ({_sanitize_text(space.get('UName'))})")
        print(f"room_id: {room_id}")
        print(f"截止: {datetime.fromtimestamp(cutoff).astimezone().isoformat()}")
        print(f"类型: {','.join(args.types)}；候选直播: {len(lives)}")
        print(f"缓存目录: {cache_dir}")
        insert_rows, stats = build_import_plan(
            client,
            db_path,
            room_id,
            lives,
            args.types,
            cutoff,
        )

    source_counts = stats["source"]
    matched_counts = stats["matched"]
    insert_counts = stats["insert"]
    assert isinstance(source_counts, Counter)
    assert isinstance(matched_counts, Counter)
    assert isinstance(insert_counts, Counter)
    semantic_duplicates = stats["duplicate_semantic_signals"]
    assert isinstance(semantic_duplicates, Counter)
    print(f"源记录: {_format_counts(source_counts)}")
    print(f"匹配已有: {_format_counts(matched_counts)}")
    print(f"计划新增: {_format_counts(insert_counts)}")
    print(f"源内付费信号去重: {_format_counts(semantic_duplicates)}")
    print(f"礼物已匹配数量单位: {stats['matched_gift_units']}")
    print(f"源 ID 重复跳过: {stats['duplicate_source_ids']}")
    print(f"截止范围外跳过: {stats['outside_range']}")
    print(f"用户名回填: {stats['filled_unames']}")
    print(f"计划新增合计: {len(insert_rows)}")
    if insert_rows:
        print(
            "新增时间范围: "
            f"{datetime.fromtimestamp(insert_rows[0].timestamp).astimezone().isoformat()} 至 "
            f"{datetime.fromtimestamp(insert_rows[-1].timestamp).astimezone().isoformat()}"
        )

    if not args.apply:
        print("dry-run 完成；未写数据库。")
        return 0
    if not insert_rows:
        print("没有新事件需要写入。")
        return 0
    inserted, first_id, last_id, manifest = apply_import(
        db_path,
        args.uid,
        room_id,
        cutoff,
        insert_rows,
        args.backup_dir.resolve(),
    )
    print(f"已新增 {inserted} 条，event.id={first_id}..{last_id}")
    print(f"恢复清单: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
