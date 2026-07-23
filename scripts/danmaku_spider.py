#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.sqlite import connect_sqlite

BASE_URL = "https://ukamnads.icu/api/v3/lives"
DEFAULT_LIMIT = 10000
TRACKED_TYPES = {0, 1, 2, 3, 5, 11, 12}
DEDUPE_WINDOW_SECONDS = 3
SESSION_MARKER_DEDUPE_WINDOW_SECONDS = 60
DB_PATH = PROJECT_ROOT / "data" / "libot.db"
EMOJI_JSON_PATH = PROJECT_ROOT / "scripts" / "emoji.json"

def _load_emoji_dict() -> dict[str, str]:
    if not EMOJI_JSON_PATH.exists():
        return {}
    try:
        content = EMOJI_JSON_PATH.read_text(encoding="utf-8")
        return json.loads(content)
    except Exception as e:
        print(f"警告: 读取 emoji.json 失败: {e}", file=sys.stderr)
        return {}

def _save_emoji_dict(emoji_dict: dict[str, str]) -> None:
    EMOJI_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    EMOJI_JSON_PATH.write_text(
        json.dumps(emoji_dict, ensure_ascii=False, indent=4), 
        encoding="utf-8"
    )

EMOJI_DICT = _load_emoji_dict()

def _request_json(url: str, timeout: int = 30) -> Any:
    transport = httpx.HTTPTransport(retries=3, local_address="0.0.0.0")
    with httpx.Client(
        headers={"User-Agent": "libot2-danmaku-spider/1.0"},
        timeout=timeout,
        transport=transport,
        trust_env=False,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()

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

def _build_event_rows(payload: dict[str, Any], room_id: int) -> tuple[list[EventRow], dict[str, dict[int, int]], dict[str, str]]:
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    actors = payload.get("actors") if isinstance(payload.get("actors"), list) else []
    room_emojis = payload.get("roomEmojis") if isinstance(payload.get("roomEmojis"), list) else []

    rows: list[EventRow] = []
    stats: dict[str, dict[int, int]] = {"total": {}, "used": {}, "ignored": {}}
    missing_emojis: dict[str, str] = {}
    
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
        uid = uname = None
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
                        emoji_name = emoji.get("name", "未知表情")
                        if isinstance(resource, str):
                            content = EMOJI_DICT.get(resource)
                            # 如果字典中没有，构建格式化文本并记录到 missing_emojis 中
                            if content is None:
                                content = f"[{emoji_name}]"
                                missing_emojis[resource] = content
                                # 同时临时追加到内存，防止同一 page 多次记录
                                EMOJI_DICT[resource] = content 
            else:
                if isinstance(record_type, int):
                    stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            if not content:
                if isinstance(record_type, int):
                    stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            rows.append(EventRow(room_id, "DANMU_MSG", uid, uname, content, None, None, None, None, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 1:
            if payload_kind != 3:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            gift_name = payload_data.get("name") if isinstance(payload_data.get("name"), str) else None
            gift_num = _safe_int(payload_data.get("count")) or 1
            price = payload_data.get("price")
            total_coin = int(round(float(price) * 1000)) if isinstance(price, (int, float)) else None
            rows.append(EventRow(room_id, "SEND_GIFT", uid, uname, None, gift_name, gift_num, total_coin, None, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 2:
            if payload_kind != 4:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            gift_name = payload_data.get("name") if isinstance(payload_data.get("name"), str) else None
            gift_num = _safe_int(payload_data.get("count")) or 1
            price = payload_data.get("price")
            total_coin = int(round(float(price) * 1000)) if isinstance(price, (int, float)) else None
            rows.append(EventRow(room_id, "GUARD_BUY", uid, uname, None, gift_name, gift_num, total_coin, None, timestamp))
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
            rows.append(EventRow(room_id, "SUPER_CHAT_MESSAGE", uid, uname, content, "醒目留言", 1, total_coin, None, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 5:
            if payload_kind != 6:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            title = payload_data.get("currentTitle") if isinstance(payload_data.get("currentTitle"), str) else None
            if not title:
                stats["ignored"][record_type] = stats["ignored"].get(record_type, 0) + 1
                continue
            rows.append(EventRow(room_id, "ROOM_CHANGE", None, None, None, None, None, None, title, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 11:
            rows.append(EventRow(room_id, "LIVE", None, None, None, None, None, None, None, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1
        elif record_type == 12:
            rows.append(EventRow(room_id, "PREPARING", None, None, None, None, None, None, None, timestamp))
            stats["used"][record_type] = stats["used"].get(record_type, 0) + 1

    return rows, stats, missing_emojis

def _merge_stats(target: dict[str, dict[int, int]], source: dict[str, dict[int, int]]) -> None:
    for group, data in source.items():
        if group not in target:
            target[group] = {}
        for key, value in data.items():
            target[group][key] = target[group].get(key, 0) + value

def process_pages_dir(pages_dir: Path, room_id: int) -> tuple[list[EventRow], dict[str, dict[int, int]], int, dict[str, str]]:
    page_files = _list_page_files(pages_dir)
    all_rows: list[EventRow] = []
    all_stats: dict[str, dict[int, int]] = {"total": {}, "used": {}, "ignored": {}}
    all_missing_emojis: dict[str, str] = {}
    total_records = 0

    for page_path in page_files:
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        records, actors, room_emojis, _, _ = _extract_frame(payload)
        total_records += len(records)
        page_payload = {"records": records, "actors": actors, "roomEmojis": room_emojis}
        
        rows, stats, missing_emojis = _build_event_rows(page_payload, room_id)
        all_rows.extend(rows)
        _merge_stats(all_stats, stats)
        all_missing_emojis.update(missing_emojis)

    return all_rows, all_stats, total_records, all_missing_emojis

def _event_key(row: EventRow) -> tuple[Any, ...]:
    if row.cmd == "DANMU_MSG": return (row.cmd, row.uid, row.content)
    if row.cmd in {"SEND_GIFT", "GUARD_BUY"}: return (row.cmd, row.uid, row.gift_name, row.gift_num, row.total_coin)
    if row.cmd == "SUPER_CHAT_MESSAGE": return (row.cmd, row.uid, row.content, row.total_coin)
    if row.cmd == "ROOM_CHANGE": return (row.cmd, row.title)
    return (row.cmd,)

def _load_existing_events(room_id: int, min_ts: int, max_ts: int) -> dict[tuple[Any, ...], list[int]]:
    existing: dict[tuple[Any, ...], list[int]] = {}
    with connect_sqlite(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT cmd, uid, content, gift_name, gift_num, total_coin, title, timestamp
            FROM event
            WHERE room_id = ? AND timestamp BETWEEN ? AND ?
            """,
            (room_id, min_ts, max_ts),
        ).fetchall()

    for cmd, uid, content, gift_name, gift_num, total_coin, title, timestamp in rows:
        if cmd == "DANMU_MSG": key = (cmd, uid, content)
        elif cmd in ("SEND_GIFT", "GUARD_BUY"): key = (cmd, uid, gift_name, gift_num, total_coin)
        elif cmd == "SUPER_CHAT_MESSAGE": key = (cmd, uid, content, total_coin)
        elif cmd == "ROOM_CHANGE": key = (cmd, title)
        else: key = (cmd,)
        existing.setdefault(key, []).append(int(timestamp))

    return existing

def _filter_duplicates(rows: list[EventRow], existing_map: dict[tuple[Any, ...], list[int]]) -> tuple[list[EventRow], dict[str, int]]:
    filtered: list[EventRow] = []
    new_map: dict[tuple[Any, ...], list[int]] = {}
    removed_by_cmd: dict[str, int] = {}

    for row in rows:
        key = _event_key(row)
        window = (
            SESSION_MARKER_DEDUPE_WINDOW_SECONDS
            if row.cmd in {"LIVE", "PREPARING"}
            else DEDUPE_WINDOW_SECONDS
        )
        timestamps = existing_map.get(key, [])
        if any(abs(row.timestamp - ts) <= window for ts in timestamps):
            removed_by_cmd[row.cmd] = removed_by_cmd.get(row.cmd, 0) + 1
            continue
        new_ts = new_map.get(key, [])
        if any(abs(row.timestamp - ts) <= window for ts in new_ts):
            removed_by_cmd[row.cmd] = removed_by_cmd.get(row.cmd, 0) + 1
            continue
        new_ts.append(row.timestamp)
        new_map[key] = new_ts
        filtered.append(row)

    return filtered, removed_by_cmd

def main() -> int:
    if len(sys.argv) != 2:
        print("用法: python danmaku_spider.py <live_hash>", file=sys.stderr)
        return 2

    live_hash = sys.argv[1].strip()
    target_dir = PROJECT_ROOT / "data" / "danmaku" / live_hash

    if _list_page_files(target_dir):
        print(f"发现已有数据目录: {target_dir}，跳过获取步骤。")
    else:
        print(f"未在 {target_dir} 发现数据，开始获取...")
        fetch_pages_to_dir(live_hash, limit=DEFAULT_LIMIT, output_dir=target_dir)

    meta_path = target_dir / "meta.json"
    if not meta_path.exists():
        print(f"致命错误: {meta_path} 不存在，无法获取 room_id", file=sys.stderr)
        return 2

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    room_id = _safe_int(meta.get("channel", {}).get("roomId"))
    if room_id is None:
        print("致命错误: 无法从 meta.json 解析出 roomId", file=sys.stderr)
        return 2

    # 获取所有页面的数据、统计信息以及未知的表情
    rows, stats, total_records, missing_emojis = process_pages_dir(target_dir, room_id)

    # 发现未知表情时自动回写 JSON 并播报，不需要中止运行
    if missing_emojis:
        print(f"\n自动学习了 {len(missing_emojis)} 个新表情映射并更新了 emoji.json。")
        _save_emoji_dict(EMOJI_DICT)

    if not rows:
        print(f"处理了 0 行数据 (总记录数: {total_records})")
        return 0

    min_ts = min(row.timestamp for row in rows) - SESSION_MARKER_DEDUPE_WINDOW_SECONDS
    max_ts = max(row.timestamp for row in rows) + SESSION_MARKER_DEDUPE_WINDOW_SECONDS
    existing = _load_existing_events(room_id, min_ts, max_ts)
    rows, removed_by_cmd = _filter_duplicates(rows, existing)

    if not rows:
        print("去重后无新数据需插入。")
        return 0

    rows_by_cmd: dict[str, list[EventRow]] = {}
    for row in rows:
        rows_by_cmd.setdefault(row.cmd, []).append(row)

    conn = connect_sqlite(DB_PATH)
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
                [(r.room_id, r.cmd, r.uid, r.uname, r.content, r.gift_name, r.gift_num, r.total_coin, r.title, r.timestamp) for r in cmd_rows]
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"完成。处理记录={total_records}。新插入事件={len(rows)}。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())