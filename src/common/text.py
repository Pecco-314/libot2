from __future__ import annotations

import re
import unicodedata

import emoji

_TEXT_UNIT_PATTERN = re.compile(
    r"[a-zA-Z0-9]+|[^\S\r\n]+|\r\n|\r|\n|[^\x00-\xff]|."
)


def split_text_units(value: str) -> list[str]:
    """按英文单词、空白、普通字符和完整 Emoji 序列拆分文本。"""
    text = str(value)
    result: list[str] = []
    cursor = 0

    for match in emoji.emoji_list(text):
        start = match["match_start"]
        end = match["match_end"]
        result.extend(_TEXT_UNIT_PATTERN.findall(text[cursor:start]))
        result.append(match["emoji"])
        cursor = end

    result.extend(_TEXT_UNIT_PATTERN.findall(text[cursor:]))
    return result


def text_unit_width(value: str) -> int:
    """返回适合名称截断的终端式显示宽度。"""
    if emoji.is_emoji(value):
        return 2

    return sum(
        2 if unicodedata.east_asian_width(char) in "WFA" else 1
        for char in value
    )
