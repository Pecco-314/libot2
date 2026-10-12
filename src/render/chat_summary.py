"""群聊总结分页卡片：主题正文与参与讨论者头像。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from src.common.utils import ROOT
from src.render.emoji_text import Text2Image

WIDTH = 1000
PADDING = 44
CARD_WIDTH = WIDTH - PADDING * 2
MAX_BODY_HEIGHT = 1420
VERSION = 3


def text_image(text, size, *, width=CARD_WIDTH - 64, bold=False, color="#354158"):
    return Text2Image.from_text(
        str(text),
        size,
        fill=color,
        spacing=8,
        weight="bold" if bold else "normal",
        fontname=str(ROOT / "fonts" / "NotoSansCJKsc-Regular.otf"),
        fallback_fonts=[str(p) for p in (ROOT / "fonts").glob("*.ttf")],
    ).wrap(width)


def avatar_icon(path, size=44):
    icon = Image.new("RGBA", (size, size), "#e6eaf3")
    try:
        if path:
            with Image.open(path) as source:
                icon = ImageOps.fit(
                    source.convert("RGBA"),
                    (size, size),
                    method=Image.Resampling.LANCZOS,
                )
        else:
            draw = ImageDraw.Draw(icon)
            draw.ellipse(
                (size * 0.35, size * 0.18, size * 0.65, size * 0.49), fill="#99a5bb"
            )
            draw.ellipse(
                (size * 0.19, size * 0.53, size * 0.81, size * 1.12), fill="#99a5bb"
            )
    except (OSError, ValueError):
        return avatar_icon(None, size)
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    icon.putalpha(mask)
    return icon


def _display_text(value, participants):
    text = str(value)
    for person in participants:
        qq = str(person["qq"])
        if len(qq) >= 5:
            name = person.get("name") or "群友"
            text = text.replace(qq, name if str(name) != qq else "群友")
    return text


def topic_cards(topic, avatars):
    participants = topic.get("participants", [])
    title = text_image(
        _display_text(topic["title"], participants), 29, bold=True, color="#28314c"
    )
    body_lines = text_image(
        ((topic['batch_period'] + '\n') if topic.get('batch_period') else '')
        + _display_text(topic["summary"], participants), 26
    ).wrapped_text.splitlines() or [""]
    # 长话题按正文行拆开，每个片段保留标题和参与人，避免超长单图。
    for offset in range(0, len(body_lines), 18):
        body = text_image("\n".join(body_lines[offset : offset + 18]), 26)
        columns = (CARD_WIDTH - 64) // 54
        avatar_height = (
            math.ceil(len(participants) / columns) * 54 if participants else 0
        )
        height = int(
            32
            + title.height
            + 18
            + body.height
            + (24 + avatar_height if participants else 0)
            + 28
        )
        card = Image.new("RGBA", (CARD_WIDTH, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        draw.rounded_rectangle(
            (0, 0, CARD_WIDTH - 1, height - 1),
            radius=22,
            fill="white",
            outline="#e7eaf2",
            width=1,
        )
        draw.rounded_rectangle(
            (16, 32, 20, 32 + title.height), radius=2, fill="#929cce"
        )
        title.draw_on_image(card, (32, 32))
        y = 32 + title.height + 18
        body.draw_on_image(card, (32, y))
        y += body.height + 24
        for index, person in enumerate(participants):
            icon = avatar_icon(avatars.get(int(person["qq"])))
            card.alpha_composite(
                icon, (32 + (index % columns) * 54, int(y) + (index // columns) * 54)
            )
        yield card


def render_summary(result, avatars=None, output_dir=None):
    avatars = avatars or {}
    output_dir = Path(output_dir or ROOT / "data" / "images" / "chat_summary")
    output_dir.mkdir(parents=True, exist_ok=True)
    topics = result.get("topics", [])
    displayed = {
        "period": result["period"],
        "message_count": result["message_count"],
        "raw_message_count": result.get("raw_message_count"),
        "image_message_count": result.get("image_message_count", 0),
        "topics": topics,
    }
    stamps = {
        str(qq): Path(path).stat().st_mtime_ns
        for qq, path in avatars.items()
        if Path(path).is_file()
    }
    digest = hashlib.sha256(
        json.dumps(
            [VERSION, displayed, stamps], ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()[:20]
    cards = [card for topic in topics for card in topic_cards(topic, avatars)]
    if not cards:
        cards = list(
            topic_cards(
                {"title": "没有明确的讨论主题", "summary": "这段消息以零散闲聊为主，已略过。"},
                {},
            )
        )
    pages, page, height = [], [], 0
    for card in cards:
        if page and height + card.height + 18 > MAX_BODY_HEIGHT:
            pages.append(page)
            page, height = [], 0
        page.append(card)
        height += card.height + 18
    if page:
        pages.append(page)
    paths = []
    for index, page in enumerate(pages, 1):
        path = output_dir / f"summary_{digest}_{index}.png"
        if not path.is_file():
            title = text_image("大家在聊什么", 40, bold=True, color="#28314c")
            counts = f"{result['message_count']}条文字消息"
            if 'raw_message_count' in result:
                counts = f"覆盖{result['raw_message_count']}条消息 · {result['message_count']}条文字"
            if result.get('image_message_count'):
                counts += f" · {result['image_message_count']}条含图"
            subtitle = text_image(
                f"{result['period']}  ·  {counts}",
                21,
                color="#858ca0",
            )
            header_height = int(PADDING + title.height + 14 + subtitle.height + 30)
            height = header_height + sum(card.height + 18 for card in page) + 44
            canvas = Image.new("RGBA", (WIDTH, height), "#f3f5fa")
            title.draw_on_image(canvas, (PADDING, PADDING))
            subtitle.draw_on_image(canvas, (PADDING, PADDING + title.height + 14))
            y = header_height
            for card in page:
                canvas.alpha_composite(card, (PADDING, y))
                y += card.height + 18
            footer = text_image(f"{index} / {len(pages)}", 18, color="#939aac")
            footer.draw_on_image(canvas, ((WIDTH - footer.width) // 2, height - 37))
            canvas.convert("RGB").save(path, format="PNG")
        paths.append(path)
    return paths


async def render_summary_images(result):
    import asyncio
    from src.common.qq_avatars import ensure_avatars

    avatars = await ensure_avatars(
        person["qq"]
        for topic in result.get("topics", [])
        for person in topic.get("participants", [])
    )
    return await asyncio.to_thread(render_summary, result, avatars)
