#!/bin/bash
# CherryVoice · WSL 流式识别服务启动器
#
# 由 systemd 用户服务（scripts/r2t2-stream.service）或手动调用。
# 关键点：venv 里装了 cudart/cublas，而 WSL 只有驱动层 libcuda.so，
# 所以运行前必须把这两个目录塞进 LD_LIBRARY_PATH（见 需求文档 §6.2①）。
set -euo pipefail

VENV="${R2T2_VENV:-/home/r2t2/Confucius4-R2T2/.venv}"
APP_DIR="${R2T2_APP_DIR:-/home/r2t2/cherryvoice}"

export LD_LIBRARY_PATH="$VENV/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:$VENV/lib/python3.12/site-packages/nvidia/cublas/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GGUF_DIR="${GGUF_DIR:-/home/r2t2/models}"
export R2T2_REPO="${R2T2_REPO:-/home/r2t2/Confucius4-R2T2}"
export R2T2_HF_DIR="${R2T2_HF_DIR:-/home/r2t2/Confucius4-R2T2-hf}"
export R2T2_STREAM_HOST="${R2T2_STREAM_HOST:-0.0.0.0}"
export R2T2_STREAM_PORT="${R2T2_STREAM_PORT:-18300}"
export R2T2_CHUNK_MS="${R2T2_CHUNK_MS:-160}"
export R2T2_LOG_DIR="${R2T2_LOG_DIR:-$APP_DIR/logs}"
export PYTHONUNBUFFERED=1

mkdir -p "$R2T2_LOG_DIR"
cd "$APP_DIR" || exit 1
exec "$VENV/bin/python" -u "$APP_DIR/r2t2_stream_server.py" "$@"
