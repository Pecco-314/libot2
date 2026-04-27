from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.db.sqlite import connect_sqlite, DEFAULT_DB_PATH

def cleanup_old_backups(backup_dir: Path, keep_count: int = 2) -> None:
    # 获取目录下所有匹配 libot_*.db 格式的备份文件
    backups = sorted(backup_dir.glob("libot_*.db"))
    
    # 如果文件数量超过限制，则删除最早的文件
    if len(backups) > keep_count:
        to_delete = backups[:-keep_count]
        for old_file in to_delete:
            old_file.unlink()


def backup_sqlite_db(keep_count: int = 2) -> Path:
    backup_dir = DEFAULT_DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"libot_{timestamp}.db"
    
    # 建立连接并执行备份
    source_conn = connect_sqlite(DEFAULT_DB_PATH)
    dest_conn = connect_sqlite(backup_file)
    
    try:
        with source_conn, dest_conn:
            source_conn.backup(dest_conn)
        
        # 备份成功后执行清理逻辑
        cleanup_old_backups(backup_dir, keep_count)
        
    except Exception as exc:
        if backup_file.exists():
            backup_file.unlink()
        raise RuntimeError(f"Failed to backup database: {exc}")
    finally:
        source_conn.close()
        dest_conn.close()
        
    return backup_file