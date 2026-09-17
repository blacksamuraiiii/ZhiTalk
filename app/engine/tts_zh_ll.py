"""
ZhLlTTS - vits-zh-ll（中文 LL 音色，多说话人）
模型: TTS/sherpa-onnx-vits-zh-ll/model.onnx + G_multisperaker_latest.json（sid 支持）
注意: 该模型输出 16000Hz，synthesize 内部 soxr 降采样到 8kHz（统一输出契约）
"""
from engine.tts import _MODEL_DIR, _VitsTTSBase


class ZhLlTTS(_VitsTTSBase):
    name = "zh-ll"

    def __init__(self, config: dict):
        super().__init__(config)
        model_dir = config.get("model_dir", "TTS/sherpa-onnx-vits-zh-ll")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR
        self._init_offline_tts(
            model_path=base / "model.onnx",
            tokens_path=base / "tokens.txt",
            lexicon_path=base / "lexicon.txt",
            dict_dir=base / "dict",
            data_dir=base,
        )
