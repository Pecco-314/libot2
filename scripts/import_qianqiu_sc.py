#!/usr/bin/env python3
"""按月核对并补充斗虫公开 SC 记录；默认预览，--apply 导入已缓存响应。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "https://vr.qianqiuzy.cn/gift/sc"
WINDOW = 3
DELAY_WINDOW = 300


def months_between(start, end):
    current = datetime.strptime(start, "%Y-%m")
    finish = datetime.strptime(end, "%Y-%m")
    if current > finish:
        raise ValueError("起始月份晚于结束月份")
    while current <= finish:
        yield current.strftime("%Y-%m")
        current = current.replace(year=current.year + (current.month == 12), month=current.month % 12 + 1)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def fetch_months(directory, room, months):
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, trust_env=False) as client:
        for month in months:
            path = directory / f"{month}.json"
            if path.exists():
                continue
            for attempt in range(5):
                response = client.get(SOURCE, params={"room_id": room, "month": month})
                if response.status_code not in {429, 503}:
                    break
                if attempt == 4:
                    response.raise_for_status()
                delay = max(1, min(30, float(response.headers.get("Retry-After", 2 ** attempt))))
                time.sleep(delay)
            response.raise_for_status()
            payload = response.json()
            normalize(payload, room, month)
            write_json(path, {"source": SOURCE, "fetched_at": datetime.now(TZ).isoformat(), "payload": payload})
            print(json.dumps({"month": month, "fetched": len(payload["list"])}), flush=True)
            time.sleep(1)


def clean_text(value):
    return " ".join(str(value).split())


def normalize(payload, room, month):
    if int(payload["room_id"]) != room or str(payload["month"]) != month.replace("-", ""):
        raise ValueError("响应房间或月份不匹配")
    if not isinstance(payload["list"], list):
        raise ValueError("SC 列表格式不正确")
    result = []
    for index, raw in enumerate(payload["list"]):
        sent = datetime.strptime(raw["send_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        if sent.strftime("%Y-%m") != month:
            raise ValueError("SC 时间不在请求月份内")
        price = Decimal(str(raw["price"]))
        if not price.is_finite() or price < 0 or price != int(price):
            raise ValueError("SC 金额不是非负整数元")
        if int(raw["uid"]) <= 0 or not isinstance(raw["message"], str):
            raise ValueError("SC UID 或内容无效")
        if int(sent.timestamp()) > int(time.time()) + 60:
            raise ValueError("SC 时间晚于当前时间")
        row = {"room_id": room, "uid": int(raw["uid"]), "uname": str(raw["uname"] or ""),
               "content": clean_text(raw["message"]), "price": int(price), "timestamp": int(sent.timestamp()),
               "month": month, "source_index": index}
        key = json.dumps([room, row["uid"], row["content"], row["price"], row["timestamp"]], ensure_ascii=False)
        row["source_key"] = hashlib.sha256(key.encode()).hexdigest()
        result.append(row)
    return result


def load_source(directory, room, months):
    rows = []
    for month in months:
        data = json.loads((directory / f"{month}.json").read_text())
        rows.extend(normalize(data["payload"], room, month))
    return rows


def load_existing(conn, room, start, end):
    # SC 专用部分索引避免扫描整个房间的弹幕历史。
    return [{"id": r[0], "uid": r[1], "content": clean_text(r[2] or ""), "price": r[3], "timestamp": r[4]}
            for r in conn.execute("""SELECT id,uid,content,total_coin,timestamp
                FROM event INDEXED BY idx_event_sc_unique WHERE room_id=? AND cmd='SUPER_CHAT_MESSAGE'
                AND timestamp>=? AND timestamp<?""", (room, start-DELAY_WINDOW, end+DELAY_WINDOW))]


def identity(row):
    return row["uid"], row["content"], row["price"]


def reconcile(rows, existing):
    by_identity, by_user = defaultdict(list), defaultdict(list)
    for row in existing:
        by_identity[identity(row)].append(row)
        by_user[row["uid"]].append(row)
    unique, source_seen = [], set()
    counts = defaultdict(Counter)
    for row in rows:
        counts[row["month"]]["source"] += 1
        if row["source_key"] in source_seen:
            counts[row["month"]]["source_duplicates"] += 1
        else:
            source_seen.add(row["source_key"])
            unique.append(row)
    matched, consumed, pending = [], set(), []
    source_counts = Counter(identity(row) for row in unique)
    for row in sorted(unique, key=lambda r: r["timestamp"]):
        exact = next((e for e in by_identity[identity(row)]
                      if e["timestamp"] == row["timestamp"] and e["id"] not in consumed), None)
        if exact:
            consumed.add(exact["id"])
            matched.append({"source_key": row["source_key"], "event_id": exact["id"], "delta_seconds": 0})
            counts[row["month"]]["exact"] += 1
        else:
            pending.append(row)
    missing, conflicts = [], []
    for row in pending:
        candidates = [e for e in by_identity[identity(row)]
                      if e["id"] not in consumed and abs(e["timestamp"]-row["timestamp"]) <= WINDOW]
        if candidates:
            target = min(candidates, key=lambda e: (abs(e["timestamp"]-row["timestamp"]), e["id"]))
            consumed.add(target["id"])
            matched.append({"source_key": row["source_key"], "event_id": target["id"],
                            "delta_seconds": target["timestamp"]-row["timestamp"]})
            counts[row["month"]]["nearby"] += 1
            continue
        # 历史采集存在分钟级延迟；完整身份唯一且一对一时匹配，避免合并重复发送的短句。
        delayed = [e for e in by_identity[identity(row)]
                   if abs(e["timestamp"]-row["timestamp"]) <= DELAY_WINDOW]
        if delayed:
            if (len(by_identity[identity(row)]) == 1 and source_counts[identity(row)] == 1
                    and delayed[0]["id"] not in consumed):
                target = delayed[0]
                consumed.add(target["id"])
                matched.append({"source_key": row["source_key"], "event_id": target["id"],
                                "delta_seconds": target["timestamp"]-row["timestamp"]})
                counts[row["month"]]["delayed_unique"] += 1
            else:
                conflicts.append({"source": row, "existing": delayed, "reason": "ambiguous delayed repetition"})
                counts[row["month"]]["conflicts"] += 1
            continue
        nearby = [e for e in by_user[row["uid"]] if abs(e["timestamp"]-row["timestamp"]) <= WINDOW]
        if nearby:
            conflicts.append({"source": row, "existing": nearby})
            counts[row["month"]]["conflicts"] += 1
        else:
            missing.append(row)
            counts[row["month"]]["missing"] += 1
    return {"months": dict(counts), "matched": matched, "missing": missing, "conflicts": conflicts,
            "time_offset_counts": dict(Counter(m["delta_seconds"] for m in matched))}


def insert_missing(db_path, rows, manifest_path, room):
    """小批次提交，并在写锁内重新核对，保留实际插入ID及原始字段。"""
    from src.db.sqlite import write_transaction
    manifest = {"source": SOURCE, "room_id": room, "created_at": datetime.now(TZ).isoformat(),
                "inserted": [], "skipped_during_apply": [], "pending_batch": [], "complete": False}
    write_json(manifest_path, manifest)
    for start in range(0, len(rows), 100):
        with write_transaction(db_path) as conn:
            batch = rows[start:start+100]
            existing = load_existing(conn, room, min(r["timestamp"] for r in batch), max(r["timestamp"] for r in batch)+1)
            fresh = reconcile(batch, existing)
            accepted = {r["source_key"] for r in fresh["missing"]}
            manifest["skipped_during_apply"].extend(r for r in batch if r["source_key"] not in accepted)
            inserted = []
            for row in fresh["missing"]:
                cursor = conn.execute("""INSERT INTO event
                    (room_id,cmd,uid,uname,content,gift_name,gift_num,total_coin,title,timestamp)
                    VALUES (?,'SUPER_CHAT_MESSAGE',?,?,?,'醒目留言',1,?,NULL,?)""",
                    (room, row["uid"], row["uname"], row["content"], row["price"], row["timestamp"]))
                inserted.append({**row, "event_id": cursor.lastrowid})
                if row["uname"]:
                    conn.execute("""INSERT INTO name_history(uid,uname,first_seen) VALUES(?,?,?)
                        ON CONFLICT(uid,uname) DO UPDATE SET first_seen=MIN(COALESCE(first_seen,excluded.first_seen),excluded.first_seen)""",
                        (row["uid"], row["uname"], row["timestamp"]))
            manifest["pending_batch"] = inserted
            write_json(manifest_path, manifest)
        manifest["inserted"].extend(inserted)
        manifest["pending_batch"] = []
        write_json(manifest_path, manifest)
        time.sleep(.1)
    manifest["complete"] = True
    write_json(manifest_path, manifest)
    return manifest


def retire_image_cache(rows, run_dir, room):
    """只移动受新增SC影响的日期缓存，保留可恢复的备份。"""
    moved = []
    for day in sorted({datetime.fromtimestamp(r["timestamp"], TZ).strftime("%Y-%m-%d") for r in rows}):
        source = ROOT / "data" / "images" / "superchat" / str(room) / day
        if source.is_dir():
            target = run_dir / "image_cache_backup" / day
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            moved.append({"source": str(source), "backup": str(target)})
    return moved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-11")
    parser.add_argument("--end", default=datetime.now(TZ).strftime("%Y-%m"))
    parser.add_argument("--room", type=int, default=1967216004)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "libot.db")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    months = list(months_between(args.start, args.end))
    if args.fetch:
        fetch_months(args.directory, args.room, months)
    rows = load_source(args.directory, args.room, months)
    start = int(datetime.strptime(months[0], "%Y-%m").replace(tzinfo=TZ).timestamp())
    last = datetime.strptime(months[-1], "%Y-%m").replace(tzinfo=TZ)
    end = int(last.replace(year=last.year+(last.month==12), month=last.month%12+1).timestamp())
    with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as conn:
        existing = load_existing(conn, args.room, start, end)
    report = reconcile(rows, existing)
    run = args.directory / ("run_" + datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f"))
    run.mkdir(parents=True)
    write_json(run / "plan.json", report)
    print(json.dumps({"months": report["months"], "time_offsets": report["time_offset_counts"],
                      "missing": len(report["missing"]), "conflicts": len(report["conflicts"]), "report": str(run)}, ensure_ascii=False, indent=2), flush=True)
    if args.apply:
        manifest = insert_missing(args.database, report["missing"], run / "applied.json", args.room)
        moved = retire_image_cache(manifest["inserted"], run, args.room)
        write_json(run / "image_cache_backup.json", moved)
        with sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True) as conn:
            after = reconcile(rows, load_existing(conn, args.room, start, end))
        write_json(run / "verify.json", after)
        print(json.dumps({"inserted": len(manifest["inserted"]), "remaining_missing": len(after["missing"]),
                          "conflicts": len(after["conflicts"]), "cache_directories_moved": len(moved)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
