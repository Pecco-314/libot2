from __future__ import annotations

import asyncio
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.song_list import init_song_list_db, list_songs_without_lyrics, update_song_lyrics


logger = logging.getLogger("spider.jobs.lyrics")

LYRICS_API_URL = "http://localhost:28883/jsonapi"


def _normalize(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[\s\-_/\\.,，。·]+", "", text.strip().lower())
    return cleaned


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _pick_best_match(title: str, artist: str, results: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float, float]:
    best_item: dict[str, Any] | None = None
    best_title_score = 0.0
    best_artist_score = 0.0
    best_total = 0.0
    for item in results:
        if not isinstance(item, dict):
            continue
        lyrics = (item.get("lyrics") or "").strip()
        if not lyrics:
            continue
        item_title = item.get("title") or ""
        item_artist = item.get("artist") or ""
        title_score = _similarity(title, item_title)
        artist_score = _similarity(artist, item_artist)
        total_score = title_score + artist_score
        if total_score > best_total:
            best_item = item
            best_title_score = title_score
            best_artist_score = artist_score
            best_total = total_score
            print(title, artist, item_title, item_artist)
    return best_item, best_title_score, best_artist_score


async def sync_lyrics() -> None:
    songs = list_songs_without_lyrics()
    if not songs:
        logger.info("lyrics sync skip: no missing lyrics")
        return

    logger.info("lyrics sync begin, pending=%d", len(songs))
    async with httpx.AsyncClient(timeout=20.0) as client:
        for song in songs:
            song_id = song.get("id")
            title = song.get("title") or ""
            artist = song.get("original_singer") or ""
            if not title and not artist:
                logger.warning("lyrics sync skip id=%s because title/artist empty", song_id)
                continue
            try:
                resp = await client.get(
                    LYRICS_API_URL,
                    params={"title": title, "artist": artist},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "lyrics sync failed id=%s status=%d body=%s",
                        song_id,
                        resp.status_code,
                        resp.text,
                    )
                    continue
                results = resp.json()
            except Exception as exc:
                logger.warning(
                    "lyrics sync failed id=%s title=%s artist=%s: %s",
                    song_id,
                    title,
                    artist,
                    exc,
                )
                continue

            if not isinstance(results, list) or not results:
                logger.info(
                    "lyrics sync empty id=%s title=%s artist=%s response=%s",
                    song_id,
                    title,
                    artist,
                    resp.text,
                )
                continue

            best_item, title_score, artist_score = _pick_best_match(title, artist, results)
            if (
                not best_item
                or title_score < 0.9
                or artist_score < 0.9
            ):
                logger.info(
                    "lyrics sync mismatch id=%s title=%s artist=%s title_score=%.3f artist_score=%.3f results=%s",
                    song_id,
                    title,
                    artist,
                    title_score,
                    artist_score,
                    json.dumps(results, ensure_ascii=False),
                )
                continue

            lyrics = (best_item.get("lyrics") or "").strip()
            update_song_lyrics(song_id, lyrics)
            logger.info(
                "lyrics sync updated id=%s title=%s artist=%s title_score=%.3f artist_score=%.3f",
                song_id,
                title,
                artist,
                title_score,
                artist_score,
            )


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_song_list_db()
    scheduler.add_job(
        sync_lyrics,
        "cron",
        hour=5,
        minute=0,
        id="song_lyrics_sync",
        name="song_lyrics_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(sync_lyrics())
