from __future__ import annotations

import asyncio
import csv
import logging
import os
from datetime import datetime
from io import StringIO

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.song_list import (
    init_song_list_db,
    merge_songs_from_source,
)
from src.common.utils import load_env_file

load_env_file()

logger = logging.getLogger("spider.jobs.song_list")

CSV_RAW_URL = (
    "https://raw.githubusercontent.com/mit3urifans/"
    "mit3uri-song-list/main/scripts/music_data.csv"
)
SOURCE_NAME = "github:mit3urifans/mit3uri-song-list"
GITHUB_PROXY = os.environ.get("GITHUB_PROXY")
if GITHUB_PROXY:
    CSV_RAW_URL = GITHUB_PROXY + CSV_RAW_URL


def _parse_dates(date_str: str) -> list[str]:
    if not date_str:
        return []

    parts = [p.strip() for p in date_str.split("，") if p.strip()]

    records = []
    for part in parts:
        try:
            dt = datetime.strptime(part, "%Y/%m/%d")
            records.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            records.append(part)

    return records


def _parse_clips(clips_str: str) -> list[str]:
    if not clips_str:
        return []

    clips_str = clips_str.replace("，", ",")
    clips = [c.strip() for c in clips_str.split(",") if c.strip()]
    return clips


def parse_song_csv(csv_text: str) -> list[dict]:
    reader = csv.DictReader(StringIO(csv_text.lstrip("\ufeff")))
    songs: list[dict] = []
    for row in reader:
        external_id = str(row.get("序号") or "").strip()
        if not external_id or not str(row.get("歌名") or "").strip():
            continue
        songs.append(
            {
                "external_id": external_id,
                "title": row.get("歌名", ""),
                "title_trans": row.get("歌名翻译", ""),
                "original_singer": row.get("原唱", ""),
                "records": _parse_dates(row.get("日期", "")),
                "notes": row.get("备注", ""),
                "language": row.get("语言", ""),
                "clips": _parse_clips(row.get("歌切", "")),
                "tags": row.get("标签", ""),
            }
        )
    return songs


async def sync_song_list() -> None:
    logger.info("song list sync begin")
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.get(CSV_RAW_URL)
            resp.raise_for_status()
            csv_text = resp.read().decode("utf-8-sig").lstrip("\ufeff")

        songs = parse_song_csv(csv_text)

        if songs:
            result = merge_songs_from_source(SOURCE_NAME, songs)
            logger.info(
                "song list sync success source_rows=%d matched=%d inserted=%d "
                "metadata_updates=%d records_added=%d clips_added=%d "
                "tags_added=%d conflicts=%d ambiguous=%d",
                result["source_rows"],
                result["matched_songs"],
                result["inserted_songs"],
                result["metadata_updates"],
                result["record_rows_added"],
                result["clips_added"],
                result["tags_added"],
                result["conflict_count"],
                result["ambiguous_count"],
            )
            for conflict in result["conflicts"][:20]:
                logger.warning("song list local/remote conflict: %s", conflict)
            for ambiguous in result["ambiguous_rows"][:20]:
                logger.warning("song list ambiguous remote row: %s", ambiguous)
        else:
            logger.warning("song list sync finished but no valid data parsed")

    except Exception as exc:
        logger.warning("song list sync failed: %s", exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_song_list_db()
    scheduler.add_job(
        sync_song_list,
        "cron",
        hour=4,
        minute=0,
        id="song_list_sync",
        name="song_list_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_song_list())
