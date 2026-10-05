"""Read-only title-clock cross-check using matching audio from two recordings.

Per-part title timestamps describe the mirror recording, not the official
recording. Audio lag transfers that clock locally without assuming continuous
recording across parts. Results are diagnostics, never deletion proposals.
"""
import asyncio
import json
from pathlib import Path

import httpx

from scripts.backfill_mit3uri_replay_danmaku import (
    HEADERS, audio_feature_alignment, load_playurl_audio_urls,
    sanitized_subprocess_env, write_json_atomic,
)

ROOT = Path('data/reports/title_clock_trial_20261004')


async def extract(client, bvid, cid, start, duration, label):
    path = ROOT / (label + '.wav')
    if path.exists():
        return path
    urls = await load_playurl_audio_urls(client, bvid=bvid, cid=cid)
    errors = []
    for url in urls:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-rw_timeout', '30000000', '-ss', str(start), '-headers',
            f"Referer: https://www.bilibili.com/video/{bvid}\r\nUser-Agent: {HEADERS['User-Agent']}\r\n",
            '-i', url, '-t', str(duration), '-vn', '-ac', '1', '-ar', '4000',
            '-c:a', 'pcm_s16le', str(path), env=sanitized_subprocess_env(),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return path
        errors.append(err.decode()[-300:])
    raise RuntimeError(str(errors))


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, trust_env=False) as client:
        results = []
        # Two independent windows check for local discontinuity or drift.
        for label, official_start, mirror_start in [('june06_before', 1380, 8560), ('june06_after', 1580, 8760)]:
            official = await extract(client, 'BV1MgTGzSEYS', 30360732428, official_start, 180, label + '_official')
            mirror = await extract(client, 'BV1wETMzZEec', 30355099052, mirror_start, 180, label + '_mirror')
            fit = audio_feature_alignment(official, mirror, max_lag_seconds=50)
            fit.update(label=label, official_start=official_start, mirror_start=mirror_start,
                       mirror_minus_official_seconds=mirror_start - official_start + fit['lag_seconds'])
            results.append(fit)
            print(json.dumps(fit), flush=True)
        write_json_atomic(ROOT / 'audio_clock_results.json', dict(event_mutated=False, results=results))


if __name__ == '__main__':
    asyncio.run(main())
