"""Danmakus 公开用户记录，按场次分页并保留原始事件。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

USER_API = "https://ukamnads.icu/api/v2/user"
MIT3URI_UID = 2030198123
MIT3URI_ROOM = 1967216004
EVENT_TYPES = {0: "DANMU_MSG", 1: "SEND_GIFT", 2: "GUARD_BUY", 3: "SUPER_CHAT_MESSAGE", 4: "INTERACT_WORD"}
_request_lock = asyncio.Lock()


def normalize_event(raw: dict, channel: dict, live: dict, uid: int) -> dict[str, Any]:
    if int(raw["uId"]) != uid:
        raise ValueError("Danmakus 返回了其他用户的记录")
    kind = int(raw["type"])
    room_id = int(channel["roomId"])
    timestamp_ms = int(raw["sendDate"])
    if room_id <= 0 or timestamp_ms < 1_000_000_000_000:
        raise ValueError("Danmakus 房间号或毫秒时间戳无效")
    content = str(raw.get("message") or "")
    count = int(raw.get("count") or 1) if kind in (1, 2) else None
    if count is not None and count <= 0:
        raise ValueError("Danmakus 礼物数量无效")
    # price 原样存档。礼物的单价/总价口径未经确认时不展示金额；SC 使用元。
    amount = float(Decimal(str(raw["price"]))) if kind == 3 and raw.get("price") is not None else None
    # liveId 可因场次合并变化，uploadAccountId 只是采集者，价格也可能补全。
    identity = [uid, room_id, timestamp_ms, kind, content, count]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {
        "event_key": key, "uid": uid, "uname": str(raw.get("uName") or uid),
        "room_id": room_id, "room_name": str(channel.get("uName") or room_id),
        "live_id": str(live.get("liveId") or ""), "timestamp_ms": timestamp_ms,
        "timestamp": timestamp_ms // 1000, "cmd": EVENT_TYPES.get(kind, f"DANMAKUS_{kind}"),
        "content": content if kind not in (1, 2) else None,
        "gift_name": content if kind in (1, 2) else None,
        "gift_num": count, "total_coin": amount,
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True),
    }


async def fetch_user_events(
    uid: int = MIT3URI_UID, *, since_ms: int | None = None,
    page_size: int = 50, max_pages: int = 100, delay: float = 1.0,
    client: httpx.AsyncClient | None = None,
    stop_after_page: Callable[[list[dict]], Awaitable[bool]] | None = None,
) -> tuple[list[dict], int]:
    """完整补查传 since_ms=None；高频轮询通过回调在已知页停止。

    任何分页失败都不返回半份结果，调用方不得推进抓取状态。
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as owned:
            return await fetch_user_events(uid, since_ms=since_ms, page_size=page_size,
                                           max_pages=max_pages, delay=delay, client=owned,
                                           stop_after_page=stop_after_page)
    events: dict[str, dict] = {}
    previous_starts: int | None = None
    page_signatures: set[tuple] = set()
    for page_num in range(max_pages):
        if page_num:
            await asyncio.sleep(delay)
        # 深度补查在分页间释放请求锁，让高频第一页查询可以穿插执行。
        async with _request_lock:
            response = await client.get(USER_API, params={"uid": uid, "pageNum": page_num, "pageSize": page_size},
                                        headers={"Origin": "https://danmakus.com", "Referer": "https://danmakus.com/"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise ValueError(f"Danmakus 用户接口返回错误: {payload.get('code')}")
        page = payload["data"]
        if page.get("pageNum") != page_num or page.get("pageSize") != page_size:
            raise ValueError("Danmakus 分页参数未生效")
        records = page["data"]["records"]
        if not isinstance(records, list) or not isinstance(page.get("hasMore"), bool):
            raise ValueError("Danmakus 分页响应格式错误")
        signature = tuple(row["live"]["liveId"] for row in records)
        if signature and signature in page_signatures:
            raise ValueError("Danmakus 返回重复分页")
        page_signatures.add(signature)
        page_events = []
        for record in records:
            channel, live = record["channel"], record["live"]
            start = int(live["startDate"])
            if previous_starts is not None and start > previous_starts:
                raise ValueError("Danmakus 场次顺序发生变化，等待下次重查")
            previous_starts = start
            for raw in record["danmakus"]:
                event = normalize_event(raw, channel, live, uid)
                events[event["event_key"]] = event
                page_events.append(event)
        if not page["hasMore"]:
            return sorted(events.values(), key=lambda e: (e["timestamp_ms"], e["event_key"])), page_num + 1
        if not records:
            raise ValueError("Danmakus 非末页为空")
        if stop_after_page is not None and await stop_after_page(page_events):
            return sorted(events.values(), key=lambda e: (e["timestamp_ms"], e["event_key"])), page_num + 1
        if since_ms is not None and previous_starts is not None and previous_starts < since_ms:
            return sorted(events.values(), key=lambda e: (e["timestamp_ms"], e["event_key"])), page_num + 1
    raise ValueError("Danmakus 超出分页安全上限，未推进抓取状态")
