"""
TTS 引擎工厂 + 公共基类（批次4.1 多 TTS 引擎框架）

├── BaseTTS       公共接口基类（synthesize/synthesize_async + 缓存，输出 8kHz s16le）
├── _VitsTTSBase  VITS 家族公共实现（melo8k/melo/zh-ll 共用；piper 也是 VITS 结构）
└── create_tts(config)  工厂：按 config["provider"] 返回具体引擎
    melo8k    → Melo8kTTS    （VITS 8k，原生 8k 输出，固话场景默认）
    melo      → MeloTTS      （官方 vits-melo-tts-zh_en int8，输出 44100 需降采样）
    zh-ll     → ZhLlTTS      （vits-zh-ll 多说话人，输出 16000 需降采样）
    piper-zh  → PiperZhTTS   （piper 中文 xiao_ya，espeak-ng，输出 22050 需降采样）

统一输出契约：所有 TTS synthesize 返回 8kHz int16 s16le bytes（BaseTTS.sample_rate=8000）。
非 8k 引擎内部用 soxr 降采样；缓存 key = md5(text + speed + sid)（修复改 speed 缓存串音）。
"""
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger("ai-backend.tts")

_BASE = Path(__file__).parent.parent.parent  # ai-backend/
_MODEL_DIR = _BASE / "models"

# 统一输出采样率（固话线路 8kHz）
OUTPUT_SAMPLE_RATE = 8000
# 线性放大目标 peak（留 headroom 防削波，旧 edge-tts 约 18000-19000）
_PEAK_TARGET = 20000.0


def _to_8k_s16le(samples: np.ndarray, sample_rate: int) -> bytes:
    """float32 [-1,1] → 8kHz int16 s16le bytes（统一输出契约）。

    - 非 8k 输出先 soxr 降采样
    - 不做 RMS 归一化（模型原始幅度低，RMS 归一化会放大底噪导致"嘴里含水"）
    - 线性放大到 peak≈20000（电话线路需要足够幅度）
    """
    import soxr

    if samples.dtype != np.float32:
        samples = samples.astype(np.float32)
    if sample_rate != OUTPUT_SAMPLE_RATE:
        samples = soxr.resample(samples, sample_rate, OUTPUT_SAMPLE_RATE).astype(np.float32)

    pcm = (samples * 32767.0).astype(np.int16)
    peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
    if peak > 0:
        gain = min(_PEAK_TARGET / peak, 10.0)  # 最多放大 10 倍
        pcm = np.clip(pcm.astype(np.float32) * gain, -32767, 32767).astype(np.int16)
    return pcm.tobytes()


class BaseTTS:
    """TTS 引擎统一接口（含磁盘缓存，输出 8kHz s16le）。"""

    def __init__(self, config: dict):
        self.config = config if isinstance(config, dict) else {}
        self.speed = float(config.get("speed", 1.0)) if config.get("speed") is not None else 1.0
        self.speaker_id = int(config.get("speaker_id", 0)) if config.get("speaker_id") is not None else 0
        cache_dir = config.get("cache_dir", "data/tts_cache")
        if not os.path.isabs(cache_dir):
            cache_dir = str(_BASE / cache_dir)
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        # 缓存清理阈值（默认 50MB）+ 写入计数器（节流：每 50 次写检查一次）
        self.cache_max_bytes = int(config.get("cache_max_bytes", 50 * 1024 * 1024))
        self._cache_write_count = 0

    @property
    def sample_rate(self) -> int:
        return OUTPUT_SAMPLE_RATE

    def _cache_key(self, text: str) -> str:
        """缓存 key 包含 text + speed + sid（修复原 bug：只含 text 时改语速会串音）。"""
        return hashlib.md5(f"{text}|{self.speed}|{self.speaker_id}".encode()).hexdigest()

    def _maybe_cleanup_cache(self):
        """缓存目录超过阈值时删除最旧的 30%（节流：每 50 次写缓存检查一次）。"""
        self._cache_write_count += 1
        if self._cache_write_count % 50 != 0:
            return
        try:
            files = [p for p in Path(self.cache_dir).iterdir() if p.is_file()]
            if not files:
                return
            total = sum(p.stat().st_size for p in files)
            if total < self.cache_max_bytes:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            remove_n = max(1, int(len(files) * 0.3))
            for p in files[:remove_n]:
                try:
                    p.unlink()
                except OSError:
                    pass
            logger.info(
                f"[TTS缓存清理] {self.cache_dir} 超限({total / 1024 / 1024:.1f}MB) "
                f"→ 删除最旧 {remove_n} 个文件"
            )
        except Exception:
            pass  # 清理失败不阻塞主流程

    def _get_cached_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.slin")

    def _synthesize_internal(self, text: str) -> bytes:
        """子类实现：text → 8kHz int16 PCM bytes"""
        raise NotImplementedError

    def synthesize(self, text: str) -> bytes:
        """文字 → 8kHz PCM 音频 (同步，用于预加载/测试)"""
        cache_key = self._cache_key(text)
        cached_path = self._get_cached_path(cache_key)
        if os.path.exists(cached_path):
            with open(cached_path, "rb") as f:
                return f.read()
        slin_data = self._synthesize_internal(text)
        if slin_data:
            with open(cached_path, "wb") as f:
                f.write(slin_data)
            self._maybe_cleanup_cache()
        return slin_data

    async def synthesize_async(self, text: str) -> bytes:
        """文字 → 8kHz PCM 音频 (异步版本)"""
        cache_key = self._cache_key(text)
        cached_path = self._get_cached_path(cache_key)
        if os.path.exists(cached_path):
            with open(cached_path, "rb") as f:
                return f.read()
        loop = asyncio.get_event_loop()
        slin_data = await loop.run_in_executor(None, self._synthesize_internal, text)
        if slin_data:
            with open(cached_path, "wb") as f:
                f.write(slin_data)
            self._maybe_cleanup_cache()
        return slin_data


class _VitsTTSBase(BaseTTS):
    """VITS 家族公共实现（sherpa-onnx OfflineTts + GenerationConfig）。"""

    def _init_offline_tts(self, model_path: Path, tokens_path: Path, lexicon_path: Path,
                          dict_dir: Path, data_dir: Path | None = None,
                          rule_fsts: bool = True):
        """构造 sherpa-onnx OfflineTts（VITS）。

        Args:
            model_path:  模型 onnx 文件
            tokens_path: tokens.txt
            lexicon_path: lexicon.txt（piper 模型传空字符串）
            dict_dir:    发音词典目录（piper 传空）
            data_dir:    数据目录（VITS rule_fsts 所在目录；piper 指模型目录，内含 espeak-ng-data）
            rule_fsts:   是否附加 date/number/phone/new_heteronym.fst 文本归一化规则
        """
        import sherpa_onnx

        model_path = Path(model_path)
        tokens_path = Path(tokens_path)
        if not model_path.exists():
            raise FileNotFoundError(f"TTS 模型文件不存在: {model_path}")
        if not tokens_path.exists():
            raise FileNotFoundError(f"TTS tokens 文件不存在: {tokens_path}")

        num_threads = int(self.config.get("threads", 4))

        if rule_fsts and data_dir is not None:
            rule_fsts_str = ",".join(
                str(Path(data_dir) / f) for f in ("date.fst", "number.fst", "phone.fst", "new_heteronym.fst")
                if (Path(data_dir) / f).exists()
            )
        else:
            rule_fsts_str = ""

        vits_config = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=str(model_path),
            lexicon=str(lexicon_path) if lexicon_path else "",
            tokens=str(tokens_path),
            dict_dir=str(dict_dir) if dict_dir else "",
            # ⚠ 铁律：VITS（melo/zh-ll，字符词典）只传 4 项（model/lexicon/tokens/dict_dir），
            #   不传 data_dir！多传 data_dir 会导致生成语速异常（14.8字/秒 vs 正常5.6）。
            #   但 piper 模型（espeak 音素，rule_fsts=False）必须传 data_dir 指向模型目录，
            #   否则 sherpa-onnx 找不到 espeak-ng-data/cmn_dict → 中文全 OOV 静音。
        )
        # noise_scale / noise_scale_w 从 config 读取（默认 0.4/0.6），
        # 降低 noise_scale 减少毛刺感（VITS 默认 0.667→0.4，社区推荐 0.3-0.5；
        # 降低 noise_scale_w 减少字间噪声（默认 0.8→0.6，推荐 0.5-0.7）
        vits_config.noise_scale = float(self.config.get("noise_scale", 0.4))
        vits_config.noise_scale_w = float(self.config.get("noise_scale_w", 0.6))
        if not rule_fsts and data_dir is not None:
            # piper 特化：data_dir 是 espeak-ng-data 查找路径（修复 piper-zh 中文 OOV）
            vits_config.data_dir = str(data_dir)
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits_config,
                num_threads=num_threads,
                provider="cpu",
                debug=False,
            ),
            rule_fsts=rule_fsts_str,
        )
        # piper 模型 validate() 对 data_dir 内文件（phontab 等）校验会报 False，
        # 但生成正常，故 validate 失败仅告警不中断
        if not tts_config.validate():
            logger.warning(f"TTS({self.name}) config validate() 返回 False（piper 模型属正常）")
        self._tts = sherpa_onnx.OfflineTts(tts_config)

        # GenerationConfig（官方标准传参方式）—— 只传 sid/speed，不加 silence_scale！
        # ⚠ 铁律：加 silence_scale 会把句尾静音压缩 → 音频时长大幅缩短（0.96s→0.47s），
        #   ASR 对压缩后的短音频识别变差（"你有什么功能"→"哦"）。保持默认。
        self._gen_config = sherpa_onnx.GenerationConfig()
        self._gen_config.sid = self.speaker_id
        self._gen_config.speed = self.speed

        logger.info(
            f"TTS 引擎初始化完成 ({self.name}) | model={model_path.name} | "
            f"sid={self.speaker_id} | speed={self.speed} | 输出{OUTPUT_SAMPLE_RATE}Hz"
        )

    def _synthesize_internal(self, text: str) -> bytes:
        """调用 sherpa-onnx OfflineTts.generate → 统一 8kHz int16 PCM 字节。"""
        t0 = time.time()
        audio = self._tts.generate(text, self._gen_config)
        elapsed = (time.time() - t0) * 1000

        if audio is None or not audio.samples:
            logger.error(f"TTS({self.name}) 无输出 | text={text[:50]}")
            return b""

        samples = np.array(audio.samples, dtype=np.float32)
        sr = int(audio.sample_rate)
        pcm = _to_8k_s16le(samples, sr)

        audio_len = len(pcm)
        logger.info(
            f"[TTS:{self.name}] {len(text)}字 → {audio_len}样本 ({audio_len/sr*1000/8000:.1f}s @ 8kHz) | "
            f"原始{sr}Hz | {elapsed:.0f}ms"
        )
        return pcm


def create_tts(config: dict) -> BaseTTS:
    """TTS 引擎工厂：按 config["provider"] 实例化对应引擎。

    config 形如：
      {provider: melo8k, melo8k: {...}, melo: {...}, "zh-ll": {...}, "piper-zh": {...}, "edge": {...}}
    """
    if not isinstance(config, dict):
        config = {}
    provider = config.get("provider", "melo8k")
    sub = config.get(provider, {})
    if not isinstance(sub, dict):
        sub = {}
    if provider == "melo8k":
        from engine.tts_melo8k import Melo8kTTS
        return Melo8kTTS(sub)
    if provider == "melo":
        from engine.tts_melo import MeloTTS
        return MeloTTS(sub)
    if provider == "zh-ll":
        from engine.tts_zh_ll import ZhLlTTS
        return ZhLlTTS(sub)
    if provider == "piper-zh":
        from engine.tts_piper import PiperZhTTS
        return PiperZhTTS(sub)
    if provider == "piper-xiaoya":
        from engine.tts_piper import PiperXiaoYaTTS
        return PiperXiaoYaTTS(sub)
    if provider == "edge":
        from engine.tts_edge import EdgeTTS
        return EdgeTTS(sub)
    if provider == "matcha-zh-en-8k":
        from engine.tts_matcha import MatchaTTS
        return MatchaTTS(sub)
    raise ValueError(f"未知 TTS provider: {provider}")
