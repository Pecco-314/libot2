from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.command_config import (
    CommandConfig,
    get_command_config,
    init_command_config_db,
    set_command_cooldown,
    set_command_enabled,
    enabled_command_groups,
)


class CommandConfigDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "command-config.db"
        init_command_config_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_are_enabled_and_unlimited(self) -> None:
        self.assertEqual(
            get_command_config(100, "抉择", self.db_path),
            CommandConfig(enabled=True, cooldown_seconds=None),
        )

    def test_summary_requires_explicit_opt_in_even_when_setting_cooldown(self):
        self.assertFalse(get_command_config(100, '在聊什么', self.db_path).enabled)
        set_command_cooldown(100, '在聊什么', 60, 123, self.db_path)
        self.assertFalse(get_command_config(100, '在聊什么', self.db_path).enabled)
        self.assertEqual(enabled_command_groups('在聊什么', self.db_path), [])
        set_command_enabled(100, '在聊什么', True, 123, self.db_path)
        self.assertEqual(enabled_command_groups('在聊什么', self.db_path), [100])
        self.assertFalse(get_command_config(200, '在聊什么', self.db_path).enabled)
        set_command_enabled(100, '在聊什么', False, 123, self.db_path)
        self.assertEqual(enabled_command_groups('在聊什么', self.db_path), [])

    def test_enabled_and_cooldown_are_independent_and_group_scoped(self) -> None:
        set_command_enabled(100, "抉择", False, 123, self.db_path)
        set_command_cooldown(100, "抉择", 60, 123, self.db_path)
        self.assertEqual(
            get_command_config(100, "抉择", self.db_path),
            CommandConfig(enabled=False, cooldown_seconds=60),
        )
        self.assertEqual(get_command_config(200, "抉择", self.db_path), CommandConfig())
        self.assertEqual(get_command_config(100, "来点", self.db_path), CommandConfig())

        set_command_enabled(100, "抉择", True, 456, self.db_path)
        self.assertEqual(
            get_command_config(100, "抉择", self.db_path),
            CommandConfig(enabled=True, cooldown_seconds=60),
        )
        set_command_cooldown(100, "抉择", None, 456, self.db_path)
        self.assertEqual(get_command_config(100, "抉择", self.db_path), CommandConfig())

    def test_migrates_the_previous_cooldown_only_table(self) -> None:
        old_db = Path(self.temp_dir.name) / "old.db"
        with sqlite3.connect(old_db) as conn:
            conn.execute(
                """
                CREATE TABLE command_cooldown (
                    group_id INTEGER NOT NULL,
                    command_name TEXT NOT NULL,
                    seconds REAL NOT NULL CHECK (seconds > 0),
                    updated_by INTEGER NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, command_name)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO command_cooldown
                    (group_id, command_name, seconds, updated_by)
                VALUES (100, '抉择', 30, 123)
                """
            )

        init_command_config_db(old_db)
        self.assertEqual(
            get_command_config(100, "抉择", old_db),
            CommandConfig(enabled=True, cooldown_seconds=30),
        )
        set_command_enabled(100, "抉择", False, 456, old_db)
        set_command_cooldown(100, "抉择", None, 456, old_db)
        self.assertEqual(
            get_command_config(100, "抉择", old_db),
            CommandConfig(enabled=False, cooldown_seconds=None),
        )
        with sqlite3.connect(old_db) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("command_config", tables)
        self.assertNotIn("command_cooldown", tables)

    def test_count_round_trip_and_reset(self) -> None:
        set_command_cooldown(100, "来点", 180, 123, self.db_path, count=3)
        self.assertEqual(
            get_command_config(100, "来点", self.db_path),
            CommandConfig(cooldown_seconds=180, cooldown_count=3),
        )
        init_command_config_db(self.db_path)
        self.assertEqual(get_command_config(100, "来点", self.db_path).cooldown_count, 3)
        with self.assertRaises(ValueError):
            set_command_cooldown(100, "来点", 180, 123, self.db_path, count=0)
        set_command_cooldown(100, "来点", None, 123, self.db_path)
        self.assertEqual(get_command_config(100, "来点", self.db_path), CommandConfig())


if __name__ == "__main__":
    unittest.main()
