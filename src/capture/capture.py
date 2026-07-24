from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import threading
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.common.utils import load_env_file, init_logger
from src.capture.transcribe import SenseVoiceEngine
from src.db.subscription import list_asr_enabled_room_ids
from src.db.event import is_streaming_event, get_latest_live_cmd, get_latest_live_event_id, list_live_events_after
from src.db.transcript import init_transcript_db, insert_transcript
from src.spider.api import BILI_HEADERS, build_cookies


DEFAULT_USER_AGENT = BILI_HEADERS["User-Agent"]
logger = init_logger("capture")
RETRYABLE_HTTP_ERRORS = re.compile(r"HTTP error (?:401|403|404|410)|Server returned (?:401|403|404|410)", re.IGNORECASE)
BASE_RESTART_BACKOFF = 2.0
MAX_RESTART_BACKOFF = 60.0
FAST_FAIL_SECONDS = 8.0
LIVE_GRACE_SECONDS = 180.0
AUDIO_STALL_SECONDS = 30.0


@dataclass(frozen=True)
class LiveCaptureConfig:
    room_ids: list[int]
    qn: int = 150
    cookies: dict[str, str] | None = None
    user_agent: str = DEFAULT_USER_AGENT
    asr_window_seconds: float = 15.0
    asr_overlap_seconds: float = 5.0
    asr_decode_timeout_seconds: float = 45.0
    asr_queue_size: int = 12


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
            streams = payload["data"]["playurl_info"]["playurl"]["stream"]
        except Exception:
            return None

        for stream in streams:
            for fmt in stream.get("format", []):
                for codec in fmt.get("codec", []):
                    base_url = codec.get("base_url")
                    url_info = codec.get("url_info") or []
                    if not base_url or not url_info:
                        continue
                    for info in url_info:
                        host = info.get("host")
                        extra = info.get("extra")
                        if host and extra:
                            return f"{host}{base_url}{extra}"

        return None


class LiveCapture:
    def __init__(self, config: LiveCaptureConfig):
        self._config = config
        self._resolver = BilibiliLiveStreamResolver(
            user_agent=config.user_agent,
            cookies=config.cookies,
        )
        self._processes: dict[int, subprocess.Popen] = {}
        self._last_cmd_by_room: dict[int, str] = {}
        self._last_restart_at: dict[int, float] = {}
        
        self._stderr_threads: dict[int, threading.Thread] = {}
        self._stdout_threads: dict[int, threading.Thread] = {}
        
        self._force_restart_at: dict[int, float] = {}
        self._restart_backoff: dict[int, float] = {}
        self._next_start_at: dict[int, float] = {}
        self._live_start_at: dict[int, float] = {}
        self._last_audio_at: dict[int, float] = {}
        self._lock = threading.Lock()
        
        init_transcript_db()
        self._asr_engine = SenseVoiceEngine(
            window_seconds=config.asr_window_seconds,
            overlap_seconds=config.asr_overlap_seconds,
            decode_timeout_seconds=config.asr_decode_timeout_seconds,
            queue_size=config.asr_queue_size,
        )

    def _build_ffmpeg_command(self, url: str) -> list[str]:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is not available in PATH")

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
        
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            # ffmpeg expects this network read timeout in microseconds.
            "-rw_timeout", "15000000",
            "-headers", headers_value,
            "-i", url,
            "-map", "0:a:0",
            "-vn",
            "-c:a", "pcm_s16le",
            "-f", "s16le",
            "-ar", "16000",
            "-ac", "1",
            "pipe:1"
        ]
        
        return cmd

    def _start_capture(self, room_id: int) -> None:
        if room_id in self._processes:
            return
        now = time.monotonic()
        next_allowed = self._next_start_at.get(room_id, 0.0)
        if now < next_allowed:
            wait_seconds = max(0.0, next_allowed - now)
            logger.warning("skip start for room_id=%s, backoff %.1fs", room_id, wait_seconds)
            return
            
        url = self._resolver.get_stream_url(str(room_id), qn=self._config.qn)
        if not url and self._config.qn > 150:
            url = self._resolver.get_stream_url(str(room_id), qn=150)
        if not url:
            logger.warning("failed to resolve live stream url for room_id=%s", room_id)
            self._register_failure(room_id, "resolve url failed")
            return
            
        try:
            command = self._build_ffmpeg_command(url)
        except RuntimeError as exc:
            logger.error("ffmpeg unavailable for room_id=%s: %s", room_id, exc)
            self._register_failure(room_id, "ffmpeg unavailable")
            return
            
        self._processes[room_id] = self._spawn_ffmpeg(room_id, command)
        self._last_restart_at[room_id] = time.monotonic()
        self._last_audio_at[room_id] = time.monotonic()
        self._restart_backoff.setdefault(room_id, BASE_RESTART_BACKOFF)
        logger.info("started ffmpeg capture & stream for room_id=%s", room_id)

    def _stop_capture(self, room_id: int) -> None:
        process = self._processes.pop(room_id, None)
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            
        self._last_restart_at.pop(room_id, None)
        self._stderr_threads.pop(room_id, None)
        self._stdout_threads.pop(room_id, None)
        self._force_restart_at.pop(room_id, None)
        self._next_start_at.pop(room_id, None)
        self._last_audio_at.pop(room_id, None)
        logger.info("stopped ffmpeg capture for room_id=%s", room_id)

    def _spawn_ffmpeg(self, room_id: int, command: list[str]) -> subprocess.Popen:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**6
        )
        
        stderr_thread = threading.Thread(
            target=self._stream_ffmpeg_stderr,
            args=(room_id, process),
            daemon=True,
        )
        stderr_thread.start()
        self._stderr_threads[room_id] = stderr_thread
        
        stdout_thread = threading.Thread(
            target=self._stream_ffmpeg_stdout_audio,
            args=(room_id, process),
            daemon=True,
        )
        stdout_thread.start()
        self._stdout_threads[room_id] = stdout_thread
        
        return process

    def _stream_ffmpeg_stderr(self, room_id: int, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        try:
            for line_bytes in process.stderr:
                try:
                    text = line_bytes.decode('utf-8', errors='replace').rstrip()
                    if text:
                        if "Error" in text or "warning" in text.lower():
                            pass # 忽略常规的音视频解码警告
                        if RETRYABLE_HTTP_ERRORS.search(text):
                            self._force_restart(room_id, process, reason=text)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("ffmpeg[%s] stderr stream stopped: %s", room_id, exc)
        finally:
            try:
                process.stderr.close()
            except Exception:
                pass

    def _stream_ffmpeg_stdout_audio(self, room_id: int, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
            
        # Keep fixed segmentation in the reader; native inference runs elsewhere.
        room_stream = self._asr_engine.create_room_stream()
        
        # 0.25 seconds of mono, signed 16-bit, 16 kHz PCM.
        CHUNK_SIZE = 8000 
        
        try:
            while True:
                raw_bytes = process.stdout.read(CHUNK_SIZE)
                if not raw_bytes:
                    break

                if self._processes.get(room_id) is process:
                    self._last_audio_at[room_id] = time.monotonic()

                for audio_window in room_stream.process_audio_chunk(raw_bytes):
                    self._asr_engine.submit(
                        room_id=room_id,
                        raw_pcm_bytes=audio_window,
                        captured_at=int(time.time()),
                    )
        except Exception as exc:
            logger.warning("ffmpeg[%s] stdout error: %s", room_id, exc)
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass

    def _force_restart(self, room_id: int, process: subprocess.Popen, reason: str) -> None:
        now = time.monotonic()
        with self._lock:
            last_forced = self._force_restart_at.get(room_id, 0.0)
            if now - last_forced < 5:
                return
            self._force_restart_at[room_id] = now
        logger.warning("ffmpeg[%s] forcing restart due to error: %s", room_id, reason)
        try:
            process.terminate()
        except Exception:
            pass

    def _register_failure(self, room_id: int, reason: str) -> None:
        now = time.monotonic()
        live_started_at = self._live_start_at.get(room_id)
        in_grace = live_started_at is not None and (now - live_started_at) < LIVE_GRACE_SECONDS

        if in_grace:
            backoff = BASE_RESTART_BACKOFF
        else:
            backoff = self._restart_backoff.get(room_id, BASE_RESTART_BACKOFF)
            backoff = min(backoff * 2, MAX_RESTART_BACKOFF) if backoff > 0 else BASE_RESTART_BACKOFF

        self._restart_backoff[room_id] = backoff
        self._next_start_at[room_id] = now + backoff
        if in_grace:
            logger.warning("room_id=%s retrying in %.1fs (live grace) due to %s", room_id, backoff, reason)
        else:
            logger.warning("room_id=%s backoff %.1fs due to %s", room_id, backoff, reason)

    def _restart_if_needed(self) -> None:
        now = time.monotonic()
        
        # 1. 检查并清理已经退出的进程
        for room_id, process in list(self._processes.items()):
            if process.poll() is None:
                last_audio_at = self._last_audio_at.get(room_id, now)
                if now - last_audio_at > AUDIO_STALL_SECONDS:
                    self._force_restart(
                        room_id,
                        process,
                        reason=f"no audio for {now - last_audio_at:.1f}s",
                    )
                    continue

                # 进程仍在运行，如果稳定运行超过 60 秒，重置退避时间
                last_restart = self._last_restart_at.get(room_id, 0.0)
                if now - last_restart > 60:
                    self._restart_backoff[room_id] = BASE_RESTART_BACKOFF
                continue
            
            # 进程已经退出，移出进程字典
            self._processes.pop(room_id, None)
            
            # 判断是否为快速失败
            if self._last_cmd_by_room.get(room_id) == "LIVE":
                last_restart = self._last_restart_at.get(room_id, 0.0)
                if now - last_restart < FAST_FAIL_SECONDS:
                    self._register_failure(room_id, "ffmpeg exited quickly")
                    
        # 2. 根据状态拉起缺少的进程
        for room_id, cmd in self._last_cmd_by_room.items():
            if cmd == "LIVE" and room_id not in self._processes:
                # _start_capture 内部有 now < next_allowed 的判断
                # 如果还在 backoff 冷却期内，它会自动打印 skip start 并 return，不会重复拉起
                self._start_capture(room_id)

    def _poll_asr(self) -> None:
        for result in self._asr_engine.poll():
            if result.error:
                logger.error("ASR[%s] inference failed: %s", result.room_id, result.error)
                continue
            if not result.text:
                continue
            logger.info(
                "ASR[%s] (%.2fs): %s",
                result.room_id,
                result.inference_seconds,
                result.text,
            )
            try:
                insert_transcript(
                    room_id=result.room_id,
                    content=result.text,
                    timestamp=result.captured_at,
                )
            except Exception as exc:
                logger.error("ASR[%s] database write failed: %s", result.room_id, exc)

    def _fetch_events_since(self, last_id: int) -> list[dict[str, Any]]:
        return list_live_events_after(self._config.room_ids, last_id)

    def run(self) -> int:
        logger.info("capture service starting")

        
        last_id = get_latest_live_event_id(self._config.room_ids)
        for room_id in self._config.room_ids:
            latest_cmd = get_latest_live_cmd(room_id)
            if latest_cmd:
                self._last_cmd_by_room[room_id] = latest_cmd
                if latest_cmd == "LIVE":
                    self._live_start_at[room_id] = time.monotonic()
                    self._start_capture(room_id)
        try:
            while True:
                self._poll_asr()
                rows = self._fetch_events_since(last_id)
                if not rows:
                    self._restart_if_needed()
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
                        self._live_start_at[room_id] = time.monotonic()
                        self._start_capture(room_id)
                    elif cmd == "PREPARING":
                        self._last_cmd_by_room[room_id] = "PREPARING"
                        self._live_start_at.pop(room_id, None)
                        self._stop_capture(room_id)
                self._restart_if_needed()
        except KeyboardInterrupt:
            pass
        finally:
            for room_id in list(self._processes.keys()):
                self._stop_capture(room_id)
            self._asr_engine.shutdown()
        logger.info("capture service stopped")

        return 0

    def _monitor_frames(self, room_id: int) -> None:
        frame_dir = self._resolve_output_root() / "frames" / str(room_id)
        
        # 创建一个专属的子目录用于存放正在处理的图片
        processing_dir = frame_dir / "processing"

        while getattr(self, "_monitor_running", True):
            if not frame_dir.exists():
                time.sleep(2)
                continue

            # 确保 processing 目录存在
            processing_dir.mkdir(parents=True, exist_ok=True)

            try:
                for file_path in frame_dir.glob("frame_*.jpg"):
                    if time.time() - file_path.stat().st_mtime > 2.0:
                        # 移动到 processing 子目录，不改变原有 .jpg 后缀
                        processing_path = processing_dir / file_path.name
                        file_path.rename(processing_path)

                        # 派发任务，传入新的路径
                        future = self._ocr_pool.submit_frame(str(processing_path), room_id)
                        future.add_done_callback(self._on_ocr_done)
            except Exception as e:
                pass 

            time.sleep(2)

    def _on_ocr_done(self, future) -> None:
        try:
            # 接收返回的真实帧时间戳
            room_id, image_path, texts, frame_timestamp = future.result()
            
            if texts:
                logger.info("INFO - OCR[%s]: %s", room_id, texts)
                insert_ocr_record(
                    room_id=room_id,
                    content=texts,
                    timestamp=frame_timestamp, # 使用文件名解析出的时间
                )
        except Exception as exc:
            logger.error("OCR callback error: %s", exc)
        finally:
            if 'image_path' in locals() and os.path.exists(image_path):
                _trash(image_path)


def _parse_args(argv: list[str]) -> LiveCaptureConfig:
    parser = argparse.ArgumentParser(description="Capture Bilibili live streaming with real-time ASR (SenseVoice)")
    parser.add_argument("--qn", type=int, default=150, help="quality level, default 150")
    parser.add_argument("--asr-window", type=float, default=15.0)
    parser.add_argument("--asr-overlap", type=float, default=5.0)
    parser.add_argument("--asr-timeout", type=float, default=45.0)
    parser.add_argument("--asr-queue-size", type=int, default=12)

    args = parser.parse_args(argv)
    room_ids = list_asr_enabled_room_ids()
    if not room_ids:
        raise ValueError("subscription 表为空，无法获取直播间房间号")

    return LiveCaptureConfig(
        room_ids=room_ids,
        qn=args.qn,
        cookies=build_cookies(),
        asr_window_seconds=args.asr_window,
        asr_overlap_seconds=args.asr_overlap,
        asr_decode_timeout_seconds=args.asr_timeout,
        asr_queue_size=args.asr_queue_size,
    )


def _trash(file_path: str) -> None:
    try:
        subprocess.run(["trash-put", file_path], check=True)
        return
    except Exception as exc:
        logger.warning("failed to move %s to trash: %s", file_path, exc)


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    config = _parse_args(argv or sys.argv[1:])
    capture = LiveCapture(config)
    return capture.run()


if __name__ == "__main__":
    raise SystemExit(main())
