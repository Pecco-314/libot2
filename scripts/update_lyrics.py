#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "libot.db"


def _preview_text(text: str | None, width: int = 120) -> str:
	if not text:
		return "<empty>"
	one_line = text.replace("\n", "\\n")
	if len(one_line) <= width:
		return one_line
	return one_line[:width] + "..."


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="手动更新 data/libot.db 中 song_info 表的 lyrics 字段"
	)
	parser.add_argument(
		"--db",
		type=Path,
		default=DEFAULT_DB_PATH,
		help=f"SQLite 数据库路径（默认: {DEFAULT_DB_PATH}）",
	)

	target = parser.add_mutually_exclusive_group(required=True)
	target.add_argument("--id", type=int, help="按歌曲 id 更新")
	target.add_argument("--title", type=str, help="按歌曲标题精确匹配更新")

	source = parser.add_mutually_exclusive_group(required=True)
	source.add_argument("--lyrics", type=str, help="直接传入歌词文本")
	source.add_argument("--from-file", type=Path, help="从文本文件读取歌词")

	parser.add_argument(
		"--yes",
		action="store_true",
		help="跳过交互确认，直接更新",
	)
	return parser.parse_args()


def _read_new_lyrics(args: argparse.Namespace) -> str:
	if args.lyrics is not None:
		return args.lyrics
	if args.from_file is not None:
		return args.from_file.read_text(encoding="utf-8")
	raise ValueError("必须提供 --lyrics 或 --from-file")


def _load_target_row(conn: sqlite3.Connection, song_id: int | None, title: str | None) -> tuple[int, str, str | None] | None:
	if song_id is not None:
		row = conn.execute(
			"SELECT id, title, lyrics FROM song_info WHERE id = ?",
			(song_id,),
		).fetchone()
		return row

	row = conn.execute(
		"SELECT id, title, lyrics FROM song_info WHERE title = ?",
		(title,),
	).fetchone()
	return row


def main() -> int:
	args = _parse_args()
	db_path = args.db.resolve()

	if not db_path.exists():
		print(f"数据库不存在: {db_path}", file=sys.stderr)
		return 1

	new_lyrics = _read_new_lyrics(args)

	with sqlite3.connect(db_path) as conn:
		row = _load_target_row(conn, args.id, args.title)
		if not row:
			print("未找到目标歌曲，请检查 --id 或 --title", file=sys.stderr)
			return 1

		song_id, song_title, old_lyrics = row

		print(f"目标歌曲: id={song_id}, title={song_title}")
		print(f"旧歌词预览: {_preview_text(old_lyrics)}")
		print(f"新歌词预览: {_preview_text(new_lyrics)}")

		if not args.yes:
			confirm = input("确认更新 lyrics? [y/N]: ").strip().lower()
			if confirm not in {"y", "yes"}:
				print("已取消")
				return 0

		conn.execute(
			"""
			UPDATE song_info
			SET lyrics = ?, updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(new_lyrics, song_id),
		)
		conn.commit()


	print("更新成功")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

