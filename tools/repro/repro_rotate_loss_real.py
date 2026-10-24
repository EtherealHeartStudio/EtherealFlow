#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对着**真实的 WSL 识别服务**复现「分段轮转期间音频丢失」。

用真实服务（ws://127.0.0.1:18300）+ 真实 WAV（runtime/testdata/zh_test.wav）。
流程与 client/app.py 完全一致：
  1. start（带 language + context）
  2. 喂第 1 段音频
  3. finish（= rotate）
  4. **在服务端 flush 期间继续喂音频**（模拟用户还在说话）
  5. 收到 final 后立刻 start（开新段）
  6. 喂第 3 段音频 → finish → final

对照：同样音频、不做轮转、一次性喂完再 finish，比较总字数。
"""
from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

WS_URL = "ws://127.0.0.1:18300"
WAV = Path(r"D:\AgentWorkSpace\CherryStudio\缥缈心流语音输入\runtime\testdata\zh_test.wav")
BLOCK = 5120  # 160ms @16k int16


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        return wf.readframes(wf.getnframes())


def split_blocks(pcm: bytes) -> list[bytes]:
    return [pcm[i:i + BLOCK] for i in range(0, len(pcm), BLOCK)]


async def recv_until_final(ws) -> str:
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        if msg.get("type") == "final":
            return msg.get("text", "")
        if msg.get("type") == "error":
            print("[error]", msg.get("message"))
            return ""


async def scenario_rotate(blocks: list[bytes]) -> tuple[str, str]:
    """带轮转：中途 finish，然后继续喂，再 start，再喂。"""
    async with websockets.connect(WS_URL, max_size=None, ping_interval=20,
                                  ping_timeout=60) as ws:
        await ws.send(json.dumps({"type": "start", "language": "Chinese",
                                  "context": "热词测试"}))
        await asyncio.sleep(0.2)

        third = len(blocks) // 3
        # 第 1 段
        for b in blocks[:third]:
            await ws.send(b)
        # 轮转：finish
        await ws.send(json.dumps({"type": "finish"}))
        # 关键：**在服务端 flush 期间继续喂**（用户还在说话）
        for b in blocks[third:2 * third]:
            await ws.send(b)
        first_final = await recv_until_final(ws)
        # 新段 start（client/app.py _on_segment_final 的行为）
        await ws.send(json.dumps({"type": "start", "language": "Chinese",
                                  "context": "热词测试"}))
        await asyncio.sleep(0.2)
        for b in blocks[2 * third:]:
            await ws.send(b)
        await ws.send(json.dumps({"type": "finish"}))
        second_final = await recv_until_final(ws)
    return first_final, second_final


async def scenario_plain(blocks: list[bytes]) -> str:
    """不轮转：一次性喂完再 finish（对照组）。"""
    async with websockets.connect(WS_URL, max_size=None, ping_interval=20,
                                  ping_timeout=60) as ws:
        await ws.send(json.dumps({"type": "start", "language": "Chinese",
                                  "context": "热词测试"}))
        await asyncio.sleep(0.2)
        for b in blocks:
            await ws.send(b)
        await ws.send(json.dumps({"type": "finish"}))
        return await recv_until_final(ws)


async def main() -> int:
    if not WAV.exists():
        print("[SKIP] 找不到测试音频:", WAV)
        return 0
    blocks = split_blocks(load_pcm(WAV))
    print("音频：%d 块（%.1f 秒）" % (len(blocks), len(blocks) * 0.16))

    print("\n>>> 对照组：一次性喂完")
    plain = await scenario_plain(blocks)
    print("   总字数 =", len(plain), "|", plain[:60])

    print("\n>>> 实验组：中途轮转")
    f1, f2 = await scenario_rotate(blocks)
    print("   第一段 =", len(f1), "字 |", f1[:50])
    print("   第二段 =", len(f2), "字 |", f2[:50])
    total = len(f1) + len(f2)

    print("\n" + "=" * 72)
    print("对照组总字数 : %d" % len(plain))
    print("轮转组总字数 : %d" % total)
    print("丢失         : %d 字（%.0f%%）" % (len(plain) - total,
                                              100.0 * (len(plain) - total) / max(1, len(plain))))
    print("=" * 72)
    if len(plain) - total > 3:
        print("[FAIL] 轮转确实丢字了 —— 确认是真缺陷")
        return 1
    print("[PASS] 轮转未丢字")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
