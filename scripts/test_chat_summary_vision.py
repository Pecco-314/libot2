"""用人工测试图片验证Codex按需读图，不访问真实群聊或向群发送消息。"""

import asyncio
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from src.common.chat_summary import SYSTEM_PROMPT
from src.common.chat_summary_codex import CodexClient
from src.common.chat_summary_images import ImageCatalog
from src.common.utils import load_env_file


async def main():
    load_env_file()
    with tempfile.TemporaryDirectory(prefix="libot-vision-test-") as folder:
        root = Path(folder)
        stamp = int(time.time())
        filename = f"{stamp}_991_123_0.img"
        image = Image.new("RGB", (640, 300), "white")
        ImageDraw.Draw(image).text(
            (50, 85), "21:37", fill="black",
            font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 80),
        )
        image.save(root / filename, format="PNG")
        cache = root / "cache.db"
        with sqlite3.connect(cache) as conn:
            conn.execute("CREATE TABLE message(group_id,message_id,sent_at,segments,recalled_at)")
            conn.execute("INSERT INTO message VALUES(991,123,?,?,NULL)", (
                stamp, json.dumps([{"type": "image", "file": filename}]),
            ))
        catalog = ImageCatalog({"i1": filename}, assets=root, cache_path=cache)
        payload = {
            "context": {"terms": []},
            "people": {"U1": ["测试甲", [], []], "U2": ["测试乙", [], []]},
            "messages": [
                ["m1", "18:20:00", "U1", "[图片]", {"images": ["i1"]}],
                ["m2", "18:20:02", "U1", "图里的活动时间是几点？我看不清"],
                ["m3", "18:20:04", "U2", "我也想确认图里写的时间，别按聊天消息的时间猜"],
            ],
        }
        client = CodexClient()
        try:
            with patch("src.common.chat_summary_codex.ImageCatalog", return_value=catalog):
                result, usage = await client.complete(
                    SYSTEM_PROMPT, payload, image_sources={"i1": filename},
                )
            passed = catalog.read_count > 0 and "21:37" in json.dumps(result)
            print(json.dumps({"passed": passed, "result": result, "usage": usage}, ensure_ascii=False))
            if not passed:
                raise SystemExit("未完成实际读图或图中时间识别失败")
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
