"""歌曲查询的演唱次数参数。"""
import re


def parse_song_query(text: str) -> tuple[str, int | None]:
    text = text.strip()
    count = 5
    if text.startswith('#'):
        match = re.fullmatch(r'#([0-9]+|max)\s+(.+)', text, re.I | re.S)
        if not match:
            raise ValueError('用法：查歌曲#10 <歌名> 或 查歌曲#max <歌名>')
        token, text = match.groups()
        try:
            count = None if token.lower() == 'max' else int(token)
        except ValueError:
            raise ValueError('次数必须为正整数或 max') from None
        if count is not None and count < 1:
            raise ValueError('次数必须为正整数或 max')
    if not text.strip():
        raise ValueError('用法：查歌曲 <歌名>；查歌曲#10 <歌名>；查歌曲#max <歌名>')
    return text.strip(), count
