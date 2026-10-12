"""QQ 原版表情资源的本地缓存，下载源为 Koishi QFace 公共资源库。"""
import asyncio
from functools import lru_cache
from io import BytesIO
import json
import logging
from pathlib import Path
import time
import uuid

import httpx
from PIL import Image

from src.common.utils import ROOT

ASSETS = ROOT / "data" / "assets" / "qq_emoji"
logger = logging.getLogger(__name__)
_downloads = asyncio.Semaphore(4)
_failed = {}


@lru_cache(maxsize=1)
def asset_index():
    return json.loads(Path(__file__).with_name("qq_emoji_assets.json").read_text())


def cache_path(emoji_id):
    if not str(emoji_id).isdigit():
        raise ValueError("Invalid emoji ID")
    return ASSETS / f"{emoji_id}.png"


def _save_image(data, path):
    with Image.open(BytesIO(data)) as source:
        if source.width > 1024 or source.height > 1024:
            raise ValueError("emoji image too large")
        img = source.convert("RGBA")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        img.save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_assets(emoji_ids):
    index = asset_index()
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, trust_env=False) as client:
        async def fetch(eid):
            eid = str(eid)
            if eid not in index or cache_path(eid).is_file() or time.monotonic() - _failed.get(eid, -4000) < 3600:
                return
            async with _downloads:
                if cache_path(eid).is_file():
                    return
                asset = index[eid]
                if asset.get("url"):
                    urls = [asset["url"]]
                else:
                    path = asset["path"]
                    urls = [f"https://koishi.js.org/QFace/{path}",
                            f"https://raw.githubusercontent.com/koishijs/QFace/master/public/{path}"]
                for url in urls:
                    try:
                        async with client.stream("GET", url) as response:
                            response.raise_for_status()
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 1024 * 1024:
                                    raise ValueError("emoji exceeds 1 MiB")
                        await asyncio.to_thread(_save_image, data, cache_path(eid))
                        return
                    except Exception as exc:
                        logger.debug("QQ 表情下载失败 id=%s error=%s", eid, type(exc).__name__)
                _failed[eid] = time.monotonic()
        await asyncio.gather(*(fetch(eid) for eid in set(emoji_ids)))
