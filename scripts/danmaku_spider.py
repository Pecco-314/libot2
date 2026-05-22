#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from importlib.machinery import SourceFileLoader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.sqlite import connect_sqlite

BASE_URL = "https://ukamnads.icu/api/v3/lives"
DEFAULT_LIMIT = 10000
TRACKED_TYPES = {0, 1, 2, 3, 5, 11, 12}
DEDUPE_WINDOW_SECONDS = 3
def _load_emoji_dict() -> dict[str, str]:
    emoji_path = PROJECT_ROOT / "scripts" / "emoji.py"
    if not emoji_path.exists():
        return {}
    module = SourceFileLoader("libot2_emoji", str(emoji_path)).load_module()
    emoji_dict = getattr(module, "emoji_dict", {})
    return emoji_dict if isinstance(emoji_dict, dict) else {}


EMOJI_DICT = _load_emoji_dict()



def _request_json(url: str, timeout: int = 30) -> Any:
    req = Request(url, headers={"User-Agent": "libot2-danmaku-spider/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read().decode(charset)
    return json.loads(data)


def _extract_frame(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool, int | None]:
    if not isinstance(payload, dict):
        return [], [], [], False, None

    data = payload.get("data")
    if not isinstance(data, dict):
        return [], [], [], False, None

    frame = data.get("frame") or {}
    if not isinstance(frame, dict):
        frame = {}

    records = frame.get("records") if isinstance(frame.get("records"), list) else []
    actors = frame.get("actors") if isinstance(frame.get("actors"), list) else []
    room_emojis = frame.get("roomEmojis") if isinstance(frame.get("roomEmojis"), list) else []

    has_more = bool(data.get("hasMore"))
    total = data.get("total") if isinstance(data.get("total"), int) else None

    return records, actors, room_emojis, has_more, total


def fetch_pages_to_dir(live_hash: str, limit: int, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    meta = fetch_live_meta(live_hash)
    if meta:
        meta_path = output_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    offset = 0
    pages: list[Path] = []
    while True:
        query = urlencode({"offset": offset, "limit": limit})
        url = f"{BASE_URL}/{live_hash}/danmakus?{query}"
        payload = _request_json(url)
        records, _, _, has_more, _ = _extract_frame(payload)
        if not records:
            break
        page_path = output_dir / f"page_{offset}.json"
        page_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        pages.append(page_path)
        if not has_more:
            break
        offset += limit

    return pages


def _list_page_files(pages_dir: Path) -> list[Path]:
    files = [p for p in pages_dir.glob("page_*.json") if p.is_file()]
    def _offset(path: Path) -> int:
        try:
            return int(path.stem.split("_")[-1])
        except Exception:
            return 0
    return sorted(files, key=_offset)


def fetch_live_meta(live_hash: str) -> dict[str, Any]:
    url = f"{BASE_URL}/{live_hash}"
    payload = _request_json(url)
    if not isinstance(payload, dict) or "data" not in payload:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    return data


@dataclass(slots=True)
class EventRow:
    room_id: int
    cmd: str
    uid: int | None
    uname: str | None
    content: str | None
    gift_name: str | None
    gift_num: int | None
    total_coin: int | None
    title: str | None
    timestamp: int


def _safe_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _build_event_rows(payload: dict[str, Any], room_id: int) -> tuple[list[EventRow], dict[str, dict[int, int]]]:
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    actors = payload.get("actors") if isinstance(payload.get("actors"), list) else []
    room_emojis = payload.get("roomEmojis") if isinstance(payload.get("roomEmojis"), list) else []

    rows: list[EventRow] = []
    stats: dict[str, dict[int, int]] = {
        "total": {},
        "used": {},
        "ignored": {},
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        if isinstance(record_type, int):
            stats["total"][record_type] = stats["total"].get(record_type, 0) + 1
        if record_type not in TRACKED_TYPES:
            if isinstance(record_type, int):
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
            continue
        payload_kind = record.get("payloadKind")
        ts = record.get("ts")
        if not isinstance(ts, int):
            if isinstance(record_type, int):
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
            continue
        timestamp = ts // 1000

        actor_id = record.get("actorId")
        uid = None
        uname = None
        if isinstance(actor_id, int) and 0 <= actor_id < len(actors):
            actor = actors[actor_id]
            if isinstance(actor, dict):
                uid = _safe_int(actor.get("uid"))
                uname = actor.get("name") if isinstance(actor.get("name"), str) else None

        payload_data = record.get("payload") if isinstance(record.get("payload"), dict) else {}

        if record_type == 0:
            if payload_kind == 1:
                content = payload_data.get("rawText") if isinstance(payload_data.get("rawText"), str) else None
            elif payload_kind == 2:
                emoji_id = record.get("payload", {}).get("roomEmojiId")
                content = None
                if isinstance(emoji_id, int) and 0 <= emoji_id < len(room_emojis):
                    emoji = room_emojis[emoji_id]
                    if isinstance(emoji, dict):
                        resource = emoji.get("resource")
                        if isinstance(resource, str):
                            content = EMOJI_DICT.get(resource)
                            if content is None:
                                print(f"[warn] unknown emoji resource: {resource}", file=sys.stderr)
                                content = f"[EMOJI:{resource}]"
                if content is None:
                    print(f"[warn] unknown emoji id: {emoji_id}", file=sys.stderr)
                    if isinstance(record_type, int):
                        stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                    continue
            else:
                if isinstance(record_type, int):
                    stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            if not content:
                if isinstance(record_type, int):
                    stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="DANMU_MSG",
                    uid=uid,
                    uname=uname,
                    content=content,
                    gift_name=None,
                    gift_num=None,
                    total_coin=None,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 1:
            if payload_kind != 3:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            gift_name = payload_data.get("name") if isinstance(payload_data.get("name"), str) else None
            gift_num = _safe_int(payload_data.get("count")) or 1
            price = payload_data.get("price")
            total_coin = int(round(float(price) * 1000)) if isinstance(price, (int, float)) else None
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="SEND_GIFT",
                    uid=uid,
                    uname=uname,
                    content=None,
                    gift_name=gift_name,
                    gift_num=gift_num,
                    total_coin=total_coin,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 2:
            if payload_kind != 4:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            gift_name = payload_data.get("name") if isinstance(payload_data.get("name"), str) else None
            gift_num = _safe_int(payload_data.get("count")) or 1
            price = payload_data.get("price")
            total_coin = int(round(float(price) * 1000)) if isinstance(price, (int, float)) else None
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="GUARD_BUY",
                    uid=uid,
                    uname=uname,
                    content=None,
                    gift_name=gift_name,
                    gift_num=gift_num,
                    total_coin=total_coin,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 3:
            if payload_kind != 5:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            content = payload_data.get("text") if isinstance(payload_data.get("text"), str) else None
            if not content:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            price = payload_data.get("price")
            total_coin = int(round(float(price))) if isinstance(price, (int, float)) else None
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="SUPER_CHAT_MESSAGE",
                    uid=uid,
                    uname=uname,
                    content=content,
                    gift_name="醒目留言",
                    gift_num=1,
                    total_coin=total_coin,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 5:
            if payload_kind != 6:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            title = payload_data.get("currentTitle") if isinstance(payload_data.get("currentTitle"), str) else None
            if not title:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="ROOM_CHANGE",
                    uid=None,
                    uname=None,
                    content=None,
                    gift_name=None,
                    gift_num=None,
                    total_coin=None,
                    title=title,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 11:
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="LIVE",
                    uid=None,
                    uname=None,
                    content=None,
                    gift_name=None,
                    gift_num=None,
                    total_coin=None,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 12:
            rows.append(
                EventRow(
                    room_id=room_id,
                    cmd="PREPARING",
                    uid=None,
                    uname=None,
                    content=None,
                    gift_name=None,
                    gift_num=None,
                    total_coin=None,
                    title=None,
                    timestamp=timestamp,
                )
            )
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1

    return rows, stats


def _merge_stats(target: dict[str, dict[int, int]], source: dict[str, dict[int, int]]) -> None:
    for group, data in source.items():
        if group not in target:
            target[group] = {}
        for key, value in data.items():
            target[group][key] = target[group].get(key, 0) + value


def process_pages_dir(pages_dir: Path, room_id: int) -> tuple[list[EventRow], dict[str, dict[int, int]], int]:
    page_files = _list_page_files(pages_dir)
    all_rows: list[EventRow] = []
    all_stats: dict[str, dict[int, int]] = {
        "total": {},
        "used": {},
        "ignored": {},
    }
    total_records = 0

    for page_path in page_files:
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        records, actors, room_emojis, _, _ = _extract_frame(payload)
        total_records += len(records)
        page_payload = {
            "records": records,
            "actors": actors,
            "roomEmojis": room_emojis,
        }
        rows, stats = _build_event_rows(page_payload, room_id)
        all_rows.extend(rows)
        _merge_stats(all_stats, stats)

    return all_rows, all_stats, total_records


def _event_key(row: EventRow) -> tuple[Any, ...]:
    if row.cmd == "DANMU_MSG":
        return (row.cmd, row.uid, row.content)
    if row.cmd in {"SEND_GIFT", "GUARD_BUY"}:
        return (row.cmd, row.uid, row.gift_name, row.gift_num, row.total_coin)
    if row.cmd == "SUPER_CHAT_MESSAGE":
        return (row.cmd, row.uid, row.content, row.total_coin)
    if row.cmd == "ROOM_CHANGE":
        return (row.cmd, row.title)
    return (row.cmd,)


def _load_existing_events(room_id: int, min_ts: int, max_ts: int) -> dict[tuple[Any, ...], list[int]]:
    existing: dict[tuple[Any, ...], list[int]] = {}
    with connect_sqlite() as conn:
        rows = conn.execute(
            """
            SELECT cmd, uid, content, gift_name, gift_num, total_coin, title, timestamp
            FROM event
            WHERE room_id = ? AND timestamp BETWEEN ? AND ?
            """,
            (room_id, min_ts, max_ts),
        ).fetchall()

    for cmd, uid, content, gift_name, gift_num, total_coin, title, timestamp in rows:
        if cmd == "DANMU_MSG":
            key = (cmd, uid, content)
        elif cmd in ("SEND_GIFT", "GUARD_BUY"):
            key = (cmd, uid, gift_name, gift_num, total_coin)
        elif cmd == "SUPER_CHAT_MESSAGE":
            key = (cmd, uid, content, total_coin)
        elif cmd == "ROOM_CHANGE":
            key = (cmd, title)
        else:
            key = (cmd,)
        existing.setdefault(key, []).append(int(timestamp))

    return existing


def _filter_duplicates(rows: list[EventRow], existing_map: dict[tuple[Any, ...], list[int]]) -> tuple[list[EventRow], dict[str, int]]:
    filtered: list[EventRow] = []
    new_map: dict[tuple[Any, ...], list[int]] = {}
    removed_by_cmd: dict[str, int] = {}

    for row in rows:
        key = _event_key(row)
        timestamps = existing_map.get(key, [])
        if any(abs(row.timestamp - ts) <= DEDUPE_WINDOW_SECONDS for ts in timestamps):
            removed_by_cmd[row.cmd] = removed_by_cmd.get(row.cmd, 0) + 1
            continue
        new_ts = new_map.get(key, [])
        if any(abs(row.timestamp - ts) <= DEDUPE_WINDOW_SECONDS for ts in new_ts):
            removed_by_cmd[row.cmd] = removed_by_cmd.get(row.cmd, 0) + 1
            continue
        new_ts.append(row.timestamp)
        new_map[key] = new_ts
        filtered.append(row)

    return filtered, removed_by_cmd


def _backup_database(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with connect_sqlite(db_path) as src, connect_sqlite(backup_path) as dst:
        src.backup(dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch danmaku pages and optionally import to DB.")
    parser.add_argument("live_hash", help="Live hash, e.g. 67769e7b-3b72-42a0-a901-319792cc419e")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Page size for API requests")
    parser.add_argument("--output-dir", default=None, help="Directory to save per-page json files")
    parser.add_argument("--process-dir", default=None, help="Directory containing per-page json files")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching pages (process only)")
    parser.add_argument("--db-path", default="data/libot.db", help="SQLite database path")
    parser.add_argument("--room-id", type=int, default=None, help="Override room_id when writing to DB")
    parser.add_argument("--commit", action="store_true", help="Write to DB (otherwise dry-run)")
    parser.add_argument("--no-backup", action="store_true", help="Skip DB backup before commit")
    args = parser.parse_args()

    live_hash = args.live_hash.strip()
    if not live_hash:
        print("live_hash is required", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "data" / "danmaku" / live_hash
    process_dir = Path(args.process_dir) if args.process_dir else None

    if args.no_fetch and process_dir is None:
        print("--no-fetch requires --process-dir", file=sys.stderr)
        return 2

    if not args.no_fetch:
        pages = fetch_pages_to_dir(live_hash, limit=args.limit, output_dir=output_dir)
        print(f"Fetched pages: {len(pages)} -> {output_dir}")

    if process_dir is None:
        return 0

    if not process_dir.exists():
        print(f"process_dir not found: {process_dir}", file=sys.stderr)
        return 2

    meta_path = process_dir / "meta.json"
    room_id = args.room_id
    if room_id is None and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        channel = meta.get("channel") if isinstance(meta.get("channel"), dict) else {}
        room_id = _safe_int(channel.get("roomId"))
    if room_id is None:
        print("room_id is required (use --room-id or ensure meta.json contains roomId)", file=sys.stderr)
        return 2

    rows, stats, total_records = process_pages_dir(process_dir, room_id)
    if not rows:
        print(f"Processed 0 rows from {process_dir} (total records: {total_records})")
        return 0

    min_ts = min(row.timestamp for row in rows) - DEDUPE_WINDOW_SECONDS
    max_ts = max(row.timestamp for row in rows) + DEDUPE_WINDOW_SECONDS
    existing = _load_existing_events(room_id, min_ts, max_ts)
    rows, removed_by_cmd = _filter_duplicates(rows, existing)

    if not rows:
        print(f"0 new rows after dedupe from {process_dir}")
        return 0

    rows_by_cmd: dict[str, list[EventRow]] = {}
    for row in rows:
        rows_by_cmd.setdefault(row.cmd, []).append(row)

    if args.commit:
        db_path = Path(args.db_path)
        if not args.no_backup:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = db_path.parent / "backups" / f"libot_{stamp}.db"
            _backup_database(db_path, backup_path)
            print(f"Database backup created at {backup_path}")

        conn = connect_sqlite(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for cmd, cmd_rows in rows_by_cmd.items():
                conn.executemany(
                    """
                    INSERT INTO event (
                        room_id, cmd, uid, uname, content, gift_name, gift_num,
                        total_coin, title, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            row.title,
                            row.timestamp,
                        )
                        for row in cmd_rows
                    ],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        print("Dry-run: use --commit to write into DB.")

    print(
        f"Processed records={total_records}, pages={len(_list_page_files(process_dir))}. "
        f"New events={len(rows)}."
    )

    cmd_labels = {
        "DANMU_MSG": "type0弹幕",
        "SEND_GIFT": "type1礼物",
        "GUARD_BUY": "type2大航海",
        "SUPER_CHAT_MESSAGE": "type3醒目留言",
        "ROOM_CHANGE": "type5修改标题",
        "LIVE": "type11开播",
        "PREPARING": "type12下播",
    }
    type_by_cmd = {
        "DANMU_MSG": 0,
        "SEND_GIFT": 1,
        "GUARD_BUY": 2,
        "SUPER_CHAT_MESSAGE": 3,
        "ROOM_CHANGE": 5,
        "LIVE": 11,
        "PREPARING": 12,
    }
    for cmd, label in cmd_labels.items():
        parsed_count = stats.get("used", {}).get(type_by_cmd[cmd], 0)
        inserted_count = len(rows_by_cmd.get(cmd, [])) if rows_by_cmd else 0
        removed_count = removed_by_cmd.get(cmd, 0)
        print(f"{label}: parsed={parsed_count}, deduped={removed_count}, to_insert={inserted_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
