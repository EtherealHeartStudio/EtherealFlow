#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CherryVoice · WSL 流式识别服务（WebSocket）。

把官方 Confucius4-R2T2 的 llama.cpp 流式后端（``R2T2LlamaASRModel.LlamaNative``）
包装成一个常驻 WebSocket 服务，为 Windows 端语音输入法客户端提供**累积**识别文本。

运行环境（见 需求文档 §6）::

    export LD_LIBRARY_PATH=/home/r2t2/Confucius4-R2T2/.venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/home/r2t2/Confucius4-R2T2/.venv/lib/python3.12/site-packages/nvidia/cublas/lib
    export GGUF_DIR=/home/r2t2/models
    /home/r2t2/Confucius4-R2T2/.venv/bin/python r2t2_stream_server.py

协议（需求文档 §7.1）

    客户端 -> 服务端
      二进制帧   16 kHz / 单声道 / int16 小端 PCM（建议 160 ms = 2560 采样 = 5120 字节）
      文本帧     {"type":"start","language":"Chinese","context":"热词"}   开始一个会话
      文本帧     {"type":"finish"}                                       音频结束，要最终结果
      文本帧     {"type":"cancel"}                                       丢弃本次会话（扩展）

    服务端 -> 客户端
      {"type":"ready",   "model":"confucius4-r2t2", ...}   连上即发（扩展）
      {"type":"partial", "text":"累积全文"}                 文本有变化时才发
      {"type":"final",   "text":"最终文本", ...}
      {"type":"error",   "message":"..."}

设计要点

* ``partial`` 是**累积全文**，客户端直接替换显示即可（后端偶发回滚也天然被覆盖）。
* 空文本的 ``partial`` 是正常的中间态（后端在还没有 ``<asr_text>`` 标签时会清空
  ``state.text``），服务端**不下发**空串，并保留最后一个非空文本兜底。
* 每个连接一个独立的 ``streaming_state``；GPU 推理用全局锁串行化。
* llama.cpp 在 C 层直接往 fd 1/2 打大量日志，Python 侧关不掉，启动时统一重定向到
  ``logs/native.log``，避免污染 systemd 日志。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:  # websockets >= 14
    from websockets.asyncio.server import serve as ws_serve
except ImportError:  # pragma: no cover - websockets < 14
    from websockets import serve as ws_serve  # type: ignore

# --------------------------------------------------------------------------- #
# 配置（全部可用环境变量覆盖）
# --------------------------------------------------------------------------- #

SAMPLE_RATE = 16000
MODEL_ID = "confucius4-r2t2"

REPO_DIR = os.environ.get("R2T2_REPO", "/home/r2t2/Confucius4-R2T2")
HF_DIR = os.environ.get("R2T2_HF_DIR", "/home/r2t2/Confucius4-R2T2-hf")
GGUF_DIR = os.environ.get("GGUF_DIR", "/home/r2t2/models")

HOST = os.environ.get("R2T2_STREAM_HOST", "0.0.0.0")
PORT = int(os.environ.get("R2T2_STREAM_PORT", "18300"))

CHUNK_MS = int(os.environ.get("R2T2_CHUNK_MS", "160"))
FIRST_LOOKAHEAD_MS = int(os.environ.get("R2T2_FIRST_LOOKAHEAD_MS", "160"))
UNFIXED_CHUNK_NUM = int(os.environ.get("R2T2_UNFIXED_CHUNK_NUM", "0"))
UNFIXED_TOKEN_NUM = int(os.environ.get("R2T2_UNFIXED_TOKEN_NUM", "1"))

# 长语音时服务端解码会接近甚至超过实时，客户端可能在 sendall 里阻塞几十秒而
# 顾不上回 pong。默认 20 s 的 keepalive 超时会把这种**正常背压**误判成死连接并
# 断开（实测 53.9 s 音频就是被这样打断的）。本地回环不需要这么激进，放宽到 60 s。
PING_INTERVAL = int(os.environ.get("R2T2_PING_INTERVAL", "20"))
PING_TIMEOUT = int(os.environ.get("R2T2_PING_TIMEOUT", "60"))
MAX_MESSAGE_BYTES = int(os.environ.get("R2T2_MAX_MESSAGE_BYTES",
                                       str(4 * 1024 * 1024)))

DEFAULT_LANGUAGE = os.environ.get("R2T2_LANGUAGE") or None
DEFAULT_CONTEXT = os.environ.get("R2T2_CONTEXT", "")

LOG_DIR = Path(os.environ.get("R2T2_LOG_DIR", "/home/r2t2/cherryvoice/logs"))
NATIVE_LOG_MAX_BYTES = 8 * 1024 * 1024

log = logging.getLogger("cherryvoice.stream")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def _setup_logging(level: str = "INFO") -> None:
    """我们自己的日志写 logs/service.log（独立 fd，不受 fd 重定向影响）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers[:] = [handler]


def _redirect_native_logs() -> None:
    """把 C 层（llama.cpp）写到 fd 1/2 的日志导流到独立文件。

    ``R2T2_STREAM_NATIVE_LOG=1`` 时保留原样（调试用）。
    """
    if os.environ.get("R2T2_STREAM_NATIVE_LOG", "0") == "1":
        log.info("保留 llama.cpp 原生日志（R2T2_STREAM_NATIVE_LOG=1）")
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "native.log"
    try:
        if path.exists() and path.stat().st_size > NATIVE_LOG_MAX_BYTES:
            path.unlink()
    except OSError:
        pass
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)


# --------------------------------------------------------------------------- #
# 推理引擎
# --------------------------------------------------------------------------- #

class StreamingEngine:
    """进程内单例模型；所有解码串行执行（单 GPU）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._asr: Any = None

        step = int(round(CHUNK_MS / 1000.0 * SAMPLE_RATE))
        lookahead = int(round(FIRST_LOOKAHEAD_MS / 1000.0 * SAMPLE_RATE))
        self.step_samples = step
        self.first_chunk_samples = step + lookahead

        # max_new_tokens 自适应，取自官方 example.py 的 run_streaming()
        self.mnt_first = max(1, int(self.first_chunk_samples / 1280))
        self.mnt_base = max(1, int(step / 1280))
        self.mnt_floor = min(32, max(4, 2 * int(step / 1280)))

    # -- 生命周期 ---------------------------------------------------------- #

    @property
    def ready(self) -> bool:
        return self._asr is not None

    def load(self) -> None:
        """加载模型（阻塞，约 3~9 秒）。必须在开始服务前调用。"""
        if REPO_DIR not in sys.path:
            sys.path.insert(0, REPO_DIR)
        from r2t2_llama import R2T2LlamaASRModel

        log.info("加载模型：processor=%s gguf=%s", HF_DIR, GGUF_DIR)
        t0 = time.monotonic()
        self._asr = R2T2LlamaASRModel.LlamaNative(
            processor_path=HF_DIR,
            gguf_dir=GGUF_DIR,
        )
        log.info("模型加载完成，用时 %.1f s", time.monotonic() - t0)

    def new_state(self, language: Optional[str], context: str) -> Any:
        state = self._asr.init_streaming_state(
            context=context or "",
            language=language,
            unfixed_chunk_num=UNFIXED_CHUNK_NUM,
            unfixed_token_num=UNFIXED_TOKEN_NUM,
            chunk_size_sec=CHUNK_MS / 1000.0,
        )
        # 首个块多等一个 lookahead，与官方 run_streaming() 的首块策略一致
        state.chunk_size_samples = self.first_chunk_samples
        return state

    # -- 解码 -------------------------------------------------------------- #

    def feed(self, session: "Session", pcm_int16: np.ndarray) -> str:
        """喂一段 PCM，返回累积文本（可能是空串）。"""
        state = session.state
        with self._lock:
            text, _fixed = self._asr.streaming_transcribe(pcm_int16, state, int(session.mnt))
            # 首块解码完成后恢复常规块长
            if state.chunk_size_samples != self.step_samples:
                state.chunk_size_samples = self.step_samples
        return (text or "").split("|")[0]

    def finish(self, session: "Session") -> str:
        state = session.state
        with self._lock:
            text = self._asr.finish_streaming_transcribe(state, self.mnt_first)
        return (text or "").split("|")[0]

    def adapt(self, session: "Session", text: str) -> None:
        """官方 run_streaming() 的 max_new_tokens 自适应（输出停滞就多给几个 token）。"""
        if len(text) > len(session.last_text):
            session.mnt = self.mnt_base
        else:
            session.mnt = session.mnt + 1
        session.mnt = min(self.mnt_floor, session.mnt)


class Session:
    """一个连接 / 一次说话对应的流式会话。"""

    def __init__(self, engine: StreamingEngine, language: Optional[str], context: str) -> None:
        self.engine = engine
        self.language = language
        self.context = context
        self.state = engine.new_state(language, context)
        self.mnt = engine.mnt_first
        self.last_text = ""
        self.text = ""
        self.samples = 0
        self.chunks = 0
        self.decode_ms = 0.0
        self.max_decode_ms = 0.0
        self.started_at = time.monotonic()
        self.finished = False

    @property
    def duration(self) -> float:
        return self.samples / SAMPLE_RATE

    def push(self, pcm_int16: np.ndarray) -> Optional[str]:
        """返回需要下发的 partial 文本；无变化或空串时返回 None。"""
        if pcm_int16.size == 0:
            return None
        self.samples += int(pcm_int16.size)
        t0 = time.monotonic()
        text = self.engine.feed(self, pcm_int16)
        dt = (time.monotonic() - t0) * 1000.0
        self.decode_ms += dt
        self.max_decode_ms = max(self.max_decode_ms, dt)
        self.chunks = int(self.state.chunk_id)
        self.engine.adapt(self, text)
        if not text or text == self.last_text:
            return None
        self.last_text = text
        self.text = text
        return text

    def flush(self) -> str:
        """音频结束：冲洗尾部缓冲，返回最终文本（空则回退到最后一个非空 partial）。"""
        if not self.finished:
            self.finished = True
            final = self.engine.finish(self)
            if final:
                self.text = final
        return self.text

    def stats(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "chunks": self.chunks,
            "decode_ms": round(self.decode_ms, 1),
            "max_decode_ms": round(self.max_decode_ms, 1),
            "wall_ms": round((time.monotonic() - self.started_at) * 1000.0, 1),
        }


# --------------------------------------------------------------------------- #
# WebSocket 处理
# --------------------------------------------------------------------------- #

class Server:
    def __init__(self, engine: StreamingEngine) -> None:
        self.engine = engine
        self.clients = 0

    async def handler(self, websocket: Any) -> None:
        peer = getattr(websocket, "remote_address", None)
        self.clients += 1
        log.info("客户端接入 %s（当前 %d 个连接）", peer, self.clients)
        session: Optional[Session] = None
        try:
            await websocket.send(json.dumps({
                "type": "ready",
                "model": MODEL_ID,
                "sample_rate": SAMPLE_RATE,
                "chunk_ms": CHUNK_MS,
                "protocol": 1,
            }, ensure_ascii=False))

            async for message in websocket:
                if isinstance(message, (bytes, bytearray, memoryview)):
                    raw = bytes(message)
                    if len(raw) % 2:  # int16 对齐保护
                        raw = raw[:-1]
                    if not raw:
                        continue
                    if session is None:
                        session = Session(self.engine, DEFAULT_LANGUAGE, DEFAULT_CONTEXT)
                        log.info("%s 未发 start 帧，按默认参数开会话", peer)
                    pcm = np.frombuffer(raw, dtype=np.int16)
                    text = await asyncio.to_thread(session.push, pcm)
                    if text is not None:
                        await websocket.send(json.dumps(
                            {"type": "partial", "text": text}, ensure_ascii=False))
                    continue

                # ---- 文本帧 ----
                try:
                    payload = json.loads(message)
                except (TypeError, ValueError):
                    await self._error(websocket, "文本帧不是合法 JSON")
                    continue
                mtype = str(payload.get("type") or "").lower()

                if mtype == "start":
                    if session is not None and not session.finished:
                        log.info("%s 重开会话，丢弃旧会话（%.2f s）", peer, session.duration)
                    language = payload.get("language")
                    language = str(language).strip() if language else None
                    context = payload.get("context") or DEFAULT_CONTEXT
                    session = Session(self.engine, language or None, str(context))
                    log.info("%s start: language=%s context=%r", peer, language, context[:60])
                    await websocket.send(json.dumps(
                        {"type": "started", "language": language, "context": str(context)},
                        ensure_ascii=False))

                elif mtype == "finish":
                    if session is None:
                        await self._error(websocket, "finish 之前没有收到任何音频")
                        continue
                    final = await asyncio.to_thread(session.flush)
                    stats = session.stats()
                    log.info("%s finish: %.2f s 音频 → %d 字 | %s",
                             peer, stats["duration"], len(final), stats)
                    await websocket.send(json.dumps(
                        {"type": "final", "text": final, **stats}, ensure_ascii=False))
                    session = None

                elif mtype == "cancel":
                    log.info("%s cancel（丢弃 %.2f s 音频）",
                             peer, session.duration if session else 0.0)
                    session = None
                    await websocket.send(json.dumps({"type": "cancelled"}, ensure_ascii=False))

                elif mtype == "ping":
                    await websocket.send(json.dumps({"type": "pong"}, ensure_ascii=False))

                else:
                    await self._error(websocket, "未知帧类型：%r" % mtype)

        except Exception as exc:  # noqa: BLE001 - 单个连接出错不能拖垮服务
            log.warning("连接 %s 异常结束：%r", peer, exc)
        finally:
            self.clients -= 1
            log.info("客户端断开 %s（剩余 %d 个连接）", peer, self.clients)

    @staticmethod
    async def _error(websocket: Any, message: str) -> None:
        log.warning("下发 error：%s", message)
        try:
            await websocket.send(json.dumps({"type": "error", "message": message},
                                            ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 自检 / 入口
# --------------------------------------------------------------------------- #

def run_selftest(engine: StreamingEngine, wav_path: str) -> int:
    """脱离网络，用一份 WAV 跑一遍流式循环（P1 验收用）。"""
    import librosa

    wav, _sr = librosa.load(wav_path, sr=SAMPLE_RATE, mono=True)
    pcm = np.clip(np.asarray(wav, dtype=np.float32) * 32768.0, -32768, 32767).astype(np.int16)

    session = Session(engine, DEFAULT_LANGUAGE, DEFAULT_CONTEXT)
    step = engine.step_samples
    block = step if engine.first_chunk_samples == step else engine.first_chunk_samples
    pos = 0
    t0 = time.monotonic()
    while pos < pcm.size:
        piece = pcm[pos:pos + block]
        pos += piece.size
        text = session.push(piece)
        if text is not None:
            print("[%6.2fs] partial: %s" % (time.monotonic() - t0, text), flush=True)
        block = step
        if piece.size < step:
            break
    final = session.flush()
    print("FINAL: %s" % final, flush=True)
    print("stats: %s" % session.stats(), flush=True)
    return 0


async def _amain(args: argparse.Namespace) -> int:
    engine = StreamingEngine()
    if args.selftest_wav:
        await asyncio.to_thread(engine.load)
        return await asyncio.to_thread(run_selftest, engine, args.selftest_wav)

    await asyncio.to_thread(engine.load)

    server = Server(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    async with ws_serve(server.handler, HOST, PORT,
                        ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT,
                        max_size=MAX_MESSAGE_BYTES) as ws_server:
        log.info("流式识别服务已就绪：ws://%s:%d（chunk=%d ms，ping %d/%d s）",
                 HOST, PORT, CHUNK_MS, PING_INTERVAL, PING_TIMEOUT)
        await stop.wait()
        ws_server.close()
    log.info("服务退出")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CherryVoice WSL 流式识别服务")
    parser.add_argument("--selftest-wav", metavar="WAV",
                        help="不启服务，直接对一份 WAV 跑流式自检")
    parser.add_argument("--log-level", default=os.environ.get("R2T2_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    _setup_logging(args.log_level)
    _redirect_native_logs()
    log.info("=" * 60)
    log.info("CherryVoice 流式识别服务启动 | pid=%d", os.getpid())
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
