"""
X-ASR 流式引擎（现有默认 ASR）
模型: x-asr-zh-en-streaming-base-onnx-demo（streaming zipformer2 transducer）
接口: 流式（is_streaming=True），逻辑在 _TransducerStreamASR 公共基类
"""
from engine.asr import _TransducerStreamASR, logger


class XAsrStreamASR(_TransducerStreamASR):
    def __init__(self, config: dict):
        super().__init__(config, provider="xasr")
