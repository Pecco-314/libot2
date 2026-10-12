from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, execute_write, write_transaction

DEFAULT_DISABLED_COMMANDS = frozenset({"在聊什么"})


@dataclass(frozen=True)
class CommandConfig:
    enabled: bool = True
    cooldown_seconds: float | None = None
    cooldown_count: int = 1


def init_command_config_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with write_transaction(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "command_cooldown" in tables and "command_config" not in tables:
            # 兼容短暂使用过的仅 CD 表，原有设置会完整保留。
            conn.execute("ALTER TABLE command_cooldown RENAME TO command_config")
            conn.execute(
                "ALTER TABLE command_config RENAME COLUMN seconds TO cooldown_seconds"
            )
            conn.execute(
                "ALTER TABLE command_config ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )

        columns = {
            str(row[1]): row
            for row in conn.execute("PRAGMA table_info(command_config)")
        }
        cooldown_column = columns.get("cooldown_seconds")
        if cooldown_column is not None and bool(cooldown_column[3]):
            # SQLite 无法直接移除旧 seconds 列的 NOT NULL，重建一次表。
            execute_write(
                conn,
                """
                CREATE TABLE command_config_new (
                    group_id INTEGER NOT NULL,
                    command_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    cooldown_seconds REAL CHECK (
                        cooldown_seconds IS NULL OR cooldown_seconds > 0
                    ),
                    updated_by INTEGER NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, command_name)
                )
                """,
            )
            execute_write(
                conn,
                """
                INSERT INTO command_config_new (
                    group_id, command_name, enabled, cooldown_seconds,
                    updated_by, updated_at
                )
                SELECT
                    group_id, command_name, enabled, cooldown_seconds,
                    updated_by, updated_at
                FROM command_config
                """,
            )
            conn.execute("DROP TABLE command_config")
            conn.execute("ALTER TABLE command_config_new RENAME TO command_config")

        execute_write(
            conn,
            """
            CREATE TABLE IF NOT EXISTS command_config (
                group_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                cooldown_seconds REAL CHECK (
                    cooldown_seconds IS NULL OR cooldown_seconds > 0
                ),
                updated_by INTEGER NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, command_name)
            )
            """,
        )


        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(command_config)")}
        if "cooldown_count" not in columns:
            conn.execute(
                "ALTER TABLE command_config ADD COLUMN cooldown_count INTEGER "
                "NOT NULL DEFAULT 1 CHECK (cooldown_count > 0)"
            )


def get_command_config(
    group_id: int,
    command_name: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> CommandConfig:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT enabled, cooldown_seconds, cooldown_count
            FROM command_config
            WHERE group_id = ? AND command_name = ?
            """,
            (group_id, command_name),
        ).fetchone()
    if row is None:
        return CommandConfig(enabled=command_name not in DEFAULT_DISABLED_COMMANDS)
    return CommandConfig(
        enabled=bool(row[0]),
        cooldown_seconds=float(row[1]) if row[1] is not None else None,
        cooldown_count=int(row[2]),
    )


def enabled_command_groups(command_name: str, db_path: Path = DEFAULT_DB_PATH) -> list[int]:
    """列出显式开启功能的群，供默认关闭的后台任务使用。"""
    with connect_sqlite(db_path) as conn:
        return [int(row[0]) for row in conn.execute(
            "SELECT group_id FROM command_config WHERE command_name=? AND enabled=1 ORDER BY group_id",
            (command_name,),
        )]


def set_command_enabled(
    group_id: int,
    command_name: str,
    enabled: bool,
    updated_by: int,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with write_transaction(db_path) as conn:
        execute_write(
            conn,
            """
            INSERT INTO command_config (
                group_id, command_name, enabled, cooldown_seconds, updated_by
            ) VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(group_id, command_name) DO UPDATE SET
                enabled = excluded.enabled,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, command_name, int(enabled), updated_by),
        )


def set_command_cooldown(
    group_id: int,
    command_name: str,
    seconds: float | None,
    updated_by: int,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    count: int = 1,
) -> None:
    if seconds is not None and seconds <= 0:
        raise ValueError("cooldown must be positive")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("cooldown count must be a positive integer")
    with write_transaction(db_path) as conn:
        execute_write(
            conn,
            """
            INSERT INTO command_config (
                group_id, command_name, enabled, cooldown_seconds, updated_by, cooldown_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, command_name) DO UPDATE SET
                cooldown_seconds = excluded.cooldown_seconds,
                cooldown_count = excluded.cooldown_count,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, command_name, int(command_name not in DEFAULT_DISABLED_COMMANDS),
             seconds, updated_by, count if seconds else 1),
        )
