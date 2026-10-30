#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：**长语音分段（rotate）不丢内容**（BF-1）。

背景
----
连续说话超过 30 秒，客户端会「结束当前段 → 累积 final → 立刻开新的一段」
（见 ``client/asr.py:rotate``）。实测踩过：7.7 秒音频分三段喂，
对照组（一次性喂完）34 字，轮转组只有 23 字 —— **丢了 32%**。

原因链
------
1. 轮转时发 ``finish``，服务端开始 flush（要几秒）；
2. 这几秒用户还在说话，``feed()`` 照常推音频；
3. 服务端 flush 完把 session 置空，随后收到的音频会**按默认参数自动开新会话**
   （用户的 language / context 全丢）；
4. 客户端重开会话时发的 ``start`` 又把这个自动会话**丢弃** ——
   那段音频的文本永远进不了 final。

修法
----
* 客户端：分段期间把音频**扣住**（``AsrClient._audio_hold``），
  新会话 ``start`` 发出去之后再放行；
* 服务端：finish 之后不再自动开默认会话（``accepting`` 标志），兜底。

**这个用例走真实的 ``AsrClient``**（不是裸 WebSocket）——
裸协议客户端不扣音频，测不到客户端这一侧的修复。
对照组与实验组用**同一份音频、同一套代码**，唯一差别是「中途轮转」。

用法::

    python tools/test_rotate_no_loss.py

需要 WSL 里的识别服务在跑；没有则明确 SKIP 并退出 0。
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps  # noqa: E402
import _utf8  # noqa: F401,E402

from client.asr import AsrClient  # noqa: E402

BLOCK = 5120          # 160 ms @ 16 kHz int16
WS_URL = "ws://127.0.0.1:18300"


def load_blocks(path: Path) -> list[bytes]:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 16000, wf.getframerate()
        assert wf.getnchannels() == 1, wf.getnchannels()
        assert wf.getsampwidth() == 2, wf.getsampwidth()
        pcm = wf.readframes(wf.getnframes())
    return [pcm[i:i + BLOCK] for i in range(0, len(pcm), BLOCK)]


class Recorder:
    """驱动真实 AsrClient，按真实时序喂音频。

    复刻 ``client/app.py`` 的会话流程：
    ``_on_press`` → begin_session；``_on_segment_final`` → 累积前缀 + 重新 begin_session；
    ``_on_release`` → end_session。
    """

    def __init__(self) -> None:
        self.finals: list[str] = []          # 各段的 final 文本
        self.segments: list[str] = []        # 分段中途的 final
        self.errors: list[str] = []
        self.done = threading.Event()
        self._prefix = ""
        self._client: AsrClient | None = None
        self._lock = threading.Lock()

    # -- 回调（都在 ASR 工作线程触发） --

    def _on_segment_final(self, msg: dict) -> None:
        part = (msg.get("text") or "").strip()
        with self._lock:
            self.segments.append(part)
            self._prefix = part
        # 复刻 app._on_segment_final：立刻开新的一段（这会释放扣住的音频）
        assert self._client is not None
        self._client.begin_session(language="Chinese", context="",
                                   keep_end_pending=True)

    def _on_final(self, msg: dict) -> None:
        with self._lock:
            self.finals.append(msg.get("text") or "")
        self.done.set()

    def _on_error(self, msg: str) -> None:
        self.errors.append(msg)

    # -- 驱动 --

    def run(self, blocks: list[bytes], rotate_at: int | None) -> str:
        self._client = AsrClient(
            url=WS_URL,
            on_partial=lambda _t: None,
            on_final=self._on_final,
            on_segment_final=self._on_segment_final,
            on_error=self._on_error,
            on_status=lambda _s: None,
        )
        self._client.start()
        time.sleep(1.0)                       # 等连接建立
        self._client.begin_session(language="Chinese", context="")

        for i, block in enumerate(blocks):
            if rotate_at is not None and i == rotate_at:
                self._client.rotate()
                # 轮转在途：这段时间的音频会被客户端扣住
                time.sleep(0.05)
            self._client.feed(block)
            time.sleep(0.005)                # 模拟实时推流

        self._client.end_session()
        self.done.wait(timeout=180)
        self._client.close()
        with self._lock:
            # 完整文本 = 分段累积 + 最后一段
            return "".join(self.segments + self.finals)


def main() -> int:
    skip = _deps.require_asr()
    if skip:
        print(skip)
        return 0

    wav = _deps.find_wav()
    if wav is None:
        print("[SKIP] 找不到测试音频（需要 16k 单声道 WAV）—— 本项跳过，"
              "不代表代码有问题。")
        return 0
    blocks = load_blocks(wav)
    print("音频：%d 块（%.1f 秒）" % (len(blocks), len(blocks) * 0.16))

    print("\n>>> 对照组：一次性喂完")
    plain = Recorder().run(blocks, rotate_at=None)
    print("   文本 = %r" % plain)

    print("\n>>> 实验组：第 %d 块处轮转" % (len(blocks) // 3))
    rotated = Recorder().run(blocks, rotate_at=len(blocks) // 3)
    print("   文本 = %r" % rotated)

    print("\n" + "=" * 72)
    print("对照组字数 : %d" % len(plain))
    print("轮转组字数 : %d" % len(rotated))
    diff = len(plain) - len(rotated)
    print("差值       : %d 字（%.0f%%）" % (diff, 100.0 * diff / max(1, len(plain))))
    print("=" * 72)

    if diff <= 1:
        print("[PASS] 轮转未丢内容（差值 ≤ 1 字）")
        return 0
    print("[FAIL] 轮转丢了 %d 字 —— BF-1 未修复" % diff)
    return 1


if __name__ == "__main__":
    sys.exit(main())
