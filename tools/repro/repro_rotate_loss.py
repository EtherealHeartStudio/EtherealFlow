#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现「分段轮转期间音频丢失」。

场景：asr.rotate() 把 finish 帧排队 → 服务端 flush（慢）→ 客户端在这期间继续喂音频
→ 服务端收到音频时 session is None，自动用**默认参数**开新会话
→ 客户端收到 final 后发 start → 服务端「丢弃旧会话」→ 这段音频全没了。

本脚本不依赖 WSL 服务，直接用 client.asr.AsrClient 的队列语义 + 假服务端复现。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from client.asr import AsrClient  # noqa: E402


class FakeServer:
    """假服务端：复刻 wsl/r2t2_stream_server.py 的关键语义。

    - 收到 finish → flush（耗时 flush_delay 秒）→ 发 final → session = None
    - 收到二进制且 session is None → **自动开默认会话**（丢用户 language/context）
    - 收到 start 且旧会话未 finished → **丢弃旧会话**（这正是丢音频的地方）
    """

    def __init__(self, flush_delay: float = 0.5):
        self.session = None
        self.flush_delay = flush_delay
        self.audio_seen_in_autosession = 0
        self.discarded_audio_chars = 0
        self.finals: list[str] = []

    def on_start(self, payload: dict) -> dict:
        if self.session is not None:
            # 服务端「丢弃旧会话」——旧会话里的音频就此丢失
            self.discarded_audio_chars += len(self.session)
            self.session = None
        self.session = ""
        return {"type": "started", "language": payload.get("language"),
                "context": payload.get("context")}

    def on_audio(self, nbytes: int) -> None:
        if self.session is None:
            # 自动开默认会话：丢了用户的 language / context
            self.session = ""
            self.audio_seen_in_autosession += nbytes
        self.session += "字" * (nbytes // 2)

    def on_finish(self) -> dict:
        if self.session is None:
            return {"type": "error", "message": "finish 之前没有收到任何音频"}
        final = self.session
        self.finals.append(final)
        self.session = None
        return {"type": "final", "text": final, "duration": 1.0}


def main() -> int:
    server = FakeServer()
    events: list[tuple[str, object]] = []
    lock = threading.Lock()

    def on_partial(t):
        with lock:
            events.append(("partial", t))

    def on_final(m):
        with lock:
            events.append(("final", m.get("text", "")))

    def on_segment_final(m):
        with lock:
            events.append(("segment_final", m.get("text", "")))
        # 复刻 app._on_segment_final：立刻开新段
        client.begin_session(language="Chinese", context="热词XYZ")

    client = AsrClient(url="ws://unused", on_partial=on_partial, on_final=on_final,
                       on_segment_final=on_segment_final)
    # 用假服务端替换真实 WS
    def fake_run():
        # 模拟客户端工作线程：把队列里的东西喂给假服务端
        while not client._stop.is_set():
            try:
                kind, payload = client._q.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "start":
                server.on_start(payload)
            elif kind == "audio":
                server.on_audio(len(payload))
            elif kind == "finish":
                time.sleep(server.flush_delay)  # 模拟 flush 耗时
                msg = server.on_finish()
                if client._rotating:
                    client._rotating = False
                    on_segment_final(msg)
                else:
                    on_final(msg)
            elif kind == "cancel":
                server.session = None
    client._thread = threading.Thread(target=fake_run, daemon=True)
    client._thread.start()

    # --- 场景：第一段说话 ---
    client.begin_session(language="Chinese", context="热词XYZ")
    for _ in range(10):
        client.feed(b"\x01\x02" * 1280)   # 每块 2560 采样
    time.sleep(0.2)

    # --- 场景：轮转 ---
    print(">> 触发 rotate()，随后在 flush 期间继续喂音频")
    client.rotate()
    time.sleep(0.05)                      # 服务端还在 flush
    for _ in range(10):                   # 这 10 块在 flush 期间产生
        client.feed(b"\x03\x04" * 1280)
    time.sleep(1.0)

    time.sleep(0.3)
    client.close()

    finals = server.finals
    print()
    print("=" * 70)
    print("第一段 final 字数 :", len(finals[0]) if finals else 0)
    print("第二段 final 字数 :", len(finals[1]) if len(finals) > 1 else 0)
    print("服务端丢弃的字符数:", server.discarded_audio_chars)
    print("自动会话收到的音频字节:", server.audio_seen_in_autosession)
    print("=" * 70)

    ok = True
    if len(finals) < 2:
        print("[FAIL] 没有拿到两段 final")
        ok = False
    if server.discarded_audio_chars > 0:
        print("[FAIL] 服务端丢弃了 %d 字 —— 轮转期间的音频确实丢了"
              % server.discarded_audio_chars)
        ok = False
    if ok:
        print("[PASS] 轮转期间音频未丢失")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
