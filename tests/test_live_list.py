from __future__ import annotations

import ast
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.db.live_list import get_live_list_by_tag


class LiveListTest(unittest.TestCase):
    def test_tag_lookup_is_exact_case_insensitive_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "live_list.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE global_live_list (room_id INTEGER PRIMARY KEY, uname TEXT, adder_uid INTEGER, tags TEXT)"
                )
                conn.executemany(
                    "INSERT INTO global_live_list VALUES (?, ?, ?, ?)",
                    [
                        (1, "Zeta", 1, '["VR", "拼好团"]'),
                        (2, "alpha", 1, '["vr"]'),
                        (3, "Other", 1, '["vr圈"]'),
                        (4, "Broken", 1, "not-json"),
                    ],
                )

            rooms = get_live_list_by_tag("Vr", db_path)
            self.assertEqual([(room["uname"], room["room_id"]) for room in rooms], [("alpha", 2), ("Zeta", 1)])
            self.assertEqual(get_live_list_by_tag("", db_path), [])


class Finished(Exception):
    pass


class LiveListCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_all_and_explicit_tags(self):
        tree = ast.parse(Path("src/plugins/commands.py").read_text())
        handler = next(n for n in tree.body
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_live_list_show")
        handler.decorator_list = []
        rooms = [{"room_id": 1, "tags": ["VR"]},
                 {"room_id": 2, "tags": ["vr"]},
                 {"room_id": 3, "tags": ["其他"]},
                 {"room_id": 4, "tags": ["PSP"]},
                 {"room_id": 5, "tags": ["ldc"]},
                 {"room_id": 6, "tags": ["VR", "PSP", "LDC"]},
                 {"room_id": 7, "tags": ["VR圈"]},
                 {"room_id": 8, "tags": []}]
        for raw, expected, tag in [("", [1, 2, 4, 5, 6], "VR+PSP+LDC"),
                                   ("  ", [1, 2, 4, 5, 6], "VR+PSP+LDC"),
                                   ("全部", list(range(1, 9)), None), ("vr", [1, 2, 6], "vr"),
                                   ("PSP", [4, 6], "PSP"), ("LDC", [5, 6], "LDC"),
                                   ("其他", [3], "其他"), ("未知", [], "未知")]:
            with self.subTest(raw=raw):
                renderer = AsyncMock(return_value="test.png")
                matcher = SimpleNamespace(finish=AsyncMock(side_effect=Finished))
                env = dict(Matcher=object, Event=object, CommandArg=lambda: None,
                           get_live_list=lambda: rooms, render_live_list_image=renderer,
                           logger=logging.getLogger("test"),
                           MessageSegment=SimpleNamespace(image=lambda **kwargs: kwargs))
                exec(compile(ast.Module(body=[handler], type_ignores=[]), "live_list_handler", "exec"), env)
                with self.assertRaises(Finished):
                    await env["handle_live_list_show"](matcher, object(),
                        SimpleNamespace(extract_plain_text=lambda: raw))
                if expected:
                    selected, selected_tag = renderer.call_args.args
                    self.assertEqual([r["room_id"] for r in selected], expected)
                    self.assertEqual(selected_tag, tag)
                else:
                    renderer.assert_not_awaited()
                    self.assertIn("[未知]", matcher.finish.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
