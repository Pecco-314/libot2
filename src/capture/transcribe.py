from __future__ import annotations

import os
import logging
from pathlib import Path
import numpy as np
import re

import sherpa_onnx

logger = logging.getLogger("capture")

class SenseVoiceEngine:
    def __init__(self):
        root = Path(__file__).resolve().parents[2]
        model_dir = root / "models" / "sherpa" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
        vad_model_path = root / "models" / "sherpa" / "silero_vad.onnx"

        if not os.path.exists(vad_model_path):
            raise FileNotFoundError(f"VAD model not found at {vad_model_path}")
        if not os.path.exists(model_dir):
            raise FileNotFoundError(f"SenseVoice model dir not found at {model_dir}")

        logger.info("Initializing SenseVoice + VAD Engine in Big-Buffer Mode...")

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=1,
            use_itn=True,
            language="",
        )

        self.vad_config = sherpa_onnx.VadModelConfig()
        self.vad_config.silero_vad.model = str(vad_model_path)
        self.vad_config.silero_vad.threshold = 0.15
        self.vad_config.silero_vad.min_speech_duration = 0.4
        self.vad_config.silero_vad.min_silence_duration = 1.5
        self.vad_config.silero_vad.max_speech_duration = 15.0
            
        self.vad_config.sample_rate = 16000

        logger.info("SenseVoice Engine Initialized.")

    def create_room_stream(self) -> RoomAudioStream:
        return RoomAudioStream(self.recognizer, self.vad_config)


class RoomAudioStream:
    def __init__(self, recognizer: sherpa_onnx.OfflineRecognizer, vad_config: sherpa_onnx.VadModelConfig):
        self.recognizer = recognizer
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=60)
        self._sample_rate = 16000
        
        # 用于兜底释放一直挂起但不触发静音阈值的极长音频
        self._pending_seconds = 0.0
        self._max_pending_seconds = 20.0

    def _vad_is_empty(self) -> bool:
        empty_attr = self.vad.empty
        return empty_attr() if callable(empty_attr) else bool(empty_attr)

    def process_audio_chunk(self, raw_pcm_bytes: bytes) -> list[str]:
        results = []
        try:
            samples = np.frombuffer(raw_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            self.vad.accept_waveform(samples)
            self._pending_seconds += len(samples) / self._sample_rate

            if self._pending_seconds >= self._max_pending_seconds and hasattr(self.vad, "flush"):
                try:
                    self.vad.flush()
                except Exception:
                    pass

            while not self._vad_is_empty():
                front_attr = self.vad.front
                segment = front_attr() if callable(front_attr) else front_attr
                audio = segment.samples
                
                # 简单粗暴的物理静音填充（首尾各 0.5 秒纯零）
                # 既解决了 SenseVoice 开头丢字的边缘效应，也保证了短句能达标
                pad = np.zeros(int(0.5 * self._sample_rate), dtype=np.float32)
                final_audio = np.concatenate([pad, audio, pad])

                stream = self.recognizer.create_stream()
                stream.accept_waveform(self._sample_rate, final_audio)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                
                clean_text = self._clean_sense_voice_tags(text)
                
                # 唯一的过滤逻辑：如果全是大段音乐或杂音，SenseVoice 会输出空字符串，直接过滤掉即可
                if clean_text and not re.fullmatch(r'[^\w\s]+', clean_text):
                    results.append(clean_text)
                
                self.vad.pop()
                self._pending_seconds = 0.0

        except Exception as exc:
            logger.error(f"VAD chunk process failed: {exc}", exc_info=True)
            
        return results

    def _clean_sense_voice_tags(self, text: str) -> str:
        # 去除模型自带的 <|zh|><|NEUTRAL|> 等标签
        return re.sub(r'<\|.*?\|>', '', text).strip()