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

        logger.info("Initializing SenseVoice Engine in RMS-Volume Hard-Cut Mode...")

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
        
        # 物理切分核心参数（可根据实际直播间情况微调）
        self.chunk_duration_limit = 15.0  # 极限长度：满 15 秒无论如何强制送去识别，防止延迟过高
        self.volume_threshold = 0.005    # 底噪门槛：低于此音量视为没声音（0.005 适用于一般直播间）
        self.silence_timer = 0.0
        self.max_silence = 1.0           # 换气判定：连续安静 1 秒，视为一句话结束，提前送去识别

    def _calculate_rms(self, samples: np.ndarray) -> float:
        """计算音频片段的平均音量"""
        if len(samples) == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples**2)))

    def process_audio_chunk(self, raw_pcm_bytes: bytes) -> list[str]:
        results = []
        try:
            # 你的 capture.py 每次传进来的是 8000 bytes (0.25秒)
            samples = np.frombuffer(raw_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            
            current_volume = self._calculate_rms(samples)
            
            # 无条件装进胶水桶
            self._acc_audio = np.concatenate([self._acc_audio, samples])
            acc_duration = len(self._acc_audio) / self._sample_rate
            
            # 判断这 0.25 秒是静音还是有声音
            if current_volume < self.volume_threshold:
                self.silence_timer += (len(samples) / self._sample_rate)
            else:
                self.silence_timer = 0.0
                
            # 触发条件 A：歌手换气/间奏停顿了 1 秒，且桶里至少有 0.5 秒的有效音频
            is_natural_pause = (self.silence_timer >= self.max_silence and acc_duration > 0.5)
            # 触发条件 B：高潮部分一直唱没有停顿，桶里积压到了 15 秒
            is_forced_cut = (acc_duration >= self.chunk_duration_limit)
            
            if is_natural_pause or is_forced_cut:
                # 前后垫入 0.1 秒的绝对静音，这能有效防止 SenseVoice 吞掉首尾的辅音
                pad = np.zeros(int(0.1 * self._sample_rate), dtype=np.float32)
                final_audio = np.concatenate([pad, self._acc_audio, pad])

                # 直接识别
                stream = self.recognizer.create_stream()
                stream.accept_waveform(self._sample_rate, final_audio)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                
                clean_text = self._clean_sense_voice_tags(text)
                
                # 过滤纯标点符号和超短句
                if clean_text and not re.fullmatch(r'[^\w\s]+', clean_text) and not len(clean_text) <= 4:
                    results.append(clean_text)
                
                # 清空桶和计时器，准备接收下一句
                self._acc_audio = np.zeros(0, dtype=np.float32)
                self.silence_timer = 0.0

        except Exception as exc:
            logger.error(f"RMS chunk process failed: {exc}", exc_info=True)
            
        return results

    def _clean_sense_voice_tags(self, text: str) -> str:
        return re.sub(r'<\|.*?\|>', '', text).strip()