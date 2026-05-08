from __future__ import annotations

import csv
import logging
import json
import os
from datetime import datetime
from io import StringIO

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.song_list import (
    init_song_list_db, 
    batch_upsert_songs, 
    get_all_songs, 
    delete_songs_not_in
)
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


def _is_same_song(old_s: dict, new_s: dict) -> bool:
    diff_cols = 0
    # 我们只关注除了 id 以外的核心数据列的变动情况
    columns_to_check = [
        "title", "title_trans", "original_singer", "records", 
        "notes", "language", "count", "clips", "tags"
    ]
    
    for col in columns_to_check:
        if old_s.get(col) != new_s.get(col):
            diff_cols += 1
            
    # 按照要求：变动列数小于等于3，认为仍然是同一首歌
    return diff_cols <= 3


def _align_and_diff(old_songs: list[dict], new_songs: list[dict]) -> None:
    i, j = 0, 0
    mapped_lyrics = {}
    
    while i < len(old_songs) and j < len(new_songs):
        old_s = old_songs[i]
        new_s = new_songs[j]
        
        if _is_same_song(old_s, new_s):
            # 是同一首歌，记录它的旧歌词备用
            mapped_lyrics[new_s["id"]] = (old_s.get("lyrics"), old_s.get("lyrics_cleaned"))
            
            # 日志记录更新和位移
            if old_s["id"] != new_s["id"]:
                logger.info("DIFF SHIFT: [%s]%s 移动到了 [%s]%s", old_s["id"], old_s["title"], new_s["id"], new_s["title"])
            elif old_s.get("title") != new_s.get("title"):
                logger.info("DIFF UPDATE: [%s] 顶正歌名/更新信息: %s -> %s", new_s["id"], old_s.get("title"), new_s.get("title"))
                
            i += 1
            j += 1
        else:
            found_match = False
            # 遇到对不上的行，向下看最多 15 行，寻找是否由于被删除或插入产生了错位
            for offset in range(1, 16):
                # 场景A：新数据增加了行，把老歌顶到后面了。在 new_songs 后面找旧歌
                if j + offset < len(new_songs) and _is_same_song(old_s, new_songs[j + offset]):
                    logger.info("DIFF INSERT: 探测到在 id=%s 附近有 %d 行新插入的数据", new_s["id"], offset)
                    j += offset
                    found_match = True
                    break
                
                # 场景B：新数据删除了行，把新歌提上来了。在 old_songs 后面找新歌
                if i + offset < len(old_songs) and _is_same_song(old_songs[i + offset], new_s):
                    logger.info("DIFF DELETE: 探测到在旧 id=%s 附近有 %d 行数据被删除", old_s["id"], offset)
                    i += offset
                    found_match = True
                    break
            
            if not found_match:
                logger.warning("DIFF UNKNOWN: 无法对齐，可能发生了全面覆写。旧 id=%s (%s) / 新 id=%s (%s)", old_s["id"], old_s.get("title"), new_s["id"], new_s.get("title"))
                i += 1
                j += 1
                
    # 将提取出的历史歌词塞回要写入的 new_songs 中
    for s in new_songs:
        if s["id"] in mapped_lyrics:
            ly, ly_c = mapped_lyrics[s["id"]]
            s["lyrics"] = ly if ly is not None else ""
            s["lyrics_cleaned"] = ly_c if ly_c is not None else ""
        else:
            # 清理由于新歌顶替旧 ID 时造成的旧歌词残留
            s["lyrics"] = ""
            s["lyrics_cleaned"] = ""


async def sync_song_list() -> None:
    logger.info("song list sync begin")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(CSV_RAW_URL)
            resp.raise_for_status()
            csv_text = resp.read().decode("utf-8-sig").lstrip("\ufeff")

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
            # 1. 抓取本地数据库全量老数据
            old_songs = get_all_songs()
            if old_songs:
                # 2. 如果存在老数据，进行指针比对与歌词重定位
                _align_and_diff(old_songs, songs)
            
            # 3. 将包含歌词状态的最新数据合并入库
            batch_upsert_songs(songs)
            
            # 4. 清理末尾因为远程删库导致不再被占用的一批游离旧 ID
            valid_ids = [s["id"] for s in songs]
            delete_songs_not_in(valid_ids)
            
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