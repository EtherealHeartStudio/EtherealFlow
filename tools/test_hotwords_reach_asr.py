#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：**热词真的到达了识别引擎**（BF-4）。

背景
----
需求文档 FR-7 写着「词典：热词列表（每行一个），**传给识别引擎作为
context**」。但 v0.3.0 实际只把热词喂给了 LLM —— ``client/app.py`` 里
``begin_session`` 传的始终是 ``cfg["asr"]["context"]``，而 ``cfg["hotwords"]``
只走到 ``llm.correct(hotwords=...)``。

界面、README、config.py 的注释**三处都写着「会传给识别引擎」**，
实际没有 —— 文档全在撒谎。

这个用例验两件事：

1. **纯函数层** ``build_asr_context()`` 把热词与识别上下文正确合成
   （去重、限长、无热词时保持原样）。
2. **客户端层**：真的发出去的 ``start`` 帧里，``context`` 含有热词
   —— 用一个假 WebSocket 服务端接住客户端的帧来看。

用法::

    python tools/test_hotwords_reach_asr.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _utf8  # noqa: F401,E402

from client.textutil import smart_join  # noqa: E402,F401

# ⚠️ ``build_asr_context`` 是这次修复**新增**的函数。
# 用 try/except 导入，让「修复前」跑这个用例时得到一个**明确的 FAIL**，
# 而不是 ImportError 崩掉 —— 后者看起来像测试脚本坏了，不像产品有缺陷。
try:
    from client.textutil import ASR_CONTEXT_MAX_CHARS, build_asr_context
    _HAVE_BUILDER = True
except ImportError:
    ASR_CONTEXT_MAX_CHARS = 200
    build_asr_context = None          # type: ignore[assignment]
    _HAVE_BUILDER = False


def check_build_asr_context() -> tuple[bool, str]:
    """纯函数：热词与识别上下文的合成规则。"""
    if not _HAVE_BUILDER:
        return False, ("client.textutil.build_asr_context 不存在 —— "
                       "热词还没接到识别引擎上（BF-4 未修复）")

    cases: list[tuple[str, bool, str]] = []

    # 1) 热词进 context
    ctx = build_asr_context(["快手科技", "EtherealFlow"], "项目背景")
    cases.append(("热词出现在 context 里", "快手科技" in ctx and "EtherealFlow" in ctx, ctx))
    cases.append(("既有识别上下文保留", "项目背景" in ctx, ctx))
    cases.append(("热词排在上下文前面", ctx.index("快手科技") < ctx.index("项目背景"), ctx))

    # 2) 去重
    ctx2 = build_asr_context(["快手科技", "快手科技"], "")
    cases.append(("重复热词去重", ctx2.count("快手科技") == 1, ctx2))

    # 3) 逗号/顿号分隔也算分隔符
    ctx3 = build_asr_context(["快手科技，EtherealFlow"], "")
    cases.append(("中英文标点分隔都能拆开",
                  "快手科技" in ctx3 and "EtherealFlow" in ctx3, ctx3))

    # 4) 空白项忽略
    ctx4 = build_asr_context(["  ", "", "快手科技"], "")
    cases.append(("空白项被忽略", ctx4.strip() == "快手科技", repr(ctx4)))

    # 5) 没有热词时保持原样
    ctx5 = build_asr_context([], "只有上下文")
    cases.append(("没有热词时原样返回 context", ctx5 == "只有上下文", ctx5))
    ctx6 = build_asr_context([], "")
    cases.append(("都没有时返回空串", ctx6 == "", repr(ctx6)))

    # 6) 限长
    many = ["词%03d" % i for i in range(200)]
    ctx7 = build_asr_context(many, "")
    cases.append(("超长时截断到上限",
                   len(ctx7) <= ASR_CONTEXT_MAX_CHARS,
                   "%d 字（上限 %d）" % (len(ctx7), ASR_CONTEXT_MAX_CHARS)))

    failed = []
    for name, ok, detail in cases:
        if not ok:
            failed.append("%s —— %s" % (name, detail))
    return not failed, ("；".join(failed) if failed else "全部 %d 项通过" % len(cases))


def check_start_frame_carries_hotwords() -> tuple[bool, str]:
    """客户端层：发出去的 ``start`` 帧里含热词。

    用一个**假 WebSocket 服务端**接住客户端的帧 ——
    这样不用真服务也能验「客户端到底发了什么」。

    ⚠️ 用 **asyncio 版** 的 ``websockets.asyncio.server.serve``：
    同步版 ``websockets.sync.server.serve`` 在 ``__init__`` 里就跑事件循环，
    会把调用线程**堵死**（实测整个测试超时、无输出）。
    """
    import asyncio

    from websockets.asyncio.server import serve

    from client.asr import AsrClient

    captured: list[dict] = []
    port_holder: dict = {}

    async def handler(ws) -> None:  # noqa: ANN001
        try:
            await ws.send(json.dumps({"type": "ready", "model": "fake",
                                      "sample_rate": 16000, "chunk_ms": 160,
                                      "protocol": 1}))
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                captured.append(msg)
                if msg.get("type") == "start":
                    return
        except Exception:  # noqa: BLE001
            pass

    async def run_server() -> None:
        server = await serve(handler, "127.0.0.1", 0)
        port_holder["port"] = server.sockets[0].getsockname()[1]
        await asyncio.sleep(30)          # 够测试跑完

    def background() -> None:
        try:
            asyncio.run(run_server())
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=background, daemon=True, name="fake-asr")
    t.start()

    deadline = time.time() + 10
    while time.time() < deadline and "port" not in port_holder:
        time.sleep(0.02)
    if "port" not in port_holder:
        return False, "假服务端起不来"

    port = port_holder["port"]
    # ⚠️ 走 **App 层**（产品真正用的路径），而不是直接调 AsrClient ——
    # 合并热词这件事发生在 app._begin_asr_session() 里。
    # 修复前 app 传的始终是 cfg["asr"]["context"]，热词根本到不了这里。
    from client.app import App
    from client.config import DEFAULT_CONFIG

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["hotwords"] = ["快手科技", "EtherealFlow"]
    cfg["asr"]["context"] = ""
    cfg["llm"]["enabled"] = False

    app = App(cfg)
    app.asr = AsrClient(url="ws://127.0.0.1:%d" % port)
    app.asr.start()
    time.sleep(0.6)
    if hasattr(app, "_begin_asr_session"):
        app._begin_asr_session()
    else:
        # 修复前没有这个方法 → 走老路径（context 里没有热词），
        # 断言会明确报 FAIL，而不是 AttributeError 崩掉。
        app.asr.begin_session(language=cfg["asr"].get("language"),
                              context=cfg["asr"].get("context", ""))
    deadline = time.time() + 10
    while time.time() < deadline and not captured:
        time.sleep(0.05)
    time.sleep(0.3)
    app.asr.close()

    starts = [m for m in captured if m.get("type") == "start"]
    if not starts:
        return False, "没收到 start 帧（收到 %r）" % (captured,)
    ctx = starts[0].get("context", "")
    ok = "快手科技" in ctx and "EtherealFlow" in ctx
    return ok, "start 帧 context=%r" % ctx


def main() -> int:
    print("=" * 72)
    print("BF-4 验收：热词到达识别引擎")
    print("=" * 72)

    results: list[tuple[str, bool, str]] = []

    ok, detail = check_build_asr_context()
    results.append(("纯函数：build_asr_context 合成规则", ok, detail))

    ok, detail = check_start_frame_carries_hotwords()
    results.append(("客户端：start 帧的 context 含热词", ok, detail))

    failed = 0
    for name, ok, detail in results:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
        if not ok:
            failed += 1
    print("-" * 72)
    print("结论：%s" % ("热词确实到达识别引擎" if not failed else "存在失败项"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
