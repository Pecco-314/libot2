import re
from datetime import datetime
from typing import Dict, Any, Generator, Iterable, Optional

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# 正则定义
CUSTOM_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}),\d{3}\s+-\s+(.*?)\s+-\s+([A-Z]+)\s+-\s+(.*)$')
NONEBOT_RE = re.compile(r'^(\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+\[(.*?)\]\s+nonebot\s*\|\s*(.*)$')
NAPCAT_RE = re.compile(r'^(\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+\[(.*?)\]\s+(.*?)\s*\|\s*(.*)$')

def clean_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub('', text)

def format_timestamp(ts_str: str) -> str:
    """统一日期格式"""
    now = datetime.now()
    try:
        if len(ts_str) > 15 and ts_str[4] == '-':
            dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        else:
            dt = datetime.strptime(ts_str, '%m-%d %H:%M:%S')
        return dt.strftime('%m-%d %H:%M:%S')
    except:
        return ts_str

class LogStreamParser:
    def __init__(self):
        self.current_log: Optional[Dict[str, Any]] = None

    def feed(self, raw_line: str) -> Optional[Dict[str, Any]]:
        line = clean_ansi(raw_line).rstrip('\r\n')
        if not line: return None

        # 尝试匹配并规范化日期
        match = CUSTOM_RE.match(line)
        if match:
            return self._flush_and_start_new(format_timestamp(match.group(1)), match.group(2), match.group(3), match.group(4), "custom")

        match = NONEBOT_RE.match(line)
        if match:
            return self._flush_and_start_new(format_timestamp(match.group(1)), "nonebot", match.group(2).upper(), match.group(3), "nonebot")

        match = NAPCAT_RE.match(line)
        if match:
            return self._flush_and_start_new(format_timestamp(match.group(1)), match.group(3), match.group(2).upper(), match.group(4), "napcat")

        if self.current_log:
            self.current_log["message"] += f"\n{line}"
        return None

    def _flush_and_start_new(self, ts, logger_name, level, msg, l_type):
        old = self.current_log
        self.current_log = {"timestamp": ts, "logger": logger_name, "level": level.strip(), "message": msg, "type": l_type}
        return old

    def flush(self):
        old = self.current_log
        self.current_log = None
        return old

def parse_log_iterable(lines: Iterable[str]) -> Generator[Dict[str, Any], None, None]:
    parser = LogStreamParser()
    for line in lines:
        parsed = parser.feed(line)
        if parsed: yield parsed
    final = parser.flush()
    if final: yield final