"""Capture raw gift signals for protocol diagnosis, without writing event data."""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import blivedm
from src.common.bilibili_auth import get_bilibili_auth
from src.monitor.monitor import _build_session


async def capture(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.output.open("a", encoding="utf-8") as output:
        class Handler(blivedm.BaseHandler):
            def handle(self, client, command):
                nonlocal count
                cmd = str(command.get("cmd", ""))
                if any(part in cmd for part in ("GIFT", "COMBO", "BLIND")):
                    output.write(json.dumps(dict(time=datetime.now().isoformat(), room_id=client.room_id,
                                                 command=command), ensure_ascii=False) + "\n")
                    output.flush()
                    count += 1
                    print(cmd, flush=True)

        async with _build_session(get_bilibili_auth().cookies) as session:
            client = blivedm.BLiveClient(args.room, session=session)
            client.set_handler(Handler())
            try:
                client.start()
                await asyncio.sleep(args.seconds)
            finally:
                await client.stop_and_close()
    print(f"Captured {count} signals: {args.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", type=int, default=1967216004)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(capture(parser.parse_args()))
