from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from src.common.utils import ROOT


MEME_IMAGE_DIR = ROOT / "data" / "images" / "memes"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_SIDE = 2048


class MemeImageError(RuntimeError):
    pass


def extract_reply_image_url(event) -> str | None:
    reply = getattr(event, "reply", None)
    message = getattr(reply, "message", None)
    if message is None:
        return None
    for segment in message:
        if getattr(segment, "type", None) != "image":
            continue
        data = getattr(segment, "data", {}) or {}
        for key in ("url", "file"):
            value = str(data.get(key) or "").strip()
            if urlsplit(value).scheme in {"http", "https"}:
                return value
    return None


def _normalize_image(data: bytes) -> bytes:
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise MemeImageError("图片超过10 MB")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if source.width <= 0 or source.height <= 0:
                raise MemeImageError("图片尺寸无效")
            image = source.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise MemeImageError("无法识别或不安全的图片") from exc

    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def save_meme_image(data: bytes) -> str:
    normalized = _normalize_image(data)
    digest = hashlib.sha256(normalized).hexdigest()
    MEME_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = MEME_IMAGE_DIR / f"{digest}.png"
    if not path.exists():
        path.write_bytes(normalized)
    return str(path.relative_to(ROOT))


async def download_meme_image(url: str) -> str:
    try:
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=True,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > MAX_DOWNLOAD_BYTES:
                    raise MemeImageError("图片超过10 MB")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise MemeImageError("图片超过10 MB")
                    chunks.append(chunk)
    except MemeImageError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise MemeImageError("图片下载失败") from exc
    return await asyncio.to_thread(save_meme_image, b"".join(chunks))


def resolve_meme_image_path(value: str) -> Path | None:
    expected_root = MEME_IMAGE_DIR.resolve()
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(expected_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
