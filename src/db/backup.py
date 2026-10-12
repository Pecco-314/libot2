from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH

BACKUP_NAME = re.compile(r"libot_\d{8}_\d{6}\.db\Z")


def cleanup_old_backups(backup_dir: Path, keep_count: int = 1) -> None:
    """只轮替自动备份及其伴随文件，保留手工备份和审计记录。"""
    if keep_count < 1:
        raise ValueError("至少保留一份备份")
    backups = sorted(p for p in backup_dir.glob("libot_*.db") if BACKUP_NAME.fullmatch(p.name))
    for old_file in backups[:-keep_count]:
        old_file.unlink()
        for suffix in ('-wal', '-shm'):
            Path(str(old_file)+suffix).unlink(missing_ok=True)


def backup_sqlite_db(
    keep_count: int = 1,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    backup_dir: Path | None = None,
    max_seconds: float = 1800,
) -> Path:
    """从只读快照生成独立数据库，验证完成后原子发布并轮替。"""
    if keep_count < 1 or max_seconds <= 0:
        raise ValueError("备份保留数量和执行时限必须为正")
    backup_dir = backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"libot_{timestamp}.db"
    if backup_file.exists():
        raise FileExistsError(backup_file)
    temporary: Path | None = None
    started = time.monotonic()

    def check_deadline(*_):
        if time.monotonic() - started > max_seconds:
            raise TimeoutError("数据库备份超出执行时限")

    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            # 固定源快照，分页备份可让出 CPU；高频写入不会使已复制进度反复归零。
            source.execute('BEGIN')
            page_count = source.execute('PRAGMA page_count').fetchone()[0]
            page_size = source.execute('PRAGMA page_size').fetchone()[0]
            required = page_count * page_size + 512 * 1024 * 1024
            available = shutil.disk_usage(backup_dir).free
            if available < required:
                raise OSError(
                    f"备份空间不足：需要至少 {required / 1024**3:.2f} GiB，"
                    f"可用 {available / 1024**3:.2f} GiB；保留已有备份"
                )
            fd, name = tempfile.mkstemp(prefix=backup_file.stem + '_', suffix='.partial', dir=backup_dir)
            os.close(fd)
            temporary = Path(name)
            with closing(sqlite3.connect(temporary)) as destination:
                # 目的库使用回滚日志，完整副本直接写入 db，避免产生同等大小的 WAL。
                destination.execute('PRAGMA journal_mode=DELETE')
                source.backup(destination, pages=1024, progress=check_deadline, sleep=.05)
                destination.execute('PRAGMA journal_mode=DELETE')
                destination.set_progress_handler(
                    lambda: int(time.monotonic() - started > max_seconds), 10000,
                )
                check = destination.execute('PRAGMA quick_check(1)').fetchone()[0]
                if check != 'ok':
                    raise RuntimeError(f"备份完整性检查失败：{check}")
            source.rollback()
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        temporary.replace(backup_file)
        temporary = None
    except Exception as exc:
        raise RuntimeError(f"Failed to backup database: {exc}") from exc
    finally:
        if temporary is not None:
            for suffix in ('', '-wal', '-shm', '-journal'):
                Path(str(temporary)+suffix).unlink(missing_ok=True)
    cleanup_old_backups(backup_dir, keep_count)
    return backup_file
