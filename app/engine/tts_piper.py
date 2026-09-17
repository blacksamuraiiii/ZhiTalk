"""
PiperZhTTS - piper 中文（zh_CN-xiao_ya-medium，espeak-ng 音素）
模型: TTS/vits-piper-zh_CN-xiao_ya-medium/zh_CN-xiao_ya-medium.onnx

sherpa-onnx 对 piper 模型的处理（offline-tts-vits-impl.h InitFrontend）：
  - 模型元数据 comment=piper / has_espeak=1 → PiperPhonemizeLexicon（espeak-ng 音素）
  - 必须把 data_dir 指向 piper 模型目录（内含 espeak-ng-data/），
    传空 dict_dir / lexicon（piper 无字典，validate() 返回 False 属正常）
注意: 该模型输出 22050Hz，synthesize 内部 soxr 降采样到 8kHz（统一输出契约）
"""
import os
from pathlib import Path

from engine.tts import _MODEL_DIR, _VitsTTSBase


class PiperZhTTS(_VitsTTSBase):
    name = "piper-zh"

    def __init__(self, config: dict):
        super().__init__(config)
        model_dir = config.get("model_dir", "TTS/vits-piper-zh_CN-xiao_ya-medium")
        base = _MODEL_DIR / model_dir if model_dir else _MODEL_DIR

        # 兼容兜底：显式设置 espeak-ng 数据路径（data_dir 机制已能自动找到，
        # 此处双保险，防 espeak-ng 内部按 ESPEAK_DATA_PATH 查找失败）
        espeak_data = base / "espeak-ng-data"
        if espeak_data.is_dir():
            os.environ.setdefault("ESPEAK_DATA_PATH", str(espeak_data))
            os.environ.setdefault("SHERPA_ONNX_ESPEAK_DATA_DIR", str(espeak_data))

        # piper 模型目录内唯一 .onnx 即模型（zh_CN-xiao_ya-medium.onnx）
        onnx_files = sorted(base.glob("*.onnx"))
        if not onnx_files:
            raise FileNotFoundError(f"TTS(piper-zh) 模型文件不存在: {base}/*.onnx")

        self._init_offline_tts(
            model_path=onnx_files[0],
            tokens_path=base / "tokens.txt",
            lexicon_path=Path(""),       # piper 无 lexicon（espeak 音素建模）
            dict_dir=Path(""),           # piper 无 dict
            data_dir=base,               # 关键：指向模型目录（含 espeak-ng-data）
            rule_fsts=False,             # piper 无 fst 规则
        )


class PiperXiaoYaTTS(PiperZhTTS):
    """piper 中文 xiao_ya 音色（与 huayan 同源，不同音色，供对比试听）。"""

    name = "piper-xiaoya"
