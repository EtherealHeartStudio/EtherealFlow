# -*- coding: utf-8 -*-
"""外部依赖探测 —— 让「环境做不到」与「代码错了」分得开。

**为什么需要**

有几个测试要连**真实的 WSL 识别服务**（ws://127.0.0.1:18300），
还有的要用真实音频、真实麦克风、真实桌面前台。这些在 CI runner 上
统统不具备。

按项目自己的规矩（``docs/development.md`` §3）：
「**区分「环境做不到」和「代码错了」。** 合成按键在受管环境里可能根本投递不到桌面，
这时应该标 ``[SKIP]`` 并写明原因，**不要标 ``[PASS]``**，也不要算成 ``[FAIL]``。」

以前这些脚本在缺依赖时会直接失败或静默跳过，CI 上表现为红，
把「环境缺服务」和「代码有 bug」混在一起。本模块给出统一的判定。

用法::

    from _deps import require_asr, require_wav

    skip = require_asr()
    if skip:
        print(skip)          # [SKIP] 识别服务未就绪 …
        return 0             # 退出码 0 = 不算失败
"""

from __future__ import annotations

import socket
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口能否连上。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def require_asr(host: str = "127.0.0.1", port: int = 18300) -> str | None:
    """识别服务在不在。

    返回 ``None`` 表示可用；否则返回一句「为什么不能测」的中文说明。
    """
    if port_open(host, port):
        return None
    return ("[SKIP] 识别服务未就绪（%s:%d 连不上）—— "
            "需要先在 WSL 里启动 r2t2-stream 服务。本项在 CI 上跳过，"
            "不代表代码有问题。" % (host, port))


def find_wav(explicit: str | None = None) -> Path | None:
    """找一份测试音频。

    优先用显式路径；否则按已知位置找：仓库外的 runtime/testdata、
    临时目录里的旧产物。找不到返回 None。
    """
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    candidates = [
        REPO.parent / "runtime" / "testdata" / "zh_test.wav",
        REPO / "runtime" / "testdata" / "zh_test.wav",
        REPO.parent / "runtime" / "testdata" / "long_test.wav",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None
