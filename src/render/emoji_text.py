from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import emoji
from PIL import Image
from nonebot_plugin_imageutils import Text2Image as PlainText2Image

from src.common.text import split_text_units
from src.common.utils import ROOT

logger = logging.getLogger(__name__)

_EMOJI_ASSET_DIR = ROOT / "data" / "assets" / "twemoji" / "17.0.3" / "72x72"


def _twemoji_key(value: str) -> str:
    return "-".join(
        f"{ord(char):x}" for char in value if char not in {"\ufe0e", "\ufe0f"}
    )


@lru_cache(maxsize=1)
def _emoji_asset_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in _EMOJI_ASSET_DIR.glob("*.png"):
        key = "-".join(
            part for part in path.stem.split("-") if part not in {"fe0e", "fe0f"}
        )
        index[key] = path
    return index


def _read_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA")


def _unpremultiply_alpha(image: Image.Image) -> Image.Image:
    """将 Pillow 在透明底上绘制出的预乘色转换为标准 straight-alpha RGBA。"""
    pixels: list[tuple[int, int, int, int]] = []
    for red, green, blue, alpha in image.getdata():
        if alpha in {0, 255}:
            pixels.append((red, green, blue, alpha))
            continue
        pixels.append(
            (
                min(255, (red * 255 + alpha // 2) // alpha),
                min(255, (green * 255 + alpha // 2) // alpha),
                min(255, (blue * 255 + alpha // 2) // alpha),
                alpha,
            )
        )
    image.putdata(pixels)
    return image


@lru_cache(maxsize=512)
def _load_emoji_asset(value: str) -> Image.Image | None:
    """从固定版本的本地 Twemoji 资源读取彩色 Emoji。"""
    asset_path = _emoji_asset_index().get(_twemoji_key(value))
    if asset_path is None:
        logger.warning("本地 Twemoji 资源缺失：%r", value)
        return None

    try:
        return _read_image(asset_path)
    except Exception as exc:
        logger.warning("读取本地 Twemoji 资源失败 %s: %r", asset_path, exc)
        return None


async def prefetch_emoji_assets(texts: Iterable[str]) -> None:
    """兼容旧调用；完整 Twemoji 已内置，不再需要网络预取。"""
    return None


def _safe_fallback_text(value: str) -> str:
    """本地资源缺失时保留基础符号，丢弃不可见的序列控制字符。"""
    return "".join(
        char
        for char in value
        if unicodedata.category(char) != "Cf"
        and char not in {"\ufe0e", "\ufe0f"}
        and not "\U0001f3fb" <= char <= "\U0001f3ff"
        and not "\U000e0100" <= char <= "\U000e01ef"
    )


@dataclass(frozen=True)
class _Token:
    value: str
    is_emoji: bool
    width: int
    height: int


class Text2Image:
    """兼容项目常用 Text2Image API、按完整序列绘制彩色 Emoji。"""

    def __init__(
        self,
        text: str,
        fontsize: int,
        *,
        style: str = "normal",
        weight: str = "normal",
        fill="black",
        spacing: int = 4,
        align: str = "left",
        stroke_width: int = 0,
        stroke_fill=None,
        font_fallback: bool = True,
        fontname: str = "",
        fallback_fonts: tuple[str, ...] = (),
        emoji_scale_factor: float = 1.0,
    ):
        self.text = str(text)
        self.fontsize = fontsize
        self.style = style
        self.weight = weight
        self.fill = fill
        self.spacing = spacing
        self.align = align
        self.stroke_width = stroke_width
        self.stroke_fill = stroke_fill
        self.font_fallback = font_fallback
        self.fontname = fontname
        self.fallback_fonts = fallback_fonts
        self.emoji_scale_factor = emoji_scale_factor
        self._metric_cache: dict[str, tuple[int, int]] = {}
        self._max_width: float | None = None
        self._lines: list[list[_Token]] = []
        self._layout()

    @classmethod
    def from_text(
        cls,
        text: str,
        fontsize: int,
        style: str = "normal",
        weight: str = "normal",
        fill="black",
        spacing: int = 4,
        align: str = "left",
        stroke_width: int = 0,
        stroke_fill=None,
        font_fallback: bool = True,
        fontname: str = "",
        fallback_fonts: list[str] | None = None,
        emoji_scale_factor: float = 1.0,
    ) -> "Text2Image":
        return cls(
            text,
            fontsize,
            style=style,
            weight=weight,
            fill=fill,
            spacing=spacing,
            align=align,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
            font_fallback=font_fallback,
            fontname=fontname,
            fallback_fonts=tuple(fallback_fonts or ()),
            emoji_scale_factor=emoji_scale_factor,
        )

    def _plain_text(self, value: str) -> PlainText2Image:
        return PlainText2Image.from_text(
            value,
            self.fontsize,
            style=self.style,
            weight=self.weight,
            fill=self.fill,
            spacing=self.spacing,
            align=self.align,
            stroke_width=self.stroke_width,
            stroke_fill=self.stroke_fill,
            font_fallback=self.font_fallback,
            fontname=self.fontname,
            fallback_fonts=list(self.fallback_fonts),
        )

    def _plain_metrics(self, value: str) -> tuple[int, int]:
        if value not in self._metric_cache:
            rendered = self._plain_text(value)
            self._metric_cache[value] = (rendered.width, rendered.height)
        return self._metric_cache[value]

    def _tokenize(self, line: str) -> list[_Token]:
        result: list[_Token] = []
        emoji_size = max(1, round(self.fontsize * self.emoji_scale_factor))

        for value in split_text_units(line):
            if emoji.is_emoji(value):
                result.append(_Token(value, True, emoji_size, emoji_size))
            else:
                width, height = self._plain_metrics(value)
                result.append(_Token(value, False, width, height))

        return result

    def _layout(self) -> None:
        source_lines = self.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        token_lines = [self._tokenize(line) for line in source_lines]

        if not self._max_width or self._max_width <= 0:
            self._lines = token_lines
            return

        wrapped: list[list[_Token]] = []
        for tokens in token_lines:
            current: list[_Token] = []
            current_width = 0
            for token in tokens:
                if current and current_width + token.width > self._max_width:
                    wrapped.append(current)
                    current = []
                    current_width = 0
                    if not token.is_emoji and token.value.isspace():
                        continue
                current.append(token)
                current_width += token.width
            wrapped.append(current)
        self._lines = wrapped

    def wrap(self, width: float) -> "Text2Image":
        self._max_width = width
        self._layout()
        return self

    @property
    def wrapped_text(self) -> str:
        return "\n".join("".join(token.value for token in line) for line in self._lines)

    def _base_line_height(self) -> int:
        return max(1, self._plain_metrics("Ag")[1], self.fontsize)

    def _line_height(self, line: list[_Token]) -> int:
        return max([self._base_line_height(), *(token.height for token in line)])

    @property
    def width(self) -> int:
        return max((sum(token.width for token in line) for line in self._lines), default=0)

    @property
    def height(self) -> int:
        if not self._lines:
            return 0
        return sum(self._line_height(line) for line in self._lines) + self.spacing * (
            len(self._lines) - 1
        )

    def _render_plain_run(self, value: str) -> Image.Image:
        rendered = self._plain_text(value).to_image()
        if self.stroke_width == 0:
            # ImageDraw 在透明黑底上绘制抗锯齿文字时会输出预乘 RGB。
            # 直接拿该图层做 alpha 合成会再次乘 alpha，形成暗色/灰色边缘。
            straight_alpha = Image.new("RGBA", rendered.size, self.fill)
            straight_alpha.putalpha(rendered.getchannel("A"))
            return straight_alpha
        return _unpremultiply_alpha(rendered)

    def _render_emoji(self, token: _Token) -> Image.Image:
        asset = _load_emoji_asset(token.value)
        if asset is not None:
            return asset.resize((token.width, token.height), Image.Resampling.LANCZOS)

        fallback = _safe_fallback_text(token.value) or "□"
        return self._render_plain_run(fallback)

    def to_image(
        self,
        bg_color=None,
        padding: tuple[int, int] | tuple[int, int, int, int] = (0, 0),
    ) -> Image.Image:
        if len(padding) == 4:
            padding_left, padding_top, padding_right, padding_bottom = padding
        else:
            padding_left = padding_right = padding[0]
            padding_top = padding_bottom = padding[1]

        image_width = max(1, self.width + padding_left + padding_right)
        image_height = max(1, self.height + padding_top + padding_bottom)
        background = bg_color if bg_color is not None else (0, 0, 0, 0)
        image = Image.new("RGBA", (image_width, image_height), background)

        top = padding_top
        for line in self._lines:
            line_width = sum(token.width for token in line)
            line_height = self._line_height(line)
            left = padding_left
            if self.align == "center":
                left += (self.width - line_width) // 2
            elif self.align == "right":
                left += self.width - line_width

            index = 0
            while index < len(line):
                token = line[index]
                if token.is_emoji:
                    rendered = self._render_emoji(token)
                    y = top + (line_height - rendered.height) // 2
                    image.alpha_composite(rendered, (int(left), int(y)))
                    left += token.width
                    index += 1
                    continue

                run: list[_Token] = []
                while index < len(line) and not line[index].is_emoji:
                    run.append(line[index])
                    index += 1
                value = "".join(item.value for item in run)
                rendered = self._render_plain_run(value)
                y = top + (line_height - rendered.height) // 2
                if rendered.width > 0 and rendered.height > 0:
                    image.alpha_composite(rendered, (int(left), int(y)))
                left += sum(item.width for item in run)

            top += line_height + self.spacing

        return image

    def draw_on_image(self, image: Image.Image, pos: tuple[float, float]) -> None:
        target = getattr(image, "image", image)
        rendered = self.to_image()
        dest = (int(pos[0]), int(pos[1]))
        if target.mode == "RGBA":
            target.alpha_composite(rendered, dest)
        else:
            target.paste(rendered, dest, rendered)
