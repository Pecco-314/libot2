"""长文本无损分片；优先在换行处拆分，单行超限时按字符拆分。"""

DEFAULT_THRESHOLD = 300
DEFAULT_MAX_LINES = 10


def needs_forward(text: str, threshold: int = DEFAULT_THRESHOLD, max_lines: int = DEFAULT_MAX_LINES) -> bool:
    return len(text) > threshold or len(text.splitlines()) > max_lines


def split_text(text: str, limit: int = 1500) -> list[str]:
    if limit < 1:
        raise ValueError("分片长度必须大于0")
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks
