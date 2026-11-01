"""歌词与ASR共用的搜索视图；原始文字保持不变。"""
import re
import unicodedata

_TAGS = re.compile(r'<\|.*?\|>|\[[^\]\n]*\]')
_CREDITS = re.compile(
    r'^\s*(?:作词|作曲|编曲|填词|词|曲|制作人?|监制|混音|母带|录音|演唱|出品|'
    r'lyrics?|composer|arranger|producer|vocal|mixing|mastering|copyright)'
    r'(?:\s+[A-Za-z ]+)?\s*[:：]', re.I)


def search_text(text: str, *, compact: bool = True) -> str:
    """NFKC和大小写归一；紧凑视图跨越ASR不稳定的空白与标点。"""
    text = _TAGS.sub('', unicodedata.normalize('NFKC', text or '')).casefold()
    if compact:
        return ''.join(c for c in text if c.isalnum())
    return ' '.join(re.sub(r'[^\w]+', ' ', text).replace('_', ' ').split())


def lyric_lines(text: str) -> list[str]:
    """保留歌词行，跳过明确的制作人员标签；单行旧数据仅安全归一化。"""
    lines = []
    for line in (text or '').splitlines():
        line = _TAGS.sub('', line).strip()
        if line and not (_CREDITS.match(line) and ('\n' in text or len(line) < 120)):
            lines.append(line)
    return lines
