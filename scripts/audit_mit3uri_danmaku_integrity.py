"""Read-only audit of official-replay provenance and cross-source duplicates.

Outputs are evidence for review, not a deletion plan. Production connections
are read-only and scoped to one session at a time to avoid long WAL snapshots.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sqlite3
import statistics
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.backfill_mit3uri_replay_danmaku import (
    ROOM_ID, decode_proto_fields, normalize_content, proto_int, proto_text,
)

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def save_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat()


def audit_raw(db, work, recordings, output):
    totals = collections.Counter()
    unusual = []
    summaries = []
    with output.joinpath("replay_comment_candidates.jsonl").open("w") as out:
        for n, rec in enumerate(recordings, 1):
            stats = collections.Counter()
            # Rows from independent live sources must survive even if a later
            # replay comment happened to match them during historical import.
            with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
                for page in rec["pages"]:
                    cid = page["cid"]
                    for path in sorted((work / "segments" / str(cid)).glob("*.pb")):
                        for encoded in decode_proto_fields(path.read_bytes()).get(1, []):
                            if not isinstance(encoded, bytes):
                                continue
                            fields = decode_proto_fields(encoded)
                            origin = proto_int(fields, 27)
                            attr = proto_int(fields, 13)
                            ctime = proto_int(fields, 8)
                            stats[f"origin_{origin}"] += 1
                            stats["raw_rows"] += 1
                            if ctime > rec["pubdate"]:
                                stats[f"after_publish_origin_{origin}"] += 1
                            if origin == 3:
                                continue
                            dm_id = proto_text(fields, 12) or str(proto_int(fields, 1))
                            prov = conn.execute(
                                "SELECT p.event_id,p.match_kind,e.uid,e.content,e.timestamp "
                                "FROM mit3uri_replay_danmaku_provenance p "
                                "JOIN event e ON e.id=p.event_id "
                                "WHERE p.source='bilibili_official_replay' AND p.cid=? AND p.dm_id=?",
                                (cid, dm_id),
                            ).fetchone()
                            classification = (
                                "late_video_comment" if origin == 1 and ctime > rec["pubdate"] + 300
                                else "video_comment_near_publish" if origin == 1 and ctime > rec["pubdate"]
                                else "review_unknown_origin"
                            )
                            stats[classification] += 1
                            if prov:
                                stats[classification + "_" + prov[1]] += 1
                            item = dict(
                                session_id=rec["session_id"], live_date=rec["live_date"],
                                bvid=rec["bvid"], cid=cid, dm_id=dm_id,
                                raw_origin=origin, attr=attr, classification=classification,
                                ctime=ctime, sent_at=iso(ctime), pubdate=iso(rec["pubdate"]),
                                seconds_after_publish=ctime-rec["pubdate"],
                                event_id=prov[0] if prov else None,
                                match_kind=prov[1] if prov else None,
                                uid=prov[2] if prov else None,
                                content=proto_text(fields, 7),
                                mapped_time=iso(prov[4]) if prov else None,
                            )
                            out.write(json.dumps(item, ensure_ascii=False) + "\n")
                            if prov:
                                unusual.append(item)
            totals.update(stats)
            summaries.append(dict(session_id=rec["session_id"], live_date=rec["live_date"], bvid=rec["bvid"], **stats))
            if n % 50 == 0:
                print(f"raw {n}/{len(recordings)}", flush=True)
    save_json(output / "raw_summary.json", dict(totals=totals, recordings=summaries))
    save_csv(output / "imported_video_comments.csv", unusual)
    return dict(totals)


def unique_anchors(replays, baseline):
    left, right = collections.defaultdict(list), collections.defaultdict(list)
    for r in replays:
        left[(r["uid"], normalize_content(r["content"]))].append(r)
    for r in baseline:
        right[(r["uid"], normalize_content(r["content"]))].append(r)
    anchors = collections.defaultdict(list)
    for key, rs in left.items():
        others = right.get(key, [])
        if len(rs) != 1 or len(others) != 1 or len(key[1]) < 6 or key[1].startswith("["):
            continue
        replay, base = rs[0], others[0]
        delta = replay["mapped_timestamp_ms"] / 1000 - base["timestamp"]
        if abs(delta) <= 300:
            cell = (replay["cid"], replay["progress_ms"] // 300000)
            anchors[cell].append((delta, key[0], replay["event_id"], base["id"]))
    return anchors, right


def audit_duplicates(db, recordings, output):
    meta = {r["session_id"]: r for r in recordings}
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        sessions = conn.execute(
            "SELECT session_id,MIN(mapped_timestamp_ms),MAX(mapped_timestamp_ms) "
            "FROM mit3uri_replay_danmaku_provenance GROUP BY session_id ORDER BY MIN(mapped_timestamp_ms)"
        ).fetchall()
    summaries, cells, pairs = [], [], []
    for n, (sid, start, end) in enumerate(sessions, 1):
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            replay = [dict(r) for r in conn.execute(
                "SELECT p.*,e.uid,e.content,e.timestamp FROM mit3uri_replay_danmaku_provenance p "
                "JOIN event e ON e.id=p.event_id WHERE p.session_id=?", (sid,))]
            baseline = [dict(r) for r in conn.execute(
                "SELECT e.id,e.uid,e.content,e.timestamp FROM event e "
                "LEFT JOIN mit3uri_replay_danmaku_provenance p ON p.event_id=e.id "
                "WHERE e.room_id=? AND e.cmd='DANMU_MSG' AND e.timestamp BETWEEN ? AND ? "
                "AND (p.match_kind IS NULL OR p.match_kind='existing') ORDER BY e.timestamp,e.id",
                (ROOM_ID, start//1000-300, end//1000+300))]
        anchors, lookup = unique_anchors(replay, baseline)
        models = {}
        for cell, values in anchors.items():
            if len(values) < 6:
                continue
            center = statistics.median(v[0] for v in values)
            close = [v for v in values if abs(v[0]-center) <= 2.5]
            if len(close)/len(values) < .8 or len({v[1] for v in close}) < 5:
                continue
            models[cell] = (center, len(close))
            cells.append(dict(session_id=sid, live_date=meta[sid]["live_date"], cid=cell[0],
                              progress_bin=cell[1], offset_seconds=round(center,3),
                              agreeing_anchors=len(close), anchors=len(values)))
        used = set()
        session_pairs = []
        for r in sorted(replay, key=lambda x: x["mapped_timestamp_ms"]):
            if r["match_kind"] != "inserted":
                continue
            model = models.get((r["cid"], r["progress_ms"]//300000))
            if model is None or abs(model[0]) <= 5:
                continue
            choices = [b for b in lookup.get((r["uid"], normalize_content(r["content"])), [])
                       if b["id"] not in used and abs(r["mapped_timestamp_ms"]/1000-b["timestamp"]-model[0]) <= 2.5]
            if len(choices) != 1:
                continue
            b = choices[0]
            used.add(b["id"])
            session_pairs.append(dict(
                session_id=sid, live_date=meta[sid]["live_date"], bvid=r["bvid"], cid=r["cid"],
                extra_event_id=r["event_id"], baseline_event_id=b["id"], uid=r["uid"], content=r["content"],
                replay_time=iso(r["timestamp"]), baseline_time=iso(b["timestamp"]),
                delta_seconds=round(r["mapped_timestamp_ms"]/1000-b["timestamp"],3),
                local_offset_seconds=round(model[0],3), anchor_count=model[1],
            ))
        pairs.extend(session_pairs)
        shifted = [v for v in cells if v["session_id"]==sid and abs(v["offset_seconds"])>5]
        summaries.append(dict(session_id=sid,live_date=meta[sid]["live_date"],replay_rows=len(replay),
                              inserted_rows=sum(r["match_kind"]=="inserted" for r in replay),
                              shifted_cells=len(shifted), duplicate_candidates=len(session_pairs)))
        if n % 25 == 0:
            print(f"duplicates {n}/{len(sessions)} candidates={len(pairs)}",flush=True)
    save_csv(output / "duplicate_candidates.csv", pairs)
    save_csv(output / "local_offsets.csv", cells)
    save_csv(output / "sessions.csv", summaries)
    result = dict(sessions=len(sessions), candidate_pairs=len(pairs),
                  affected_sessions=sum(s["duplicate_candidates"]>0 for s in summaries),
                  top_sessions=sorted(summaries,key=lambda s:s["duplicate_candidates"],reverse=True)[:30])
    save_json(output / "duplicates_summary.json", result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db",type=Path,default=ROOT/"data/libot.db")
    p.add_argument("--work-dir",type=Path,default=ROOT/"data/mit3uri_replay_danmaku")
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--mode",choices=("raw","duplicates"),required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    recordings=json.loads((args.work_dir/"metadata.json").read_text())["recordings"]
    if args.mode=="raw": result=audit_raw(args.db,args.work_dir,recordings,args.output)
    else: result=audit_duplicates(args.db,recordings,args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
