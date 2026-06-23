from __future__ import annotations

import os
import logging
import re
from pathlib import Path
import numpy as np

import sherpa_onnx

logger = logging.getLogger("capture")

class SenseVoiceEngine:
    def __init__(self):
        root = Path(__file__).resolve().parents[2]
        model_dir = root / "models" / "sherpa" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

        if not os.path.exists(model_dir):
            raise FileNotFoundError(f"SenseVoice model dir not found at {model_dir}")

        logger.info("Initializing SenseVoice Engine in 15-Sec Fixed-Length Mode...")

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=1,
            use_itn=True,
            language="",
        )
        logger.info("SenseVoice Engine Initialized.")

    def create_room_stream(self) -> SimpleAudioStream:
        return SimpleAudioStream(self.recognizer)


class SimpleAudioStream:
    def __init__(self, recognizer: sherpa_onnx.OfflineRecognizer):
        self.recognizer = recognizer
        self._sample_rate = 16000
        
        self._acc_audio = np.zeros(0, dtype=np.float32)
        
        # 定义严格的15秒采样点数量 (15 * 16000 = 240000)
        self._target_samples = 15 * self._sample_rate
        
        # 预先生成0.1秒的静音垫片
        self._pad = np.zeros(int(0.1 * self._sample_rate), dtype=np.float32)

    def process_audio_chunk(self, raw_pcm_bytes: bytes) -> list[str]:
        results = []
        try:
            # 转换 capture.py 传进来的原始字节
            samples = np.frombuffer(raw_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            
            # 追加到缓冲区
            self._acc_audio = np.concatenate([self._acc_audio, samples])
            
            # 只要缓冲区达到或超过 15 秒，就立刻切片识别
            while len(self._acc_audio) >= self._target_samples:
                
                # 截取精确的 15 秒数据
                chunk_to_process = self._acc_audio[:self._target_samples]
                
                # 剩余的零头保留在缓冲区里，等待下一次拼接
                self._acc_audio = self._acc_audio[self._target_samples:]
                
                # 前后垫入0.1秒静音，防止SenseVoice吞掉首尾发音
                final_audio = np.concatenate([self._pad, chunk_to_process, self._pad])

                # 直接送入识别
                stream = self.recognizer.create_stream()
                stream.accept_waveform(self._sample_rate, final_audio)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                
                clean_text = self._clean_sense_voice_tags(text)
                results.append(clean_text)

        except Exception as exc:
            logger.error(f"Fixed chunk process failed: {exc}", exc_info=True)
            
        return results

    def _clean_sense_voice_tags(self, text: str) -> str:
        text = re.sub(r'<\|.*?\|>', '', text).strip()
        text = re.sub(r'([^\x00-\x7F])\s+', r'\1', text)
        text = re.sub(r'\s+([^\x00-\x7F])', r'\1', text)
        return text.strip()