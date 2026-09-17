#!/usr/bin/env bash
# ========================================================
#  zhihuatong - llama-server (Linux CUDA, WSL2)
#  对齐 run-llama-server.bat 的参数
#  用法: bash run-llama-server.sh           前台运行 (Ctrl+C 停止)
#        bash run-llama-server.sh --daemon  后台运行 + 日志落盘(供 llama-bridge 控制)
# ========================================================
set -u

# --- 运行模式 ---
# 默认前台（Ctrl+C 停止）；--daemon 后台运行 + 日志落盘（供 llama-bridge 控制）
if [ "${1:-}" = "--daemon" ]; then
    LOGS="$HOME/.unsloth/llama.cpp/logs"
    mkdir -p "$LOGS"
    SCRIPT="$(realpath "$0")"
    nohup bash "$SCRIPT" > "$LOGS/llama-server.log" 2>&1 &
    echo "llama-server 后台启动中 (PID $!)"
    echo "日志: $LOGS/llama-server.log"
    exit 0
fi

# --- 路径 ---
# 二进制与模型位于 WSL 内盘(避免 9p 读盘慢)
BIN="$HOME/.unsloth/llama.cpp/llama-server"
MDL="$HOME/.cache/huggingface/hub/models--unsloth--Qwen3.5-4B-MTP-GGUF/snapshots/86835bf9949e4d14d6860f7910b1340ad4f271a9/Qwen3.5-4B-UD-Q4_K_XL.gguf"

# unsloth 预编译包 rpath=$ORIGIN，加 LD_LIBRARY_PATH 兜底
export LD_LIBRARY_PATH="$HOME/.unsloth/llama.cpp/build/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# 槽位保存目录(与 Windows 版 data\llama-slots 对应)
SLOT_DIR="$(dirname "$(realpath "$0")")/data/llama-slots"

# --- 校验 ---
if [ ! -x "$BIN" ]; then
    echo "[ERROR] cannot find $BIN" >&2
    exit 1
fi
if [ ! -f "$MDL" ]; then
    echo "[ERROR] model file not found: $MDL" >&2
    exit 1
fi
mkdir -p "$SLOT_DIR"

echo "========================================================"
echo "  zhihuatong - llama-server (Linux CUDA)"
echo "========================================================"
echo "  Model: Qwen3.5-4B-MTP (Q4_K_XL)"
echo "  Port: 8081  Ctx: 16K"
echo "  Ctrl+C = Stop service"
echo "========================================================"
echo

exec "$BIN" -m "$MDL" \
    --host 0.0.0.0 --port 8081 \
    -c 16384 \
    -ngl -1 \
    --threads 8 \
    --flash-attn on \
    --no-context-shift \
    --cache-type-k bf16 --cache-type-v bf16 \
    --parallel 1 \
    --kv-unified \
    --fit off \
    --slot-save-path "$SLOT_DIR" \
    --alias unsloth/Qwen3.5-4B-MTP-GGUF \
    --chat-template-kwargs '{"enable_thinking": false}' \
    --spec-type draft-mtp --spec-draft-n-max 2 \
    --no-mmproj-auto \
    --jinja --metrics \
    --cache-ram 1024 --ctx-checkpoints 0
