"""预览或执行一轮三理跨直播间动态采集；首次入库不转发历史记录。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.spider.danmakus_user import fetch_user_events
from src.spider.jobs.user_activity import collect_user_activity


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="保存采集结果及待投递记录")
    parser.add_argument("--full", action="store_true", help="遍历全部历史场次")
    args = parser.parse_args()
    if args.apply:
        print(json.dumps(await collect_user_activity(full=args.full), ensure_ascii=False))
    else:
        events, pages = await fetch_user_events(since_ms=None if args.full else (int(time.time()) - 7 * 86400) * 1000)
        print(json.dumps({"pages": pages, "events": len(events), "types": Counter(e["cmd"] for e in events),
                          "examples": [{k: v for k, v in e.items() if k != "raw_json"} for e in events[-10:]]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
