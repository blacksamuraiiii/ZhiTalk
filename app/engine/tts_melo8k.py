"""
Melo8kTTS - VITS MeloTTS 8kHz（现有默认 TTS，原生 8k 输出）
模型: TTS/vits-melo-tts-zh_en-8k/model.onnx
"""
from pathlib import Path

from engine.tts import _MODEL_DIR, _VitsTTSBase


class Melo8kTTS(_VitsTTSBase):
    name = "melo8k"

    def __init__(self, config: dict):
        super().__init__(config)
        model_dir = config.get("model_dir", "TTS/vits-melo-tts-zh_en-8k")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR
        self._init_offline_tts(
            model_path=base / "model.onnx",
            tokens_path=base / "tokens.txt",
            lexicon_path=base / "lexicon.txt",
            dict_dir=base / "dict",
            data_dir=base,
        )
