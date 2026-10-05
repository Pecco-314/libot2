from __future__ import annotations

from typing import Any


def format_price(value: int | None, *, is_thousandth: bool) -> str:
    if value is None:
        return ""
    price = value / 1000 if is_thousandth else value
    if price == int(price):
        return f"￥{int(price)}"
    return f"￥{price:.2f}"


def format_event_text(event: dict[str, Any]) -> tuple[str, tuple[int, int, int], str | None]:
    cmd = str(event.get("cmd") or "")
    content = event.get("content")
    gift_name = event.get("gift_name")
    gift_num = event.get("gift_num")
    total_coin = event.get("total_coin")
    title = event.get("title")
    default_color = (80, 80, 80)
    merge_count = int(event.get("merge_count") or 1)
    merge_amount = event.get("merge_amount")
    suffix = f"（{merge_count}）" if merge_count > 1 else None

    if cmd == "DANMU_MSG":
        return str(content or ""), default_color, suffix
    if cmd == "SEND_GIFT":
        name = str(gift_name or "礼物")
        num = int(gift_num or 1)
        price_value = merge_amount if merge_amount is not None else total_coin
        price = format_price(price_value, is_thousandth=True)
        price_text = f"（{price}）" if price else ""
        return f"送出了{name}*{num}{price_text}", (199, 52, 122), None
    if cmd == "GUARD_BUY":
        name = str(gift_name or "大航海")
        num = int(gift_num or 1)
        num_text = f"*{num}" if num > 1 else ""
        return f"开通了{name}{num_text}", (230, 126, 34), suffix
    if cmd == "SUPER_CHAT_MESSAGE":
        price = format_price(total_coin, is_thousandth=False)
        text_suffix = f"（{price}）：{content}" if price else f"：{content}" if content else ""
        return f"醒目留言{text_suffix}", (26, 78, 140), suffix
    if cmd == "LIVE":
        return "开播", default_color, suffix
    if cmd == "PREPARING":
        return "下播", default_color, suffix
    if cmd == "ROOM_CHANGE":
        return (f"改标题：{title}" if title else "改标题"), default_color, suffix
    if cmd == "ONLINE_RANK_COUNT":
        return (f"同接 {content}" if content is not None else "同接"), default_color, suffix

    return str(content or cmd), default_color, suffix
