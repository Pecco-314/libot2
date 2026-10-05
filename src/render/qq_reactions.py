"""表情回应榜：单张图显示群友总次数与各自最常用的十种表情。"""
import uuid

from PIL import Image, ImageDraw
from nonebot_plugin_imageutils import BuildImage

from src.common.qq_reactions import emoji_label, top_emojis
from src.common.qq_emoji_assets import cache_path
from src.common.utils import ROOT
from src.render.emoji_text import Text2Image


def text_image(text, size, color=(50, 50, 50), width=788, bold=False):
    return Text2Image.from_text(text, size, fill=color, weight="bold" if bold else "normal",
                               fontname=str(ROOT / "fonts" / "NotoSansCJKsc-Regular.otf"),
                               fallback_fonts=[str(p) for p in (ROOT / "fonts").glob("*.ttf")]).wrap(width)


def emoji_chip(eid, count):
    number = text_image(f"×{count}", 20, (105, 105, 105))
    path = cache_path(eid)
    icon = None
    if path.is_file():
        try:
            with Image.open(path) as source:
                icon = source.convert("RGBA")
            icon.thumbnail((32, 32), Image.Resampling.LANCZOS)
        except OSError:
            pass
    fallback = text_image(emoji_label(eid), 28 if int(eid) > 1000 else 19) if icon is None else None
    icon_w = 32 if icon is not None else fallback.width
    canvas = BuildImage.new("RGBA", (int(icon_w + 5 + number.width + 16), 40), (255, 255, 255, 0))
    if icon is not None:
        canvas.image.alpha_composite(icon, ((32-icon.width)//2, (40-icon.height)//2))
    else:
        fallback.draw_on_image(canvas.image, (0, (40-fallback.height)//2))
    number.draw_on_image(canvas.image, (icon_w + 5, (40-number.height)//2))
    return canvas.image


def row_image(rank, item, width):
    name = item["name"] if item["name"] != str(item["uid"]) else "未知昵称"
    name = " ".join(name.split())[:50]
    name = "".join(c for c in name if c not in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
    heading = text_image(f"{rank}. {name}    {item['count']}次", 25, width=width, bold=True)
    chips, x, y = [], 0, 0
    for eid, count in top_emojis(item["emoji"]):
        chip = emoji_chip(eid, count)
        if x and x + chip.width > width:
            x, y = 0, y + 40
        chips.append((chip, x, y))
        x += chip.width
    detail_y = int(heading.height + 10)
    height = detail_y + y + 40 + 22
    canvas = BuildImage.new("RGBA", (width, height), "white")
    heading.draw_on_image(canvas.image, (0, 0))
    for chip, x, y in chips:
        canvas.image.alpha_composite(chip, (x, detail_y+y))
    ImageDraw.Draw(canvas.image).line((0, height-10, width, height-10), fill=(238, 238, 238))
    return canvas.image


def render_rank(result, received, period, output_dir=None):
    output_dir = output_dir or ROOT / "data" / "images" / "qq_reactions"
    output_dir.mkdir(parents=True, exist_ok=True)
    title = "被贴表情榜" if received else "贴表情榜"
    padding, content_width, gutter = 36, 788, 32
    rows = [row_image(rank, item, content_width) for rank, item in enumerate(result["items"], 1)]
    # 常规榜单合为一张，较大的自定义榜单按图片高度拆页。
    pages, page, height = [], [], 0
    for row in rows:
        if page and height + row.height > 12000:
            pages.append(page)
            page, height = [], 0
        page.append(row)
        height += row.height
    if page:
        pages.append(page)
    paths = []
    for page_no, rows in enumerate(pages, 1):
        # 保持名次顺序，按内容高度均衡两列。
        split = min(range(1, len(rows)), key=lambda i: abs(
            sum(r.height for r in rows[:i]) - sum(r.height for r in rows[i:])
        )) if len(rows)>10 else len(rows)
        columns = [rows[:split], rows[split:]] if len(rows)>10 else [rows]
        width = padding*2 + content_width*len(columns) + gutter*(len(columns)-1)
        header = text_image(title, 34, bold=True)
        suffix = f"  ·  {page_no}/{len(pages)}" if len(pages)>1 else ""
        subtitle = text_image(period+suffix, 19, (130, 130, 130))
        body_height = max(sum(r.height for r in column) for column in columns)
        height = int(padding*2 + header.height + subtitle.height + 40 + body_height)
        canvas = BuildImage.new("RGBA", (width, height), "white")
        y = padding
        for text, gap in [(header, 12), (subtitle, 28)]:
            text.draw_on_image(canvas.image, (padding, y))
            y += int(text.height + gap)
        body_y = y
        for index, column in enumerate(columns):
            y = body_y
            for row in column:
                canvas.image.alpha_composite(row, (padding+index*(content_width+gutter), y))
                y += row.height
        path = output_dir / f"{uuid.uuid4().hex}.png"
        canvas.image.convert("RGB").save(path)
        paths.append(path)
    return paths
