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

        logger.info("Initializing SenseVoice Engine in Overlap-Sewing Mode...")

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=1,
            use_itn=True,
            language="",
        )

        self.vad_config = sherpa_onnx.VadModelConfig()
        self.vad_config.silero_vad.model = str(vad_model_path)
        
        # 针对唱歌环境优化的 VAD 参数
        # 降低门槛至 0.2，防止被大声 BGM 掩盖的微弱人声被忽略
        self.vad_config.silero_vad.threshold = 0.2
        # 极短触发，哪怕只唱了一个 "I" 也能抓到
        self.vad_config.silero_vad.min_speech_duration = 0.1
        # 极速断句，捕捉唱歌时零点几秒的微弱换气
        self.vad_config.silero_vad.min_silence_duration = 0.3
        
        # 底层安全阈值：如果 10 秒钟都没停顿，让底层 VAD 自动安全切断（代替危险的 flush）
        if hasattr(self.vad_config.silero_vad, "max_speech_duration"):
            self.vad_config.silero_vad.max_speech_duration = 10.0
            
        self.vad_config.sample_rate = 16000

        logger.info("SenseVoice Engine Initialized.")

    def create_room_stream(self) -> RoomAudioStream:
        return RoomAudioStream(self.recognizer, self.vad_config)


class RoomAudioStream:
    def __init__(self, recognizer: sherpa_onnx.OfflineRecognizer, vad_config: sherpa_onnx.VadModelConfig):
        self.recognizer = recognizer
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=60)
        self._sample_rate = 16000
        
        # 智能胶水桶
        self._acc_audio = np.zeros(0, dtype=np.float32)
        self._silence_timer = 0.0

    def _vad_is_empty(self) -> bool:
        empty_attr = self.vad.empty
        return empty_attr() if callable(empty_attr) else bool(empty_attr)

    def process_audio_chunk(self, raw_pcm_bytes: bytes) -> list[str]:
        results = []
        try:
            samples = np.frombuffer(raw_pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            
            # 1. 喂入音频流
            self.vad.accept_waveform(samples)
            
            # 2. 如果桶里已经有声音了，开始计算主播闭嘴的时间
            if self._acc_audio.size > 0:
                self._silence_timer += len(samples) / self._sample_rate

            # 3. 收集 VAD 切出来的干净小段
            while not self._vad_is_empty():
                front_attr = self.vad.front
                segment = front_attr() if callable(front_attr) else front_attr
                
                self._acc_audio = np.concatenate([self._acc_audio, segment.samples])
                self._silence_timer = 0.0  # 只要 VAD 吐出新声音，静音计时清零
                self.vad.pop()

            # 4. 判断是否发送给 SenseVoice
            acc_duration = self._acc_audio.size / self._sample_rate
            
            # 触发条件 A：歌手真正停顿/换气超过 0.8 秒，这是一个完美自然的断句点
            is_natural_pause = (acc_duration > 0 and self._silence_timer >= 0.8)
            
            # 触发条件 B：长高潮连续不断，桶里积压超过 12 秒，强制送去识别以防延迟过高
            is_forced_cut = (acc_duration >= 12.0)

            if is_natural_pause or is_forced_cut:
                # 边缘静音包裹
                pad = np.zeros(int(0.5 * self._sample_rate), dtype=np.float32)
                final_audio = np.concatenate([pad, self._acc_audio, pad])

                stream = self.recognizer.create_stream()
                stream.accept_waveform(self._sample_rate, final_audio)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                
                clean_text = self._clean_sense_voice_tags(text)
                
                if clean_text and not re.fullmatch(r'[^\w\s]+', clean_text):
                    results.append(clean_text)
                
                # --- 最核心的修复逻辑：重叠缝合 ---
                if is_forced_cut:
                    # 如果是被迫切断的，说明有可能切在了一个词的中间。
                    # 我们把最后的 1.5 秒保留下来，作为下一个片段的开头，保证没有声音被丢弃！
                    keep_len = int(1.5 * self._sample_rate)
                    if self._acc_audio.size > keep_len:
                        self._acc_audio = self._acc_audio[-keep_len:]
                    else:
                        self._acc_audio = np.zeros(0, dtype=np.float32)
                else:
                    # 如果是自然停顿，说明这句话已经说干净了，清空胶水桶
                    self._acc_audio = np.zeros(0, dtype=np.float32)
                    
                self._silence_timer = 0.0

        except Exception as exc:
            logger.error(f"VAD chunk process failed: {exc}", exc_info=True)
            
        return results

    def _clean_sense_voice_tags(self, text: str) -> str:
        return re.sub(r'<\|.*?\|>', '', text).strip()