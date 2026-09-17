"""
官方 zipformer 流式引擎
模型: sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30
      （icefall multi_zh-hans 多数据集，官方出品）
接口: 流式（is_streaming=True），逻辑在 _TransducerStreamASR 公共基类
"""
from engine.asr import _TransducerStreamASR


class ZipformerStreamASR(_TransducerStreamASR):
    def __init__(self, config: dict):
        super().__init__(config, provider="zipformer")
