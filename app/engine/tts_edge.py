"""
EdgeTTS - 微软在线 TTS（多 TTS 引擎第5套）

依赖: edge-tts>=6.1.0, soundfile>=0.14.0, soxr>=0.5.0
输出: 8kHz int16 s16le bytes（统一 BaseTTS 契约）

edge-tts 在线合成 → MP3(24kHz) → soundfile 读取 → soxr 降采样 8k → RMS 归一化

支持的 voice 列表（在线查询）：
  zh-CN-XiaoxiaoNeural   (女声，默认)
  zh-CN-XiaoyiNeural     (女声，自然)
  zh-CN-YunxiNeural      (男声)
  zh-CN-YunjianNeural    (男声)
  zh-CN-YunyangNeural    (男声，新闻)
  更多见: edge-tts --list-voices
"""
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import numpy as np

from engine.tts import BaseTTS, OUTPUT_SAMPLE_RATE, _PEAK_TARGET

logger = logging.getLogger("ai-backend.tts")

_BASE = Path(__file__).parent.parent.parent  # ai-backend/


class EdgeTTS(BaseTTS):
    """微软在线 TTS 引擎（edge-tts）。

    合成链路：edge-tts → MP3 (24kHz mono) → soundfile 读取 float32
          → soxr 降采样 8kHz → 线性放大 peak≈20000 → int16 s16le bytes
    缓存 key = md5(text + voice + rate + volume)
    """

    name = "edge"

    def __init__(self, config: dict):
        config = config if isinstance(config, dict) else {}
        super().__init__(config)
        self.voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
        self.rate = config.get("rate", "+0%")
        self.volume = config.get("volume", "+0%")
        self.connect_timeout = int(config.get("connect_timeout", 5))
        self.receive_timeout = int(config.get("receive_timeout", 30))

        # 缓存目录（独立于其他引擎防串音）
        cache_dir = config.get("cache_dir", "data/tts_cache/edge")
        if not os.path.isabs(cache_dir):
            cache_dir = str(_BASE / cache_dir)
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

        # 注意：不传 aiohttp connector 给 edge-tts Communicate()
        # edge-tts 7.x 内部在 stream() 退出时关闭 session，
        # 共享 connector 导致后续请求报 "Session is closed"。
        # 每段新建连接虽然多一次 TCP 握手，但可靠性优先。

        logger.info(
            f"EdgeTTS 引擎初始化 | voice={self.voice} | rate={self.rate} | volume={self.volume} "
            f"| connect_timeout={self.connect_timeout}s | receive_timeout={self.receive_timeout}s"
        )

    def _cache_key(self, text: str) -> str:
        """缓存 key 包含 text + voice + rate + volume（改音色/语速/音量不串音）。"""
        return hashlib.md5(
            f"{text}|{self.voice}|{self.rate}|{self.volume}".encode()
        ).hexdigest()

    def _synthesize_internal(self, text: str) -> bytes:
        """edge-tts 在线合成 → 8kHz int16 PCM bytes（同步包装）。"""
        return asyncio.run(self._synthesize_async_internal(text))

    async def _synthesize_async_internal(self, text: str) -> bytes:
        """edge-tts 在线合成核心实现（异步）。

        流程：
          1. edge_tts.Communicate(text, voice, rate, volume) → MP3 bytes
          2. soundfile.read(MP3 bytes) → float32 samples @24kHz
          3. soxr.resample 降采样到 8kHz
          4. 线性放大 peak 到 ~_PEAK_TARGET（避免 RMS 归一化放大底噪）
          5. → int16 s16le bytes
        """
        import edge_tts
        import soundfile as sf
        import soxr

        t0 = time.time()

        # 1. edge-tts 合成（在线，需网络）
        communicate = edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            volume=self.volume,
            connect_timeout=self.connect_timeout,
            receive_timeout=self.receive_timeout,
        )

        mp3_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data.extend(chunk["data"])

        if not mp3_data:
            logger.error(f"EdgeTTS 无音频返回 | text={text[:50]}")
            return b""

        elapsed_synth = (time.time() - t0) * 1000

        # 2. soundfile 读取 MP3 → float32
        import io
        mp3_bytes = bytes(mp3_data)
        try:
            samples, sr = sf.read(io.BytesIO(mp3_bytes), dtype="float32")
        except Exception as e:
            logger.error(f"EdgeTTS soundfile 解码失败: {e}")
            return b""

        # soundfile 可能返回 2D (samples, channels)
        if samples.ndim > 1 and samples.shape[1] > 1:
            # 多声道 → 取第一声道
            samples = samples[:, 0]

        # 3. soxr 降采样到 8kHz
        if sr != OUTPUT_SAMPLE_RATE:
            samples = soxr.resample(samples, sr, OUTPUT_SAMPLE_RATE).astype(np.float32)
            sr = OUTPUT_SAMPLE_RATE

        # 4. 线性放大到 peak≈20000（同 _to_8k_s16le 策略）
        pcm = (samples * 32767.0).astype(np.int16)
        peak = float(np.max(np.abs(pcm))) if len(pcm) else 0.0
        if peak > 0:
            gain = min(_PEAK_TARGET / peak, 10.0)
            pcm = np.clip(pcm.astype(np.float32) * gain, -32767, 32767).astype(np.int16)

        result = pcm.tobytes()
        elapsed_total = (time.time() - t0) * 1000
        audio_len_s = len(pcm) / OUTPUT_SAMPLE_RATE

        logger.info(
            f"[TTS:edge] {len(text)}字 → {len(pcm)}样本 ({audio_len_s:.1f}s @ 8kHz) | "
            f"合成{elapsed_synth:.0f}ms | 总计{elapsed_total:.0f}ms | voice={self.voice}"
        )
        return result

    async def synthesize_async(self, text: str) -> bytes:
        """edge-tts 异步合成（带磁盘缓存）。"""
        cache_key = self._cache_key(text)
        cached_path = self._get_cached_path(cache_key)
        if os.path.exists(cached_path):
            with open(cached_path, "rb") as f:
                return f.read()

        slin_data = await self._synthesize_async_internal(text)
        if slin_data:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cached_path, "wb") as f:
                f.write(slin_data)
            self._maybe_cleanup_cache()
        return slin_data
