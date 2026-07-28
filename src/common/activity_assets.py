from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import mimetypes
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.common.utils import ROOT


logger = logging.getLogger("activity.assets")
DEFAULT_ASSET_DIR = ROOT / "data" / "images" / "activity_assets"
IMAGE_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
CONTENT_TYPE_SUFFIX = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://t.bilibili.com/",
}


@dataclass(frozen=True, slots=True)
class ActivityAsset:
    remote_url: str
    canonical_url: str
    local_path: str
    content_sha256: str
    content_type: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonicalize_bilibili_image_url(value: str) -> str | None:
    text = value.strip()
    if text.startswith("//"):
        text = f"https:{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"}:
        return None
    if not (hostname == "hdslb.com" or hostname.endswith(".hdslb.com")):
        return None
    if "/bfs/" not in parts.path:
        return None
    path = parts.path.split("@", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix and suffix not in IMAGE_SUFFIXES:
        return None
    return urlunsplit(("https", parts.netloc, path, "", ""))


def collect_bilibili_image_urls(value: Any) -> set[str]:
    result: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            if canonicalize_bilibili_image_url(node):
                result.add(node)

    visit(value)
    return result


def replace_activity_asset_urls(
    value: Any,
    replacements: dict[str, str],
) -> Any:
    if isinstance(value, dict):
        return {
            key: replace_activity_asset_urls(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_activity_asset_urls(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def resolve_activity_asset_path(value: str) -> Path | None:
    path = Path(value)
    if path.is_absolute():
        return path if path.is_file() else None
    if value.startswith("data/images/activity_assets/"):
        candidate = ROOT / path
        return candidate if candidate.is_file() else None
    return None


class ActivityAssetLocalizer:
    def __init__(
        self,
        asset_dir: Path = DEFAULT_ASSET_DIR,
        *,
        concurrency: int = 4,
        timeout: float = 30.0,
    ) -> None:
        self.asset_dir = asset_dir.resolve()
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=DOWNLOAD_HEADERS,
            trust_env=False,
        )
        self.cache: dict[str, ActivityAsset] = {}

    def seed_cache(self, assets: Iterable[Mapping[str, Any]]) -> int:
        seeded = 0
        for value in assets:
            remote_url = str(value.get("remote_url") or "")
            canonical_url = canonicalize_bilibili_image_url(remote_url)
            local_path = str(value.get("local_path") or "")
            if canonical_url is None or resolve_activity_asset_path(local_path) is None:
                continue
            try:
                size_bytes = int(value.get("size_bytes") or 0)
            except (TypeError, ValueError):
                continue
            self.cache[canonical_url] = ActivityAsset(
                remote_url=remote_url,
                canonical_url=canonical_url,
                local_path=local_path,
                content_sha256=str(value.get("content_sha256") or ""),
                content_type=str(value.get("content_type") or ""),
                size_bytes=size_bytes,
            )
            seeded += 1
        return seeded

    async def __aenter__(self) -> ActivityAssetLocalizer:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.client.aclose()

    @staticmethod
    def _suffix(content_type: str, canonical_url: str) -> str:
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if normalized_type in CONTENT_TYPE_SUFFIX:
            return CONTENT_TYPE_SUFFIX[normalized_type]
        suffix = Path(urlsplit(canonical_url).path).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return suffix
        guessed = mimetypes.guess_extension(normalized_type)
        return guessed if guessed in IMAGE_SUFFIXES else ".img"

    async def _download_canonical(self, canonical_url: str) -> ActivityAsset:
        cached = self.cache.get(canonical_url)
        if cached is not None:
            return cached
        async with self.semaphore:
            cached = self.cache.get(canonical_url)
            if cached is not None:
                return cached
            response = await self.client.get(canonical_url)
            response.raise_for_status()
            content = response.content
            content_type = response.headers.get("content-type", "")
            normalized_type = content_type.split(";", 1)[0].strip().lower()
            if normalized_type and not normalized_type.startswith("image/"):
                raise ValueError(
                    f"unexpected content type {content_type!r}: {canonical_url}"
                )
            digest = hashlib.sha256(content).hexdigest()
            suffix = self._suffix(content_type, canonical_url)
            target = self.asset_dir / digest[:2] / f"{digest}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                try:
                    temporary.write_bytes(content)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            try:
                local_path = target.relative_to(ROOT).as_posix()
            except ValueError:
                local_path = str(target)
            asset = ActivityAsset(
                remote_url=canonical_url,
                canonical_url=canonical_url,
                local_path=local_path,
                content_sha256=digest,
                content_type=normalized_type,
                size_bytes=len(content),
            )
            self.cache[canonical_url] = asset
            return asset

    async def localize(
        self,
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        remote_item = copy.deepcopy(item)
        urls = sorted(collect_bilibili_image_urls(remote_item))
        if not urls:
            return remote_item, [], True

        async def download(
            remote_url: str,
        ) -> tuple[str, ActivityAsset | None]:
            canonical = canonicalize_bilibili_image_url(remote_url)
            if canonical is None:
                return remote_url, None
            try:
                asset = await self._download_canonical(canonical)
            except Exception as exc:
                logger.warning(
                    "Failed to localize activity asset url=%s: %s",
                    remote_url,
                    exc,
                )
                return remote_url, None
            return remote_url, ActivityAsset(
                remote_url=remote_url,
                canonical_url=asset.canonical_url,
                local_path=asset.local_path,
                content_sha256=asset.content_sha256,
                content_type=asset.content_type,
                size_bytes=asset.size_bytes,
            )

        downloaded = await asyncio.gather(*(download(url) for url in urls))
        assets = [asset for _url, asset in downloaded if asset is not None]
        replacements = {
            remote_url: asset.local_path
            for remote_url, asset in downloaded
            if asset is not None
        }
        localized = replace_activity_asset_urls(remote_item, replacements)
        return (
            localized,
            [asset.to_dict() for asset in assets],
            len(assets) == len(urls),
        )


__all__ = [
    "ActivityAsset",
    "ActivityAssetLocalizer",
    "DEFAULT_ASSET_DIR",
    "canonicalize_bilibili_image_url",
    "collect_bilibili_image_urls",
    "replace_activity_asset_urls",
    "resolve_activity_asset_path",
]
