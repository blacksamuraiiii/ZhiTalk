# 模型下载说明

本项目 `models/` 目录（约 266MB ONNX 模型）已加入 .gitignore，不随仓库分发。
模型全部来自 [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 官方 release，
首次部署前需手动下载并放到对应目录。

## 下载地址

| 目录 | 模型 | 下载 URL | 解压后目录名 |
|------|------|----------|-------------|
| models/ASR/ | zipformer 流式中文 int8 | https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30.tar.bz2 | sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30 |
| models/TTS/ | matcha 中英混读 | https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-en.tar.bz2 | matcha-zh-en-8k |
| models/VAD/ | ten-vad int8 | https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.int8.onnx | （单文件，直接放 models/VAD/） |

## 下载命令

```bash
cd models/

# 1. ASR（zipformer 流式中文 int8）
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30.tar.bz2
tar -xjf sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30.tar.bz2 -C ASR/
rm sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30.tar.bz2

# 2. TTS（matcha 中英混读）
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-en.tar.bz2
tar -xjf matcha-icefall-zh-en.tar.bz2 -C TTS/
rm matcha-icefall-zh-en.tar.bz2
# 若解压出 matcha-icefall-zh-en 目录，需重命名为 matcha-zh-en-8k（与 config.yaml 一致）

# 3. VAD（ten-vad int8）
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.int8.onnx -P VAD/
```

> 目录名必须与 config.yaml 中 `model_dir` 路径一致，否则引擎加载失败。

## 模型来源说明

- ASR：由 HF `yuekai/icefall-asr-multi-zh-hans-zipformer-large` 转换，训练代码见 k2-fsa/icefall
- TTS：matcha-icefall 中英混读，sherpa-onnx 导出（model-steps-3.onnx + vocos-8khz-univ.onnx）
- VAD：ten-vad（TEN-framework，Apache 2.0 修改版）
