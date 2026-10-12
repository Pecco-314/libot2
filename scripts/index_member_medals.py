#!/usr/bin/env python3
"""显式创建粉丝团历史牌子的 UID 索引；默认仅检查，--apply 执行迁移。"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.fan_club import MEMBER_UID_INDEX, MEMBER_UID_INDEX_SQL
from src.db.sqlite import DEFAULT_DB_PATH


def build_index(db_path: Path, max_seconds: float = 50) -> float:
    """用进度回调限制构建时间，出错时回滚；提交和回滚另有少量开销。"""
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    started = time.monotonic()
    with sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=rw", uri=True, timeout=1
    ) as conn:
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("BEGIN IMMEDIATE")
            conn.set_progress_handler(
                lambda: int(time.monotonic() - started >= max_seconds), 1000
            )
            conn.execute(MEMBER_UID_INDEX_SQL)
            conn.execute(f"ANALYZE {MEMBER_UID_INDEX}")
            conn.commit()
        finally:
            conn.set_progress_handler(None, 0)
    return time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=50)
    args = parser.parse_args()
    if args.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    with sqlite3.connect(
        f"{args.db.resolve().as_uri()}?mode=ro", uri=True
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (MEMBER_UID_INDEX,),
        ).fetchone() is not None
    print(f"index={MEMBER_UID_INDEX} exists={exists}", flush=True)
    if exists or not args.apply:
        return 0
    print(f"building index; progress-handler budget={args.max_seconds:g}s", flush=True)
    try:
        elapsed = build_index(args.db, args.max_seconds)
    except sqlite3.Error as exc:
        print(f"migration rolled back: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"complete elapsed={elapsed:.3f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
