"""Reconcile a saved Danmakus full frame with local blind-box results."""
import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import msgpack

ROOT = Path(__file__).resolve().parents[1]


def reconcile(conn, frame):
    channel, live, stream = frame
    room = int(channel[2])
    timestamp, actors, _, records = stream
    expected = defaultdict(lambda: [0, 0, ""])
    for record in records:
        timestamp += record[0]
        if record[1] != 1 or record[4] != 3:
            continue
        payload = record[5]
        if len(payload) < 5 or not payload[3]:
            continue
        actor = actors[record[2] - 1]
        key = (int(actor[0]), str(payload[0]), timestamp // 1000)
        item = expected[key]
        item[0] += int(payload[1])
        item[1] += int(payload[4])
        item[2] = str(actor[1])
    changes, review = [], []
    for (uid, gift, ts), (count, cost, uname) in expected.items():
        rows = conn.execute(
            "SELECT id,gift_num,total_coin FROM event WHERE room_id=? AND uid=? AND timestamp=? AND cmd='SEND_GIFT' AND gift_name=?",
            (room, uid, ts, gift),
        ).fetchall()
        actual_count = sum(r[1] or 0 for r in rows)
        actual_cost = sum(r[2] or 0 for r in rows)
        if (actual_count, actual_cost) == (count, cost):
            continue
        if cost % count or actual_cost != actual_count * (cost // count) or actual_count > count:
            review.append(dict(uid=uid, gift=gift, timestamp=ts, expected=[count,cost], existing=rows))
            continue
        # Repeated-field loss leaves another result at the same time for this user.
        sibling = conn.execute(
            "SELECT id FROM event WHERE room_id=? AND uid=? AND timestamp=? AND cmd='SEND_GIFT' LIMIT 1",
            (room, uid, ts),
        ).fetchone()
        if not sibling:
            review.append(dict(uid=uid, gift=gift, timestamp=ts, reason="no local batch anchor"))
            continue
        changes.append((room, "SEND_GIFT", uid, uname, None, gift, count-actual_count,
                        cost-actual_cost, None, ts))
    return changes, review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frame", type=Path)
    parser.add_argument("--database", type=Path, default=ROOT / "data/libot.db")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    frame = msgpack.unpackb(args.frame.read_bytes(), raw=False, strict_map_key=False)
    uri = f"file:{args.database.resolve()}?mode={'rw' if args.apply else 'ro'}"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
        changes, review = reconcile(conn, frame)
        report = dict(source=str(args.frame.resolve()), changes=changes, review=review,
                      inserted_ids=[], applied=args.apply)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        # Save the proposed mutations before applying. Keep a unique report per run.
        with args.report.open("x", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        if args.apply:
            for row in changes:
                cursor = conn.execute(
                    "INSERT INTO event(room_id,cmd,uid,uname,content,gift_name,gift_num,total_coin,title,timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                report["inserted_ids"].append(cursor.lastrowid)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(rows=len(changes), draws=sum(r[6] for r in changes),
                         cost=sum(r[7] for r in changes), review=len(review), applied=args.apply)))


if __name__ == "__main__":
    main()
