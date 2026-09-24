"""Compare logged combo summaries with recorded gifts; never insert summaries blindly."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def audit(database: Path, logs: list[Path]) -> dict:
    results = []
    seen = set()
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as conn:
        for path in logs:
            for number, line in enumerate(path.open(), 1):
                match = re.search(r"房间 (\d+).*cmd=(COMBO_SEND|COMBO_END|GIFT_COMBO) raw=(.*)", line)
                if not match:
                    continue
                room, cmd, raw = match.groups()
                try:
                    data = json.loads(raw)["data"]
                    uid = int(data["uid"])
                    gift = data.get("gift_name") or data.get("giftName")
                    batch = data.get("combo_id") or data.get("batch_combo_id")
                    start = int(data.get("start_time") or data.get("timestamp") or float(batch.rsplit(":", 1)[1]))
                    end = int(data.get("end_time") or datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
                    count = int(data.get("batch_combo_num") or data.get("num") or data.get("combo_num"))
                    coin = int(data["combo_total_coin"])
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    results.append(dict(status="unparsed", file=str(path), line=number, error=str(exc)))
                    continue
                identity = (int(room), cmd, uid, gift, batch, start, end, count, coin)
                if identity in seen:
                    continue
                seen.add(identity)
                # Allow the observed sub-second timestamp rounding at the batch start.
                rows = conn.execute(
                    "SELECT id,gift_num,total_coin,timestamp FROM event WHERE room_id=? AND uid=? AND cmd='SEND_GIFT' AND gift_name=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp,id",
                    (int(room), uid, gift, start - 1, end + 1),
                ).fetchall()
                actual_count = sum(row[1] or 0 for row in rows)
                actual_coin = sum(row[2] or 0 for row in rows)
                status = "matched" if (actual_count, actual_coin) == (count, coin) else "review"
                receiver = data.get("ruid") or data.get("receive_user_info", {}).get("uid")
                owner = conn.execute("SELECT uid FROM liver WHERE room_id=?", (int(room),)).fetchone()
                if receiver and owner and int(receiver) != owner[0]:
                    status = "other_receiver"
                elif status == "review":
                    # A later batch can begin before the previous summary arrives.
                    # Record a prefix match explicitly, not as an exact window match.
                    prefix_count = prefix_coin = 0
                    for row in rows:
                        prefix_count += row[1] or 0
                        prefix_coin += row[2] or 0
                        if (prefix_count, prefix_coin) == (count, coin):
                            status = "matched_prefix"
                            break
                results.append(dict(status=status, file=str(path), line=number, room_id=int(room), cmd=cmd,
                                    uid=uid, uname=data.get("uname"), gift=gift, start=start, end=end,
                                    receiver_uid=receiver,
                                    expected_count=count, expected_coin=coin,
                                    recorded_count=actual_count, recorded_coin=actual_coin,
                                    event_ids=[row[0] for row in rows]))
    return dict(summary=dict(Counter(row["status"] for row in results)), items=results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/libot.db")
    parser.add_argument("--output", type=Path, default=ROOT / "data/combo_gift_audit.json")
    args = parser.parse_args()
    report = audit(args.database, sorted((ROOT / "logs").glob("monitor.log*")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(args.output)


if __name__ == "__main__":
    main()
