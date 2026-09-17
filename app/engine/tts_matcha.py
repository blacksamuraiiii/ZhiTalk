"""
MatchaTTS - Matcha-TTS 8kHz 中英混读模型（conditional flow matching）
模型: TTS/matcha-zh-en-8k/
  - acoustic_model: model-steps-3.onnx（声学模型，flow matching）
  - vocoder:        vocos-8khz-univ.onnx（Vocos 8kHz 声码器）

PESQ-NB 3.80，专门针对 G.711 电话场景蒸馏，输出 8kHz（无需重采样）。

sherpa-onnx 对 matcha 的处理（OfflineTtsMatchaModelConfig）：
  - acoustic_model + vocoder 两个 onnx 分离（声学模型 + 声码器）
  - lexicon/tokens 是 espeak 音素映射；data_dir 指向 espeak-ng-data 目录
  - rule_fsts 传 date/number/phone 三个 fst（文本归一化）
  - GenerationConfig.num_steps 是 flow matching ODE 求解步数（默认 5，越大质量越高越慢）
"""
from pathlib import Path

import numpy as np

from engine.tts import _MODEL_DIR, BaseTTS, _to_8k_s16le

logger = None  # 由引擎内部使用，避免循环 import


class MatchaTTS(BaseTTS):
    name = "matcha-zh-en-8k"

    def __init__(self, config: dict):
        super().__init__(config)
        model_dir = config.get("model_dir", "TTS/matcha-zh-en-8k")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR

        import sherpa_onnx

        acoustic = base / "model-steps-3.onnx"
        vocoder = base / "vocos-8khz-univ.onnx"
        tokens = base / "tokens.txt"
        lexicon = base / "lexicon.txt"
        data_dir = base / "espeak-ng-data"

        for p, name in [(acoustic, "acoustic_model"), (vocoder, "vocoder"),
                        (tokens, "tokens"), (lexicon, "lexicon"), (data_dir, "data_dir")]:
            if not p.exists():
                raise FileNotFoundError(f"TTS(matcha) {name} 不存在: {p}")

        matcha_config = sherpa_onnx.OfflineTtsMatchaModelConfig(
            acoustic_model=str(acoustic),
            vocoder=str(vocoder),
            lexicon=str(lexicon),
            tokens=str(tokens),
            data_dir=str(data_dir),
        )
        # matcha flow matching 噪声尺度（默认 1.0；可调 0.6-1.0 平滑度）
        matcha_config.noise_scale = float(config.get("noise_scale", 1.0))
        matcha_config.length_scale = float(config.get("length_scale", 1.0))

        # 文本归一化 fst（date/number/phone），用绝对路径逗号连接
        fst_files = [str(base / f) for f in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")
                     if (base / f).exists()]
        rule_fsts = ",".join(fst_files)

        num_threads = int(config.get("threads", 4))
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                matcha=matcha_config,
                num_threads=num_threads,
                provider="cpu",
                debug=False,
            ),
            rule_fsts=rule_fsts,
        )
        self._tts = sherpa_onnx.OfflineTts(tts_config)

        # GenerationConfig：matcha 特有 num_steps（ODE 求解步数）
        self._gen_config = sherpa_onnx.GenerationConfig()
        self._gen_config.speed = self.speed
        self._gen_config.num_steps = int(config.get("num_steps", 5))

        self._log(f"MatchaTTS 初始化完成 | noise_scale={matcha_config.noise_scale} "
                  f"| num_steps={self._gen_config.num_steps} | 输出 8kHz")

    def _log(self, msg: str):
        import logging
        logging.getLogger("ai-backend.tts").info(f"[{self.name}] {msg}")

    def _synthesize_internal(self, text: str) -> bytes:
        """调用 sherpa-onnx OfflineTts.generate → 8kHz int16 PCM 字节。"""
        import time
        t0 = time.time()
        audio = self._tts.generate(text, self._gen_config)
        elapsed = (time.time() - t0) * 1000

        if audio is None or not audio.samples:
            self._log(f"无输出 | text={text[:50]}")
            return b""

        samples = np.array(audio.samples, dtype=np.float32)
        sr = int(audio.sample_rate)
        pcm = _to_8k_s16le(samples, sr)

        self._log(f"{len(text)}字 → {len(pcm)}B ({len(pcm)/8000/2:.2f}s @8kHz) | "
                  f"原始{sr}Hz | {elapsed:.0f}ms")
        return pcm
