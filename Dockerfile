FROM ubuntu:24.04

LABEL maintainer="telecom-ai"
LABEL description="固话智连 AI 后端 - sherpa-onnx ASR+TTS + Flask"

ENV TZ=Asia/Shanghai
ENV DEBIAN_FRONTEND=noninteractive

# 系统依赖：阿里云 apt 源加速（Ubuntu 24.04 deb822 格式）
RUN sed -i 's|http://archive.ubuntu.com/|http://mirrors.aliyun.com/|g' /etc/apt/sources.list.d/ubuntu.sources && \
    sed -i 's|http://security.ubuntu.com/|http://mirrors.aliyun.com/|g' /etc/apt/sources.list.d/ubuntu.sources && \
    apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖（sherpa-onnx pip wheel 自带 onnxruntime）
COPY requirements.txt .
RUN rm -f /usr/lib/python3.12/EXTERNALLY-MANAGED && \
    python3 -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --no-cache-dir -r requirements.txt

WORKDIR /app

# ONNX 模型 + 预合成音频都改挂载（./models、./data 挂载进容器），不打包进镜像

# 应用代码
COPY app/ ./app/
COPY config.yaml ./config.yaml

RUN mkdir -p /app/data/tts_cache /app/data/cache /app/data/records /app/shared

EXPOSE 8080 9090

CMD ["python3", "app/main.py"]