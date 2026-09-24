"""Extract Mit3uri blind-box protobuf evidence and produce a read-only DB diff."""
import argparse
import ast
import base64
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.monitor.monitor import _decode_proto_fields, _proto_bytes, _proto_int, _extract_v2_gift_rows
from scripts.recover_blind_box_results import reconcile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    evidence, failures, seen = [], [], set()
    actors, actor_ids, records = [], {}, []
    for line_number, line in enumerate(args.log.open(encoding="utf-8"), 1):
        if "_handle_command() failed" not in line or "SEND_GIFT_V2" not in line:
            continue
        counts["all_v2_failures"] += 1
        match = re.search(r"room=(\d+).*?command=(.*)", line)
        if not match or int(match[1]) != 1967216004:
            continue
        counts["target_v2_failures"] += 1
        try:
            command = ast.literal_eval(match[2])
            pb = command["data"]["pb"]
            digest = hashlib.sha256(pb.encode()).hexdigest()
            if digest in seen:
                counts["duplicate_pb"] += 1
                continue
            seen.add(digest)
            root = _decode_proto_fields(base64.b64decode(pb, validate=True))
            rows = _extract_v2_gift_rows(1967216004, command)
            for index, (detail, row) in enumerate(zip(root[10], rows)):
                gift = _decode_proto_fields(detail)
                receiver = _decode_proto_fields(_proto_bytes(gift, 29))
                if _proto_int(receiver, 2) != 2030198123:
                    counts["other_receiver"] += 1
                    continue
                # Retain the explicitly priced heart-box results, not direct gifts.
                if row[5] not in {"电影票", "棉花糖", "爱心抱枕", "绮彩权杖", "时空之站", "神驹宝玺", "浪漫城堡"} or row[7] != row[6]*15000:
                    continue
                if row[9] < int(datetime(2026,8,10,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()):
                    continue
                evidence.append(dict(line=line_number, detail_index=index, sha256=digest,
                                     logged_at=line[:23], time=datetime.fromtimestamp(row[9],ZoneInfo("Asia/Shanghai")).isoformat(),
                                     row=row, pb=pb))
                uid = row[2]
                if uid not in actor_ids:
                    actors.append([uid,row[3]])
                    actor_ids[uid] = len(actors)
                records.append([row[9]*1000,1,actor_ids[uid],0,3,[row[5],row[6],0,"心动盲盒",row[7]]])
        except Exception as exc:
            failures.append(dict(line=line_number, error=str(exc)))
    records.sort(key=lambda r:r[0])
    previous = 0
    for record in records:
        timestamp = record[0]
        record[0] -= previous
        previous = timestamp
    frame = [[2030198123,"三理Mit3uri",1967216004],[],[0,actors,[],records]]
    with sqlite3.connect(f"file:{ROOT / 'data/libot.db'}?mode=ro",uri=True) as conn:
        changes, review = reconcile(conn,frame)
    summary = dict(counts, blind_box_details=len(evidence), draws=sum(e["row"][6] for e in evidence),
                   missing_rows=len(changes), missing_draws=sum(r[6] for r in changes),
                   missing_cost=sum(r[7] for r in changes), review=len(review), failures=len(failures),
                   first=min((e["time"] for e in evidence),default=None), last=max((e["time"] for e in evidence),default=None))
    report = dict(source=str(args.log.resolve()), summary=summary, changes=changes, review=review, failures=failures,
                  by_day=dict(Counter(e["time"][:10] for e in evidence)))
    (args.output/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    (args.output/"dry_run.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    import msgpack
    (args.output/"results.msgpack").write_bytes(msgpack.packb(frame,use_bin_type=True))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
