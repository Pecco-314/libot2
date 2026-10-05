#!/usr/bin/env python3
"""从 NoneBot 日志恢复 QQ 表情回应；默认预览，--apply 写入独立数据库。"""
import argparse
import ast
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.utils import ROOT
from src.db import qq_reactions as db

PREFIX = r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[SUCCESS\] nonebot \| "
NOTICE = re.compile(PREFIX + r"OneBot V11 (\d+) \| \[notice\.group_msg_emoji_like\]: (\{.*\})")
MESSAGE = re.compile(PREFIX + r"OneBot V11 (\d+) \| \[message\.group\.normal\]: Message (-?\d+) from (\d+)@\[群:(\d+)\]")
MEMBER = re.compile(r"接收 <- 群聊 \[.*?\((\d+)\)\] \[(.*?)\((\d+)\)\]")


def read_logs(paths, name_paths=()):
    notices, keys, malformed = [], set(), 0
    for path in paths:
        with path.open(errors="replace") as stream:
            for line in stream:
                match = NOTICE.search(line)
                if not match:
                    continue
                try:
                    notice = ast.literal_eval(match[2])
                    key = (int(notice["self_id"]), int(notice["group_id"]), int(notice["message_id"]))
                    int(notice["time"])
                    if not isinstance(notice.get("is_add"), bool):
                        raise ValueError("missing operation")
                    notices.append(notice)
                    keys.add(key)
                except (ValueError, SyntaxError, KeyError, TypeError):
                    malformed += 1
    authors, names = {}, {}
    for path in paths:
        with path.open(errors="replace") as stream:
            for line in stream:
                match = MESSAGE.search(line)
                if match:
                    self_id, mid, uid, gid = map(int, match.groups())
                    key = (self_id, gid, mid)
                    if key in keys:
                        authors[key] = uid
    for path in name_paths:
        with path.open(errors="replace") as stream:
            for line in stream:
                match = MEMBER.search(line)
                if match:
                    names[(int(match[1]), int(match[3]))] = match[2]
    notices.sort(key=lambda x: int(x["time"]))
    return notices, authors, names, malformed


def recover(paths, name_paths=(), apply=False, db_path=None):
    notices, authors, names, malformed = read_logs(paths, name_paths)
    summary = {"notices": len(notices), "authors": len(authors), "names": len(names),
               "malformed": malformed,
               "author_resolved_notices": sum((int(n["self_id"]), int(n["group_id"]), int(n["message_id"])) in authors for n in notices),
               "first": notices[0]["time"] if notices else None,
               "last": notices[-1]["time"] if notices else None, "applied": apply}
    if apply:
        db.init_db(db_path)
        with db.connection(db_path) as conn:
            for key, uid in authors.items():
                db.save_author(conn, *key, uid, 0, names.get((key[1], uid), ""))
            # 贴表情的人不一定在保留的日志里发过消息。
            conn.executemany("INSERT OR IGNORE INTO member VALUES(?,?,?,0)",
                             [(gid, uid, name) for (gid, uid), name in names.items()])
            summary["inserted"] = sum(db.save_notice(conn, n) for n in notices)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--db", type=Path, default=db.DB_PATH)
    args = parser.parse_args()

    def ordered(pattern):
        # 数字轮转后缀越大越旧；同秒内保持日志原始顺序。
        return sorted(args.log_dir.glob(pattern), key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0, reverse=True)

    print(json.dumps(recover(ordered("libot.log*"), ordered("napcat.log*"), args.apply, args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
