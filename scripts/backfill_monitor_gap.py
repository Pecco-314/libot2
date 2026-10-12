"""补回已核实的 monitor 短缺口，复用开播标记并保存 danmakus 来源证据。

主库只保存事件和幂等映射；原始记录留在冷归档。主库事务提交后可安全
重跑以完成冷库关联。正常 monitor 时段不参与补录，同源重复发送保持原样。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from scripts.danmaku_spider import (
    BASE_URL, _build_event_rows, _extract_frame, _request_json, fetch_live_meta,
)
from src.db import danmaku_sources as store
from src.db.sqlite import DEFAULT_DB_PATH


COLUMNS = (
    "room_id", "cmd", "uid", "uname", "content", "gift_name", "gift_num",
    "total_coin", "title", "timestamp",
)
LEDGER_SCHEMA = """
    CREATE TABLE IF NOT EXISTS monitor_gap_backfill (
        source_key TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK(source='danmakus'),
        source_session TEXT NOT NULL,
        event_id INTEGER NOT NULL REFERENCES event(id),
        cmd TEXT NOT NULL,
        source_timestamp_ms INTEGER NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        applied_at INTEGER NOT NULL
    ) WITHOUT ROWID
"""


def prepare(report: dict, live_evidence: dict) -> dict:
    room, start, end = (
        int(report[k]) for k in ("room_id", "live_start", "monitor_first_message")
    )
    selected = report["selected_source"]
    live_id = selected["live_id"]
    meta = live_evidence["metadata"]
    if (
        end <= start or meta["channel"]["roomId"] != room
        or meta["live"]["liveId"] != live_id
        or meta["live"]["startDate"] != start * 1000
    ):
        raise ValueError("danmakus 场次信息与已核实的缺口不一致")
    frame = selected["raw_frame"]
    candidates = selected["normalized_candidates"]
    if not candidates or len(frame["records"]) != len(candidates):
        raise ValueError("原始记录与候选数量不一致")
    items = []
    for slot, expected in zip(frame["records"], candidates, strict=True):
        decoded, _, _ = _build_event_rows({
            "records": [slot["record"]], "actors": frame["actors"],
            "roomEmojis": frame["roomEmojis"],
        }, room)
        if len(decoded) != 1 or asdict(decoded[0]) != expected:
            raise ValueError("原始记录与标准化候选不一致")
        if expected["cmd"] != "DANMU_MSG" or not start <= expected["timestamp"] < end:
            raise ValueError("候选超出弹幕短缺口范围")
        key = f'{live_id}:gap-page:{slot["page_offset"]}:record:{slot["record_index"]}'
        items.append({"key": key, "row": expected, "raw": slot})
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("原生记录位置重复")
    payload = store.encode({"gap_report": report, "live_evidence": live_evidence}).encode()
    return {
        "room": room, "start": start, "end": end, "live_id": live_id,
        "items": items, "payload": payload,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "live_key": f"{live_id}:live-meta:start:{start * 1000}",
    }


def inspect(conn: sqlite3.Connection, plan: dict) -> tuple[int, dict[str, int]]:
    markers = conn.execute(
        "SELECT id,timestamp FROM event INDEXED BY idx_event_cmd_time "
        "WHERE room_id=? AND cmd='LIVE' AND timestamp>=? AND timestamp<?",
        (plan["room"], plan["start"] - 10, plan["end"]),
    ).fetchall()
    if len(markers) != 1 or markers[0][1] != plan["start"]:
        raise ValueError("需要唯一且时间准确的现有开播标记；此脚本不新建开播通知")
    live_id = int(markers[0][0])
    recovered = conn.execute(
        "SELECT suppress_notice FROM live_event_recovery WHERE event_id=?", (live_id,),
    ).fetchone()
    notices = conn.execute(
        "SELECT status FROM live_notice_delivery WHERE event_id=?", (live_id,),
    ).fetchall()
    if not recovered or recovered[0] != 1 or not notices or any(r[0] != "suppressed" for r in notices):
        raise ValueError("本场开播通知必须已被明确抑制")
    existing = {}
    has_ledger = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='monitor_gap_backfill' AND type='table'",
    ).fetchone()
    if has_ledger:
        for item in plan["items"]:
            row = conn.execute(
                "SELECT event_id FROM monitor_gap_backfill WHERE source_key=?",
                (item["key"],),
            ).fetchone()
            if row:
                actual = conn.execute(
                    f'SELECT {",".join(COLUMNS)} FROM event WHERE id=?', row,
                ).fetchone()
                if actual != tuple(item["row"][k] for k in COLUMNS):
                    raise ValueError("已补录事件与来源记录不一致")
                existing[item["key"]] = int(row[0])
        linked_live = conn.execute(
            "SELECT event_id FROM monitor_gap_backfill WHERE source_key=?", (plan["live_key"],),
        ).fetchone()
        if linked_live and linked_live[0] != live_id:
            raise ValueError("开播来源已关联其他事件")
    gap_ids = {r[0] for r in conn.execute(
        "SELECT id FROM event INDEXED BY idx_event_room_time "
        "WHERE room_id=? AND cmd='DANMU_MSG' AND timestamp>=? AND timestamp<?",
        (plan["room"], plan["start"], plan["end"]),
    )}
    if gap_ids != set(existing.values()):
        raise ValueError("缺口内存在未核实的其他来源，拒绝覆盖或粗去重")
    return live_id, existing


def archive_evidence(plan: dict, archive_db: Path) -> None:
    archive_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(archive_db, timeout=1)) as cold:
        cold.execute("PRAGMA journal_mode=WAL")
        cold.execute("PRAGMA foreign_keys=ON")
        with cold:
            store.initialize(cold)
            aid = store.artifact(
                cold, "danmakus", f'{plan["live_id"]}/monitor-gap/{plan["start"]}',
                plan["payload"], {"purpose": "confirmed_monitor_gap", "includes_live_metadata": True},
            )
            store.add_records(cold, [store.record(
                "danmakus", item["key"], plan["live_id"], plan["room"],
                item["row"]["uid"], item["row"]["uname"], item["row"]["content"],
                item["raw"]["record"]["ts"], mapped=item["row"]["timestamp"] * 1000,
                artifact_id=aid, evidence={
                    "cmd": "DANMU_MSG", "purpose": "confirmed_monitor_gap",
                    "page_offset": item["raw"]["page_offset"],
                    "record_index": item["raw"]["record_index"],
                },
            ) for item in plan["items"]])


def link_archive(plan: dict, archive_db: Path, event_ids: dict[str, int], live_event_id: int) -> None:
    with closing(sqlite3.connect(archive_db, timeout=1)) as cold, cold:
        cold.executemany(
            "INSERT OR IGNORE INTO danmaku_source_link(source_id,event_id,method) "
            "SELECT id,?,'confirmed_monitor_gap' FROM danmaku_source_record "
            "WHERE source='danmakus' AND source_key=?",
            [(event_ids[item["key"]], item["key"]) for item in plan["items"]],
        )
        store.checkpoint(cold, f'monitor-gap:{plan["live_id"]}:{plan["start"]}', store.encode({
            "live_event_id": live_event_id, "live_source_key": plan["live_key"],
            "danmaku_count": len(plan["items"]), "artifact_sha256": plan["sha256"],
            "notification": "suppressed",
        }))


def backfill(report: dict, live_evidence: dict, *, apply: bool = False,
             db_path: Path = DEFAULT_DB_PATH, archive_db: Path = store.ARCHIVE_DB) -> dict:
    if db_path.resolve() == archive_db.resolve():
        raise ValueError("原始来源证据必须使用独立冷库")
    plan = prepare(report, live_evidence)
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
        live_event_id, existing = inspect(conn, plan)
    result = {
        "mode": "applied" if apply else "dry_run", "room_id": plan["room"],
        "live_start": plan["start"], "gap_end": plan["end"],
        "source_live_id": plan["live_id"], "live_event_id": live_event_id,
        "live_event_action": "reuse_existing_with_danmakus_evidence",
        "selected_danmaku": len(plan["items"]), "already_imported": len(existing),
        "inserted_danmaku": 0, "notice_status": "suppressed",
        "artifact_sha256": plan["sha256"],
    }
    if not apply:
        result["would_insert_danmaku"] = len(plan["items"]) - len(existing)
        return result
    archive_evidence(plan, archive_db)
    with closing(sqlite3.connect(db_path, timeout=1)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        live_event_id, existing = inspect(conn, plan)
        conn.execute(LEDGER_SCHEMA)
        insert_ledger = (
            "INSERT OR IGNORE INTO monitor_gap_backfill VALUES(?, 'danmakus', ?, ?, ?, ?, ?, ?)"
        )
        observed_at = int(time.time())
        conn.execute(insert_ledger, (
            plan["live_key"], plan["live_id"], live_event_id, "LIVE",
            plan["start"] * 1000, plan["sha256"], observed_at,
        ))
        event_ids = dict(existing)
        for item in plan["items"]:
            if item["key"] in existing:
                continue
            event_id = int(conn.execute(
                f'INSERT INTO event({",".join(COLUMNS)}) VALUES({",".join("?" for _ in COLUMNS)})',
                tuple(item["row"][k] for k in COLUMNS),
            ).lastrowid)
            conn.execute(insert_ledger, (
                item["key"], plan["live_id"], event_id, "DANMU_MSG",
                item["raw"]["record"]["ts"], plan["sha256"], observed_at,
            ))
            event_ids[item["key"]] = event_id
        result["inserted_danmaku"] = len(event_ids) - len(existing)
        result["already_imported"] = len(existing)
    link_archive(plan, archive_db, event_ids, live_event_id)
    result["cold_source_links"] = len(event_ids)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    live_id = report["selected_source"]["live_id"]
    meta = fetch_live_meta(live_id)
    opening = _request_json(f"{BASE_URL}/{live_id}/danmakus?offset=0&limit=1")
    records, _, _, _, _ = _extract_frame(opening)
    evidence = {"metadata": meta, "opening_records": records}
    print(json.dumps(backfill(report, evidence, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
