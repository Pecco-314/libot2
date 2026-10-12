"""QQ公开头像的地址与本地图片缓存。"""

import asyncio
import time
import uuid
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from src.common.utils import ROOT

AVATAR_DIR = ROOT / "data" / "assets" / "qq_avatars"
_failed = {}


def avatar_url(qq):
    return f"https://q1.qlogo.cn/g?b=qq&nk={int(qq)}&s=100"


def _save_avatar(data, path):
    with Image.open(BytesIO(data)) as source:
        if source.width > 2048 or source.height > 2048:
            raise ValueError("头像尺寸过大")
        image = source.convert("RGB")
        image.thumbnail((160, 160))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_avatars(qqs, directory=AVATAR_DIR):
    directory = Path(directory)
    qqs = {int(qq) for qq in qqs if str(qq).isdigit() and int(qq) > 0}
    slots = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=4, follow_redirects=True, trust_env=False) as client:

        async def fetch(qq):
            path = directory / f"{qq}.png"
            if path.is_file() and time.time() - path.stat().st_mtime < 7 * 86400:
                return
            if time.monotonic() - _failed.get(qq, -4000) < 3600:
                return
            async with slots:
                try:
                    async with client.stream("GET", avatar_url(qq)) as response:
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 1024 * 1024:
                                raise ValueError("头像文件过大")
                    await asyncio.to_thread(_save_avatar, data, path)
                except (httpx.HTTPError, OSError, ValueError):
                    _failed[qq] = time.monotonic()

        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(fetch(qq) for qq in qqs))
        except TimeoutError:
            pass
    return {
        qq: directory / f"{qq}.png" for qq in qqs if (directory / f"{qq}.png").is_file()
    }
