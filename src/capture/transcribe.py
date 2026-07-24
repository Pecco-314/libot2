from __future__ import annotations

import itertools
import logging
import multiprocessing
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import sherpa_onnx


logger = logging.getLogger("capture")

SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
DEFAULT_WINDOW_SECONDS = 15.0
DEFAULT_OVERLAP_SECONDS = 5.0
DEFAULT_DECODE_TIMEOUT_SECONDS = 45.0
DEFAULT_QUEUE_SIZE = 12


def _clean_sense_voice_tags(text: str) -> str:
    text = re.sub(r"<\|.*?\|>", "", text).strip()
    text = re.sub(r"([^\x00-\x7F])\s+", r"\1", text)
    text = re.sub(r"\s+([^\x00-\x7F])", r"\1", text)
    return text.strip()


class FixedWindowAudioStream:
    """Split PCM into fixed, overlapping windows without using VAD."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
    ):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if overlap_seconds < 0 or overlap_seconds >= window_seconds:
            raise ValueError("overlap_seconds must be in [0, window_seconds)")

        self._window_bytes = int(window_seconds * sample_rate) * SAMPLE_WIDTH_BYTES
        hop_seconds = window_seconds - overlap_seconds
        self._hop_bytes = int(hop_seconds * sample_rate) * SAMPLE_WIDTH_BYTES
        if self._window_bytes <= 0 or self._hop_bytes <= 0:
            raise ValueError("window and hop must contain at least one sample")
        self._buffer = bytearray()

    def process_audio_chunk(self, raw_pcm_bytes: bytes) -> list[bytes]:
        if not raw_pcm_bytes:
            return []

        self._buffer.extend(raw_pcm_bytes)
        windows: list[bytes] = []
        while len(self._buffer) >= self._window_bytes:
            windows.append(bytes(self._buffer[: self._window_bytes]))
            del self._buffer[: self._hop_bytes]
        return windows

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    room_id: int
    captured_at: int
    text: str
    inference_seconds: float
    error: str | None = None


def _create_recognizer(model_dir: Path) -> sherpa_onnx.OfflineRecognizer:
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_dir / "model.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=1,
        use_itn=True,
        language="",
    )


def _decode_pcm(
    recognizer: sherpa_onnx.OfflineRecognizer,
    raw_pcm_bytes: bytes,
) -> str:
    samples = (
        np.frombuffer(raw_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    )
    pad = np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32)
    final_audio = np.concatenate((pad, samples, pad))

    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, final_audio)
    recognizer.decode_stream(stream)
    return _clean_sense_voice_tags(stream.result.text)


def _recognition_worker(
    model_dir: str,
    generation: int,
    task_queue: Any,
    event_queue: Any,
) -> None:
    try:
        recognizer = _create_recognizer(Path(model_dir))
    except BaseException as exc:
        event_queue.put(("fatal", generation, f"{type(exc).__name__}: {exc}"))
        return

    event_queue.put(("ready", generation, os.getpid()))
    while True:
        task = task_queue.get()
        if task is None:
            return

        task_id, room_id, captured_at, raw_pcm_bytes = task
        started_at = time.monotonic()
        event_queue.put(("started", generation, task_id, started_at))
        try:
            text = _decode_pcm(recognizer, raw_pcm_bytes)
            error = None
        except BaseException as exc:
            text = ""
            error = f"{type(exc).__name__}: {exc}"

        event_queue.put(
            (
                "result",
                generation,
                task_id,
                room_id,
                captured_at,
                text,
                time.monotonic() - started_at,
                error,
            )
        )


class SenseVoiceEngine:
    """
    Run native SenseVoice inference in a supervised child process.

    sherpa-onnx inference is intentionally kept out of ffmpeg reader threads:
    a blocked native call can then be killed and restarted without stalling the
    audio pipe or every other room.
    """

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        overlap_seconds: float = DEFAULT_OVERLAP_SECONDS,
        decode_timeout_seconds: float = DEFAULT_DECODE_TIMEOUT_SECONDS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        startup_timeout_seconds: float = 120.0,
        model_dir: Path | None = None,
    ):
        root = Path(__file__).resolve().parents[2]
        self._model_dir = model_dir or (
            root
            / "models"
            / "sherpa"
            / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
        )
        if not self._model_dir.exists():
            raise FileNotFoundError(
                f"SenseVoice model dir not found at {self._model_dir}"
            )
        if decode_timeout_seconds <= 0:
            raise ValueError("decode_timeout_seconds must be positive")
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")

        FixedWindowAudioStream(
            window_seconds=window_seconds,
            overlap_seconds=overlap_seconds,
        )

        self._window_seconds = window_seconds
        self._overlap_seconds = overlap_seconds
        self._decode_timeout_seconds = decode_timeout_seconds
        self._queue_size = queue_size
        self._startup_timeout_seconds = startup_timeout_seconds
        self._context = multiprocessing.get_context("spawn")
        self._lifecycle_lock = threading.Lock()
        self._task_ids = itertools.count(1)
        self._generation = 0
        self._process: multiprocessing.Process | None = None
        self._task_queue: Any | None = None
        self._event_queue: Any | None = None
        self._ready = False
        self._current_task_id: int | None = None
        self._current_task_started_at: float | None = None
        self._dropped_tasks = 0
        self._closed = False
        self._worker_launched_at = 0.0

        logger.info(
            "initializing SenseVoice worker (fixed %.1fs window, %.1fs overlap)",
            window_seconds,
            overlap_seconds,
        )
        self._start_worker()
        self._wait_until_ready(startup_timeout_seconds)
        logger.info("SenseVoice worker initialized")

    def create_room_stream(self) -> FixedWindowAudioStream:
        return FixedWindowAudioStream(
            window_seconds=self._window_seconds,
            overlap_seconds=self._overlap_seconds,
        )

    def submit(
        self,
        *,
        room_id: int,
        raw_pcm_bytes: bytes,
        captured_at: int,
    ) -> bool:
        if self._closed or not raw_pcm_bytes:
            return False

        task_queue = self._task_queue
        if task_queue is None:
            self._dropped_tasks += 1
            return False

        task = (
            next(self._task_ids),
            room_id,
            captured_at,
            raw_pcm_bytes,
        )
        try:
            task_queue.put_nowait(task)
            return True
        except (OSError, ValueError):
            self._dropped_tasks += 1
            return False
        except queue.Full:
            # Prefer current audio over stale backlog. Never wait here: blocking
            # this call would eventually block ffmpeg's stdout pipe as well.
            try:
                task_queue.get_nowait()
            except (OSError, ValueError, queue.Empty):
                pass
            try:
                task_queue.put_nowait(task)
                self._dropped_tasks += 1
                logger.warning(
                    "ASR queue full; discarded oldest window (dropped=%s)",
                    self._dropped_tasks,
                )
                return True
            except (OSError, ValueError):
                self._dropped_tasks += 1
                return False
            except queue.Full:
                self._dropped_tasks += 1
                logger.warning(
                    "ASR queue full; discarded new window for room_id=%s "
                    "(dropped=%s)",
                    room_id,
                    self._dropped_tasks,
                )
                return False

    def poll(self) -> list[RecognitionResult]:
        if self._closed:
            return []

        results = self._drain_events()
        process = self._process
        if process is None or not process.is_alive():
            self._restart_worker("worker exited unexpectedly")
            return results

        if (
            not self._ready
            and time.monotonic() - self._worker_launched_at
            > self._startup_timeout_seconds
        ):
            self._restart_worker("worker initialization timed out")
            return results

        started_at = self._current_task_started_at
        if (
            started_at is not None
            and time.monotonic() - started_at > self._decode_timeout_seconds
        ):
            elapsed = time.monotonic() - started_at
            self._restart_worker(
                f"decode task {self._current_task_id} exceeded "
                f"{self._decode_timeout_seconds:.1f}s (elapsed={elapsed:.1f}s)"
            )
        return results

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def dropped_tasks(self) -> int:
        return self._dropped_tasks

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_worker(graceful=True)

    def _start_worker(self) -> None:
        self._worker_launched_at = time.monotonic()
        self._generation += 1
        self._task_queue = self._context.Queue(maxsize=self._queue_size)
        self._event_queue = self._context.Queue()
        self._ready = False
        self._current_task_id = None
        self._current_task_started_at = None
        self._process = self._context.Process(
            target=_recognition_worker,
            args=(
                str(self._model_dir),
                self._generation,
                self._task_queue,
                self._event_queue,
            ),
            name="sensevoice-worker",
            daemon=True,
        )
        self._process.start()

    def _wait_until_ready(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._drain_events()
            if self._ready:
                return
            if self._process is None or not self._process.is_alive():
                self._stop_worker(graceful=False)
                raise RuntimeError("SenseVoice worker exited during initialization")
            time.sleep(0.05)

        self._stop_worker(graceful=False)
        raise TimeoutError(
            f"SenseVoice worker did not initialize within {timeout_seconds:.1f}s"
        )

    def _drain_events(self) -> list[RecognitionResult]:
        results: list[RecognitionResult] = []
        event_queue = self._event_queue
        if event_queue is None:
            return results

        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                break

            event_type, generation, *payload = event
            if generation != self._generation:
                continue
            if event_type == "ready":
                self._ready = True
                logger.info(
                    "SenseVoice worker ready (pid=%s, generation=%s)",
                    payload[0],
                    generation,
                )
            elif event_type == "started":
                self._current_task_id = int(payload[0])
                self._current_task_started_at = float(payload[1])
            elif event_type == "result":
                (
                    task_id,
                    room_id,
                    captured_at,
                    text,
                    inference_seconds,
                    error,
                ) = payload
                if task_id == self._current_task_id:
                    self._current_task_id = None
                    self._current_task_started_at = None
                results.append(
                    RecognitionResult(
                        room_id=int(room_id),
                        captured_at=int(captured_at),
                        text=str(text),
                        inference_seconds=float(inference_seconds),
                        error=str(error) if error else None,
                    )
                )
            elif event_type == "fatal":
                logger.error("SenseVoice worker initialization failed: %s", payload[0])
        return results

    def _restart_worker(self, reason: str) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            logger.error("restarting SenseVoice worker: %s", reason)
            self._stop_worker(graceful=False)
            self._start_worker()

    def _stop_worker(self, *, graceful: bool) -> None:
        process = self._process
        task_queue = self._task_queue

        if graceful and process is not None and process.is_alive():
            try:
                task_queue.put_nowait(None)
            except (AttributeError, queue.Full):
                pass
            process.join(timeout=5)

        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process is not None and process.is_alive():
            process.kill()
            process.join(timeout=2)

        for mp_queue in (self._task_queue, self._event_queue):
            if mp_queue is None:
                continue
            try:
                mp_queue.cancel_join_thread()
                mp_queue.close()
            except (OSError, ValueError):
                pass

        self._process = None
        self._task_queue = None
        self._event_queue = None
        self._ready = False
        self._current_task_id = None
        self._current_task_started_at = None

    def __enter__(self) -> SenseVoiceEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
