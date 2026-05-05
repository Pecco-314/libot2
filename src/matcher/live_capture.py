from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from src.common.utils import load_env_file
from src.db.subscription import list_subscribed_room_ids
from src.db.event import is_streaming_event, get_latest_live_cmd, list_live_events_after
from src.spider.api import BILI_HEADERS, build_cookies


DEFAULT_USER_AGENT = BILI_HEADERS["User-Agent"]


@dataclass(frozen=True)
class LiveCaptureConfig:
    room_ids: list[int]
    qn: int = 150
    audio_segment_seconds: int = 300
    frame_interval_seconds: int = 10
    output_root: Path | None = None
    cookies: dict[str, str] | None = None
    user_agent: str = DEFAULT_USER_AGENT


class BilibiliLiveStreamResolver:
    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, cookies: dict[str, str] | None = None):
        self._user_agent = user_agent
        self._cookies = cookies

    def get_stream_url(self, room_id: str, qn: int = 150) -> str | None:
        play_api = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
        headers = {"User-Agent": self._user_agent}

        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": qn,
            "platform": "web",
            "ptype": 8,
        }

        try:
            with httpx.Client(timeout=10.0, headers=headers, cookies=self._cookies) as client:
                response = client.get(play_api, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return None

        if payload.get("code") != 0:
            return None

        try:
            stream_data: dict[str, Any] = (
                payload["data"]["playurl_info"]["playurl"]["stream"][0]["format"][0]["codec"][0]
            )
            base_url = stream_data["base_url"]
            url_info = stream_data["url_info"][0]
            host = url_info["host"]
            extra = url_info["extra"]
        except Exception:
            return None

        return f"{host}{base_url}{extra}"


class LiveCapture:
    def __init__(self, config: LiveCaptureConfig):
        self._config = config
        self._resolver = BilibiliLiveStreamResolver(
            user_agent=config.user_agent,
            cookies=config.cookies,
        )
        self._processes: dict[int, subprocess.Popen] = {}
        self._last_cmd_by_room: dict[int, str] = {}

    def _resolve_output_root(self) -> Path:
        if self._config.output_root:
            return self._config.output_root
        return Path(__file__).resolve().parents[2] / "data" / "live"

    def _build_output_paths(self, room_id: str) -> tuple[Path, Path]:
        root = self._resolve_output_root()
        audio_dir = root / "audio" / room_id
        frame_dir = root / "frames" / room_id
        audio_dir.mkdir(parents=True, exist_ok=True)
        frame_dir.mkdir(parents=True, exist_ok=True)
        audio_pattern = audio_dir / "audio_%Y%m%d_%H%M%S.m4a"
        frame_pattern = frame_dir / "frame_%Y%m%d_%H%M%S.jpg"
        return audio_pattern, frame_pattern

    def _build_ffmpeg_command(self, url: str, audio_pattern: Path, frame_pattern: Path) -> list[str]:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is not available in PATH")

        fps_value = 1 / max(self._config.frame_interval_seconds, 1)
        header_parts = [
            f"User-Agent: {self._config.user_agent}",
            "Referer: https://live.bilibili.com/",
            "Origin: https://live.bilibili.com",
        ]
        if self._config.cookies:
            cookie_value = "; ".join(f"{k}={v}" for k, v in self._config.cookies.items())
            if cookie_value:
                header_parts.append(f"Cookie: {cookie_value}")
        headers_value = "\r\n".join(header_parts) + "\r\n"
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "2",
            "-headers",
            headers_value,
            "-i",
            url,
            "-map",
            "0:a",
            "-c:a",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self._config.audio_segment_seconds),
            "-strftime",
            "1",
            str(audio_pattern),
            "-map",
            "0:v",
            "-vf",
            f"fps={fps_value},scale=in_range=full:out_range=full,format=yuv420p",
            "-strftime",
            "1",
            str(frame_pattern),
        ]

    def _start_capture(self, room_id: int) -> None:
        if room_id in self._processes:
            return
        url = self._resolver.get_stream_url(str(room_id), qn=self._config.qn)
        if not url:
            print(f"failed to resolve live stream url for room_id={room_id}", file=sys.stderr)
            return
        audio_pattern, frame_pattern = self._build_output_paths(str(room_id))
        command = self._build_ffmpeg_command(url, audio_pattern, frame_pattern)
        self._processes[room_id] = subprocess.Popen(command)

    def _stop_capture(self, room_id: int) -> None:
        process = self._processes.pop(room_id, None)
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    def _fetch_events_since(self, last_id: int) -> list[dict[str, Any]]:
        return list_live_events_after(self._config.room_ids, last_id)

    def run(self) -> int:
        last_id = 0
        for room_id in self._config.room_ids:
            latest_cmd = get_latest_live_cmd(room_id)
            if latest_cmd:
                self._last_cmd_by_room[room_id] = latest_cmd
                if latest_cmd == "LIVE":
                    self._start_capture(room_id)
        try:
            while True:
                rows = self._fetch_events_since(last_id)
                if not rows:
                    time.sleep(1)
                    continue

                for row in rows:
                    last_id = max(last_id, int(row.get("id") or 0))
                    cmd = row.get("cmd")
                    room_id = int(row.get("room_id") or 0)
                    if cmd == "LIVE":
                        if is_streaming_event(row):
                            continue
                        self._last_cmd_by_room[room_id] = "LIVE"
                        self._start_capture(room_id)
                    elif cmd == "PREPARING":
                        self._last_cmd_by_room[room_id] = "PREPARING"
                        self._stop_capture(room_id)
        except KeyboardInterrupt:
            pass
        finally:
            for room_id in list(self._processes.keys()):
                self._stop_capture(room_id)

        return 0


def _parse_args(argv: list[str]) -> LiveCaptureConfig:
    parser = argparse.ArgumentParser(description="Capture Bilibili live audio and keyframes")
    parser.add_argument("--qn", type=int, default=150, help="quality level, default 150")
    parser.add_argument(
        "--audio-segment",
        type=int,
        default=15,
        help="audio segment length in seconds",
    )
    parser.add_argument(
        "--frame-interval",
        type=int,
        default=5,
        help="keyframe interval in seconds",
    )

    args = parser.parse_args(argv)
    output_root = None
    room_ids = list_subscribed_room_ids()
    if not room_ids:
        raise ValueError("subscription 表为空，无法获取直播间房间号")

    return LiveCaptureConfig(
        room_ids=room_ids,
        qn=args.qn,
        audio_segment_seconds=args.audio_segment,
        frame_interval_seconds=args.frame_interval,
        output_root=output_root,
        cookies=build_cookies(),
    )


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    config = _parse_args(argv or sys.argv[1:])
    capture = LiveCapture(config)
    return capture.run()


if __name__ == "__main__":
    raise SystemExit(main())
