from __future__ import annotations

import csv
import logging
import json
import os
from datetime import datetime
from io import StringIO

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.song_list import init_song_list_db, batch_upsert_songs
from src.common.utils import load_env_file

load_env_file()

logger = logging.getLogger("spider.jobs.song_list")

CSV_RAW_URL = "https://raw.githubusercontent.com/mit3urifans/mit3uri-song-list/main/scripts/music_data.csv"
GITHUB_PROXY = os.environ.get("GITHUB_PROXY")
if GITHUB_PROXY:
    CSV_RAW_URL = GITHUB_PROXY + CSV_RAW_URL

def _parse_dates(date_str: str) -> str:
    if not date_str:
        return "[]"
    
    parts = [p.strip() for p in date_str.split("，") if p.strip()]
    
    records = []
    for part in parts:
        try:
            dt = datetime.strptime(part, "%Y/%m/%d")
            records.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            records.append(part)
            
    return json.dumps(records, ensure_ascii=False)


def _parse_clips(clips_str: str) -> str:
    if not clips_str:
        return "[]"
    
    clips = [c.strip() for c in clips_str.split(",") if c.strip()]
    return json.dumps(clips, ensure_ascii=False)


async def sync_song_list() -> None:
    logger.info("song list sync begin")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(CSV_RAW_URL)
            resp.raise_for_status()
            csv_text = resp.read().decode("utf-8-sig")

        reader = csv.DictReader(StringIO(csv_text))
        songs: list[dict] = []
        for row in reader:
            try:
                song_id = int(row.get("序号", 0))
            except ValueError:
                song_id = 0
                
            if song_id <= 0:
                continue

            songs.append({
                "id": song_id,
                "title": row.get("歌名", ""),
                "title_trans": row.get("歌名翻译", ""),
                "original_singer": row.get("原唱", ""),
                "records": _parse_dates(row.get("日期", "")),
                "notes": row.get("备注", ""),
                "language": row.get("语言", ""),
                "count": int(row.get("次数", 0)),
                "clips": _parse_clips(row.get("歌切", "")),
                "tags": row.get("标签", ""),
            })

        if songs:
            batch_upsert_songs(songs)
            logger.info("song list sync success, updated %d songs", len(songs))
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