"""喜报、悲报和卖掉了的有界静态图片渲染。"""

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from src.common.utils import ROOT

ASSETS = ROOT / 'resources' / 'meme_templates'
FONT = ROOT / 'fonts' / 'NotoSansCJKsc-Regular.otf'


def _png(image):
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def render_news(text: str, *, good: bool) -> bytes:
    text = text.strip()
    if not text:
        raise ValueError('请填写内容')
    if len(text) > 200:
        raise ValueError('内容过长，最多200个字符')
    name = 'good_news.jpg' if good else 'bad_news.png'
    with Image.open(ASSETS / name) as source:
        image = source.convert('RGB')
    draw = ImageDraw.Draw(image)
    width, height = image.width - 100, image.height - 200
    for size in range(60, 29, -2):
        font = ImageFont.truetype(str(FONT), size)
        stroke = max(1, size // 15)
        lines = []
        for paragraph in text.split('\n'):
            line = ''
            for char in paragraph:
                if line and draw.textlength(line + char, font=font) > width - 2 * stroke:
                    lines.append(line)
                    line = ''
                line += char
            lines.append(line)
        wrapped = '\n'.join(lines)
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=8, stroke_width=stroke)
        if box[2] - box[0] <= width and box[3] - box[1] <= height:
            x = (image.width - (box[2] - box[0])) / 2 - box[0]
            y = (image.height - (box[3] - box[1])) / 2 - box[1]
            draw.multiline_text((x, y), wrapped, font=font, spacing=8, align='center',
                                fill=(238, 0, 0) if good else 'black', stroke_width=stroke,
                                stroke_fill=(255, 255, 153) if good else 'white')
            return _png(image)
    raise ValueError('内容放不下，请缩短文字或减少换行')


def render_sold_out(path) -> bytes:
    with Image.open(path) as source:
        frame = source.convert('RGBA')
    factor = min(600 / min(frame.size), 1600 / max(frame.size))
    frame = frame.resize(tuple(max(1, round(v * factor)) for v in frame.size), Image.Resampling.LANCZOS)
    frame = Image.alpha_composite(frame, Image.new('RGBA', frame.size, (0, 0, 0, 64)))
    with Image.open(ASSETS / 'sold_out.png') as source:
        icon = source.convert('RGBA')
    icon.thumbnail(frame.size, Image.Resampling.LANCZOS)
    frame.alpha_composite(icon, ((frame.width - icon.width) // 2, (frame.height - icon.height) // 2))
    return _png(frame.convert('RGB'))
