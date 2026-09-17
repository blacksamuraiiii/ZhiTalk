"""
MeloTTS - 官方 vits-melo-tts-zh_en int8 版（B1 配套）
模型: TTS/vits-melo-tts-zh_en/model.int8.onnx
注意: 该模型输出 44100Hz，synthesize 内部 soxr 降采样到 8kHz（统一输出契约）
"""
from engine.tts import _MODEL_DIR, _VitsTTSBase


class MeloTTS(_VitsTTSBase):
    name = "melo"

    def __init__(self, config: dict):
        super().__init__(config)
        model_dir = config.get("model_dir", "TTS/vits-melo-tts-zh_en")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR
        self._init_offline_tts(
            model_path=base / "model.int8.onnx",
            tokens_path=base / "tokens.txt",
            lexicon_path=base / "lexicon.txt",
            dict_dir=base / "dict",
            data_dir=base,
        )
