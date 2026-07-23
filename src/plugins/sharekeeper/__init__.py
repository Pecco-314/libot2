from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from nonebot import get_driver, logger, on_message
from nonebot.plugin import PluginMetadata
from redis.asyncio import Redis
from redis.exceptions import WatchError

try:
    from nonebot import get_plugin_config
except ImportError:  # pragma: no cover
    get_plugin_config = None

from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="B站重复分享记录",
    description="记录指定群内的 B 站视频分享，并在重复分享时进行引用提醒。",
    usage="配置 Redis 后加载插件即可。",
    config=Config,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
B23_URL_RE = re.compile(r"(?:https?://)?b23\.tv/[0-9A-Za-z]+(?:\?[^\s\"'\]]*)?")
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

driver = get_driver()
share_matcher = on_message(priority=15, block=False)
_redis_client: Optional[Redis] = None


def _load_config() -> Config:
    if get_plugin_config is not None:
        return get_plugin_config(Config)

    driver_config = get_driver().config
    if hasattr(driver_config, "model_dump"):
        raw_config = driver_config.model_dump()
    elif hasattr(driver_config, "dict"):
        raw_config = driver_config.dict()
    else:
        raw_config = dict(driver_config)

    if hasattr(Config, "model_validate"):
        return Config.model_validate(raw_config)
    return Config(**raw_config)


plugin_config = _load_config()


@driver.on_shutdown
async def _close_redis() -> None:
    global _redis_client
    if _redis_client is None:
        return

    try:
        await _redis_client.aclose()
    except AttributeError:  # pragma: no cover
        await _redis_client.close()
    finally:
        _redis_client = None


async def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            plugin_config.bilibili_share_keeper_redis_url,
            decode_responses=True,
        )
    return _redis_client


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return

    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
        return

    if isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    raw_text = raw_text.strip()
    if not raw_text:
        return None

    candidates = [raw_text]
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(raw_text[start : end + 1])

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        if isinstance(loaded, dict):
            return loaded
    return None


def _collect_payloads(message: Message) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []

    for segment in message:
        if getattr(segment, "type", "") != "json":
            continue

        value = segment.data.get("data")
        if not isinstance(value, str):
            continue
        payload = _extract_json_object(value)
        if payload:
            payloads.append(payload)

    return payloads


def _normalize_url(url: str) -> str:
    normalized = url.strip().strip('"').strip("'")
    if normalized.startswith("//"):
        return "https:" + normalized
    if normalized.startswith("http://") or normalized.startswith("https://"):
        return normalized
    return "https://" + normalized.lstrip("/")


def _extract_b23_urls(payload: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    seen = set()

    for source in _iter_strings(payload):
        for match in B23_URL_RE.findall(source):
            normalized = _normalize_url(match)
            if normalized in seen:
                continue
            seen.add(normalized)
            found.append(normalized)

    return found


async def _resolve_url(short_url: str) -> str:
    timeout = plugin_config.bilibili_share_keeper_http_timeout
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
    ) as client:
        response = await client.get(short_url)
        
        if response.is_redirect and "location" in response.headers:
            return response.headers["location"]
            
        return str(response.url)


def _extract_bv(*texts: str) -> Optional[str]:
    for text in texts:
        match = BV_RE.search(text)
        if match:
            return match.group(0)
    return None


def _display_name(event: GroupMessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender is not None:
        card = getattr(sender, "card", "") or ""
        nickname = getattr(sender, "nickname", "") or ""
        if card:
            return str(card)
        if nickname:
            return str(nickname)
    return str(event.user_id)


def _redis_key(group_id: int, bv: str) -> str:
    prefix = plugin_config.bilibili_share_keeper_key_prefix.rstrip(":")
    return f"{prefix}:group:{group_id}:bv:{bv}"


async def _register_share(
    redis: Redis,
    *,
    group_id: int,
    message_id: int,
    user_id: int,
    user_name: str,
    bv: str,
    short_url: str,
    resolved_url: str,
) -> Tuple[bool, Dict[str, str]]:
    key = _redis_key(group_id, bv)
    first_record = {
        "bv": bv,
        "first_message_id": str(message_id),
        "first_user_id": str(user_id),
        "first_user_name": user_name,
        "first_share_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "first_short_url": short_url,
        "first_resolved_url": resolved_url,
        "count": "1",
    }

    while True:
        try:
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                existing = await pipe.hgetall(key)
                if not existing:
                    pipe.multi()
                    pipe.hset(key, mapping=first_record)
                    await pipe.execute()
                    return False, first_record

                pipe.multi()
                pipe.hsetnx(key, "count", existing.get("count") or "1")
                pipe.hincrby(key, "count", 1)
                result = await pipe.execute()
                existing["count"] = str(result[-1])
                return True, existing
        except WatchError:
            continue


@share_matcher.handle()
async def handle_group_bilibili_share(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return

    if event.group_id != plugin_config.bilibili_share_keeper_target_group:
        return

    payloads = _collect_payloads(event.message)
    if not payloads:
        return

    for payload in payloads:
        short_urls = _extract_b23_urls(payload)
        if not short_urls:
            continue

        short_url = short_urls[0]
        try:
            resolved_url = await _resolve_url(short_url)
        except Exception as exc:
            logger.warning(f"解析 b23.tv 失败 {short_url}: {exc!r}")
            continue

        bv = _extract_bv(
            resolved_url,
            short_url,
            json.dumps(payload, ensure_ascii=False),
        )
        if not bv:
            logger.debug(f"未能从分享消息中提取 BV 号，short_url={short_url}")
            continue

        redis = await _get_redis()
        duplicate, record = await _register_share(
            redis,
            group_id=event.group_id,
            message_id=event.message_id,
            user_id=event.user_id,
            user_name=_display_name(event),
            bv=bv,
            short_url=short_url,
            resolved_url=resolved_url,
        )
        if not duplicate:
            return

        first_time = record.get("first_share_time", "未知时间")
        first_user_name = record.get("first_user_name", "未知群友")
        count = record.get("count", "2")

        reply_message = Message()
        reply_message.append(MessageSegment.reply(event.message_id))
        reply_message += (
            f"此视频已于{first_time}被群友{first_user_name}分享过，"
            f"这是第{count}次被分享"
        )
        await bot.send(event, reply_message)
        return
