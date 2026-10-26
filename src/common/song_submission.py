"""提交歌单的严格文本格式。"""

from datetime import date
import re


def parse_song_submission(text):
    if '?' in text or '？' in text:
        raise ValueError('歌单含问号，请先补全所有歌曲')
    lines = text.strip().splitlines()
    if len(lines) < 2 or not re.fullmatch(r'\[\d{4}-\d{2}-\d{2}\]', lines[0]):
        raise ValueError('首行须为[YYYY-MM-DD]，其后每行一首歌曲')
    record_date = lines[0][1:-1]
    try:
        date.fromisoformat(record_date)
    except ValueError:
        raise ValueError('歌单日期无效') from None
    if len(lines) > 501:
        raise ValueError('单次最多提交500条演唱记录')
    entries = []
    for number, line in enumerate(lines[1:], 2):
        match = re.fullmatch(r'([1-9][0-9]*) # (.+ - .*)', line)
        if not match:
            raise ValueError(f'第{number}行格式错误，须为 ID # 歌名 - 歌手')
        entries.append((int(match[1]), match[2]))
    return record_date, entries
