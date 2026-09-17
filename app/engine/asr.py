"""
ASR 引擎工厂 + 公共基类（批次4.1 多 ASR 引擎框架）

├── BaseASR            公共接口基类（流式/离线统一）
├── _TransducerStreamASR  流式 transducer 基类（X-ASR / 官方 zipformer 共用，
│                         sherpa-onnx OnlineRecognizer.from_transducer）
└── create_asr(config) 工厂：按 config["provider"] 返回具体引擎
    xasr       → XAsrStreamASR        （流式 zipformer2 transducer，低延迟）
    zipformer  → ZipformerStreamASR   （官方 zipformer，流式）
    sensevoice → SenseVoiceASR        （离线，电话/噪声鲁棒）
    telespeech → TeleSpeechASR        （离线 CTC，电话语音）

统一约定：
  - 流式引擎：create_stream/feed/is_endpoint/get_result_with_tail_silence/reset
  - 离线引擎：只实现 transcribe(audio_data, 8000)，流式接口抛 NotImplementedError
  - is_streaming: 流式 True / 离线 False（audiosocket.py 据此分流）
"""
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("ai-backend.asr")

_BASE = Path(__file__).parent.parent.parent  # ai-backend/
_MODEL_DIR = _BASE / "models"

# ASR 模型统一 16kHz 输入
MODEL_SAMPLE_RATE = 16000
# AudioSocket 线路采样率 8kHz
LINE_SAMPLE_RATE = 8000


def _resample(samples: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """任意采样率 float32 [-1,1] → to_sr float32（soxr 高质量重采样）。"""
    import soxr

    if samples.dtype != np.float32:
        samples = samples.astype(np.float32)
    if len(samples) == 0 or from_sr == to_sr:
        return samples
    return soxr.resample(samples, from_sr, to_sr).astype(np.float32)


def _resample_8k_to_16k(samples_8k: np.ndarray) -> np.ndarray:
    """8kHz float32 [-1,1] → 16kHz float32 [-1,1]（audiosocket 流式路径共用）。"""
    return _resample(samples_8k, LINE_SAMPLE_RATE, MODEL_SAMPLE_RATE)


def _int16_bytes_to_float32(audio_data: bytes) -> np.ndarray:
    """int16 PCM bytes → float32 [-1,1] numpy。"""
    return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0


class BaseASR:
    """ASR 引擎统一接口。

    两种用法：
      1. 流式（对话主路径，仅流式引擎）：
           stream = asr.create_stream()
           asr.feed(stream, samples_16k)
           asr.is_endpoint(stream) → 尾部静音超阈值
           asr.get_result_with_tail_silence(stream) → 最终文本
           asr.reset(stream)
      2. 一次性（离线引擎 + 测试/回拨）：transcribe(audio_bytes, 8000) -> str
    """

    def transcribe(self, audio_data: bytes, sample_rate: int = 8000) -> str:
        raise NotImplementedError

    # ── 流式接口（离线引擎不实现）──
    def create_stream(self):
        raise NotImplementedError

    def feed(self, stream, samples_16k):
        raise NotImplementedError

    def is_endpoint(self, stream) -> bool:
        raise NotImplementedError

    def get_result_with_tail_silence(self, stream, tail_ms=400) -> str:
        raise NotImplementedError

    def get_result(self, stream) -> str:
        """endpoint 触发/强制截断后取当前最终文本（流式引擎实现；离线抛 NotImplemented）。"""
        raise NotImplementedError

    def reset(self, stream):
        raise NotImplementedError

    @property
    def is_streaming(self) -> bool:
        return False


class _TransducerStreamASR(BaseASR):
    """流式 transducer 引擎公共实现（OnlineRecognizer.from_transducer）。

    X-ASR 与官方 zipformer 都是 zipformer2 transducer 结构，接口完全一致：
    encoder.int8.onnx / decoder.onnx / joiner.int8.onnx / tokens.txt，
    差异只在 model_dir 与 rule 参数，故抽成公共基类，两个引擎只传配置。

    模型要求 16kHz 采样率（80 维 fbank，喂 8k 特征错乱）。
    """

    def __init__(self, config: dict, provider: str):
        import sherpa_onnx

        self.provider = provider
        model_dir = config.get("model_dir", "")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR

        def _resolve(key: str, default_name: str) -> Path:
            v = config.get(key)
            if v:
                p = Path(v)
                return p if p.is_absolute() else _MODEL_DIR / v
            return base / default_name

        encoder_path = _resolve("encoder", "encoder.int8.onnx")
        decoder_path = _resolve("decoder", "decoder.onnx")
        joiner_path = _resolve("joiner", "joiner.int8.onnx")
        tokens_path = _resolve("tokens_file", "tokens.txt")
        num_threads = int(config.get("threads", 4))
        self.sample_rate = int(config.get("sample_rate", MODEL_SAMPLE_RATE))

        # 验证模型文件存在
        for path, name in [
            (encoder_path, "encoder"),
            (decoder_path, "decoder"),
            (joiner_path, "joiner"),
            (tokens_path, "tokens"),
        ]:
            if not path.exists():
                raise FileNotFoundError(f"ASR({provider}) 模型文件不存在: {path} ({name})")

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(encoder_path),
            decoder=str(decoder_path),
            joiner=str(joiner_path),
            tokens=str(tokens_path),
            num_threads=num_threads,
            sample_rate=self.sample_rate,
            decoding_method="greedy_search",
            provider="cpu",
            # ── 端点检测（电话场景参数）──
            enable_endpoint_detection=bool(config.get("enable_endpoint_detection", True)),
            rule1_min_trailing_silence=float(config.get("rule1_min_trailing_silence", 1.8)),
            rule2_min_trailing_silence=float(config.get("rule2_min_trailing_silence", 2.0)),
            rule3_min_utterance_length=float(config.get("rule3_min_utterance_length", 30.0)),
            # endpoint 触发后清编码器状态，防止跨轮上下文污染
            reset_encoder=True,
        )
        logger.info(
            f"ASR 引擎初始化完成 ({provider}) | "
            f"encoder={encoder_path.name} | sr={self.sample_rate} | "
            f"endpoint(r1={config.get('rule1_min_trailing_silence', 1.8)}s) | "
            f"is_streaming=True"
        )

    @property
    def is_streaming(self) -> bool:
        return True

    def create_stream(self):
        """创建新的识别流（每句话一个 stream，endpoint 后 reset 复用）"""
        return self._recognizer.create_stream()

    def feed(self, stream, samples_16k: np.ndarray):
        """喂入 16k float32 [-1,1] 音频块并驱动解码。"""
        if samples_16k is None or len(samples_16k) == 0:
            return
        stream.accept_waveform(self.sample_rate, samples_16k)
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)

    def is_endpoint(self, stream) -> bool:
        """是否已检测到一句话说完（尾部静音超阈值）"""
        try:
            return self._recognizer.is_endpoint(stream)
        except Exception:
            return False

    def get_result(self, stream) -> str:
        """直接取当前最终文本（强制截断分支用；不加尾静音，保留流式原始结果）。"""
        try:
            return self._recognizer.get_result(stream)
        except Exception as e:
            logger.warning(f"ASR({self.provider}) get_result 失败: {e}")
            return ""

    def get_result_with_tail_silence(self, stream, tail_ms: int = 400) -> str:
        """endpoint 触发后补静音再取最终结果（修复尾部字丢失）。

        zipformer 是 960ms chunk 流式模型：endpoint（尾部静音超阈值）触发时，
        最后一个 chunk 的 token 可能尚未完全解码（语音尾音还在解码器队列中），
        get_result 会漏掉尾部 1-2 个字（如"功能"只识别出"功"）。
        补喂 tail_ms 静音驱动解码器把剩余 token 吐完，再取结果。
        """
        try:
            tail = np.zeros(int(self.sample_rate * tail_ms / 1000), dtype=np.float32)
            step = int(self.sample_rate * 0.2)
            for i in range(0, len(tail), step):
                block = tail[i:i + step]
                stream.accept_waveform(self.sample_rate, block)
                while self._recognizer.is_ready(stream):
                    self._recognizer.decode_stream(stream)
            return self._recognizer.get_result(stream)
        except Exception as e:
            logger.warning(f"ASR({self.provider}) get_result_with_tail_silence 失败: {e}")
            return ""

    def reset(self, stream):
        """清空流状态，为下一句话准备（endpoint 已触发时 get_result 会隐含 reset）"""
        try:
            self._recognizer.reset(stream)
        except Exception as e:
            logger.warning(f"ASR({self.provider}) reset 失败: {e}")

    def transcribe(self, audio_data: bytes, sample_rate: int = 8000) -> str:
        """转写 PCM 音频为文字（一次性接口，测试/回拨场景）"""
        if not audio_data or len(audio_data) < 320:
            return ""
        samples = _int16_bytes_to_float32(audio_data)
        if len(samples) == 0:
            return ""
        try:
            if sample_rate != self.sample_rate:
                samples = _resample(samples, sample_rate, self.sample_rate)
            sr = self.sample_rate

            stream = self._recognizer.create_stream()
            stream.accept_waveform(sr, samples)
            # 加尾部静音确保尾部语音被处理
            tail = np.zeros(int(0.5 * sr), dtype=np.float32)
            stream.accept_waveform(sr, tail)
            stream.input_finished()

            while self._recognizer.is_ready(stream):
                self._recognizer.decode_stream(stream)

            text = self._recognizer.get_result_all(stream).text.strip()
            if text:
                logger.info(f"[ASR:{self.provider}] {text}")
            else:
                logger.warning(f"[ASR:{self.provider}] 空结果 | audio_len={len(audio_data)}")
            return text
        except Exception as e:
            logger.error(f"ASR({self.provider}) 调用失败: {e}")
            return ""


def create_asr(config: dict) -> BaseASR:
    """ASR 引擎工厂：按 config["provider"] 实例化对应引擎。

    config 形如：
      {provider: xasr, xasr: {...}, zipformer: {...}, sensevoice: {...}, telespeech: {...}}
    每个引擎只拿自己的子段（引擎内 config.get("xxx", {}) 兜底为空）。
    """
    if not isinstance(config, dict):
        config = {}
    provider = config.get("provider", "xasr")
    sub = config.get(provider, {})
    if not isinstance(sub, dict):
        sub = {}
    if provider == "xasr":
        from engine.asr_xasr import XAsrStreamASR
        return XAsrStreamASR(sub)
    if provider == "zipformer":
        from engine.asr_zipformer import ZipformerStreamASR
        return ZipformerStreamASR(sub)
    if provider == "sensevoice":
        from engine.asr_sensevoice import SenseVoiceASR
        return SenseVoiceASR(sub)
    if provider == "telespeech":
        from engine.asr_telespeech import TeleSpeechASR
        return TeleSpeechASR(sub)
    raise ValueError(f"未知 ASR provider: {provider}")
