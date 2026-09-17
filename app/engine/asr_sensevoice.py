"""
SenseVoice 离线引擎
模型: sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09（OfflineRecognizer）
特点: 电话/噪声鲁棒，准确率高；离线：收完整句音频一次性转写
接口: transcribe(audio_data, 8000) 一次性；流式接口抛 NotImplementedError
"""
import logging

import numpy as np

from engine.asr import (
    BaseASR,
    _MODEL_DIR,
    MODEL_SAMPLE_RATE,
    LINE_SAMPLE_RATE,
    _int16_bytes_to_float32,
    _resample,
)

logger = logging.getLogger("ai-backend.asr")

# 离线转写：尾部补 0.3s 静音，确保句尾语音被解码完整（防"功能"漏成"功"）
_TAIL_SILENCE_MS = 300


class SenseVoiceASR(BaseASR):
    """SenseVoice 离线识别引擎（sherpa-onnx OfflineRecognizer.from_sense_voice）。"""

    def __init__(self, config: dict):
        import sherpa_onnx

        model_dir = config.get("model_dir", "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR
        model_path = base / "model.int8.onnx"
        tokens_path = base / "tokens.txt"
        num_threads = int(config.get("threads", 4))
        self.sample_rate = MODEL_SAMPLE_RATE

        if not model_path.exists():
            raise FileNotFoundError(f"ASR(sensevoice) 模型文件不存在: {model_path}")
        if not tokens_path.exists():
            raise FileNotFoundError(f"ASR(sensevoice) tokens 文件不存在: {tokens_path}")

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            num_threads=num_threads,
            provider="cpu",
        )
        logger.info(
            f"ASR 引擎初始化完成 (sensevoice) | model={model_path.name} | "
            f"threads={num_threads} | is_streaming=False"
        )

    @property
    def is_streaming(self) -> bool:
        return False

    def transcribe(self, audio_data: bytes, sample_rate: int = LINE_SAMPLE_RATE) -> str:
        """一次性转写：8k int16 PCM → 16k float32 → 尾部补静音 → 离线解码。"""
        if not audio_data or len(audio_data) < 320:
            return ""
        samples = _int16_bytes_to_float32(audio_data)
        if len(samples) == 0:
            return ""
        try:
            if sample_rate != self.sample_rate:
                samples = _resample(samples, sample_rate, self.sample_rate)

            # 尾部补静音，驱动解码器把句尾 token 吐完
            tail = np.zeros(int(self.sample_rate * _TAIL_SILENCE_MS / 1000), dtype=np.float32)
            samples = np.concatenate([samples, tail])

            stream = self._recognizer.create_stream()
            stream.accept_waveform(self.sample_rate, samples)
            self._recognizer.decode_stream(stream)

            text = stream.result.text.strip()
            if text:
                logger.info(f"[ASR:sensevoice] {text}")
            else:
                logger.warning(f"[ASR:sensevoice] 空结果 | audio_len={len(audio_data)}")
            return text
        except Exception as e:
            logger.error(f"ASR(sensevoice) 调用失败: {e}")
            return ""

    # ── 流式接口：离线引擎不支持 ──
    def create_stream(self):
        raise NotImplementedError("SenseVoice 是离线引擎，不支持流式接口（请使用 transcribe）")

    def feed(self, stream, samples_16k):
        raise NotImplementedError("SenseVoice 是离线引擎，不支持流式接口（请使用 transcribe）")

    def is_endpoint(self, stream) -> bool:
        raise NotImplementedError("SenseVoice 是离线引擎，不支持流式接口（请使用 transcribe）")

    def get_result_with_tail_silence(self, stream, tail_ms=400) -> str:
        raise NotImplementedError("SenseVoice 是离线引擎，不支持流式接口（请使用 transcribe）")

    def reset(self, stream):
        raise NotImplementedError("SenseVoice 是离线引擎，不支持流式接口（请使用 transcribe）")
