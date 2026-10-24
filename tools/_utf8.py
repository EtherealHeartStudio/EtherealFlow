# -*- coding: utf-8 -*-
"""强制标准输出用 UTF-8 —— 修 CI 的 cp1252 崩溃。

**为什么需要这个文件**

本项目几乎所有输出都是中文。Windows 上 Python 的标准输出编码默认跟
「活动代码页」走，在 GitHub Actions 的 Windows runner 上就是 **cp1252**，
于是第一句 ``print("将运行 11 个测试…")`` 就抛::

    UnicodeEncodeError: 'charmap' codec can't encode characters ...

v0.1.0 – v0.3.0 三个 Release 的 CI **全部死在这一行**，
打包步骤从来没被执行过（详见 ``docs/analysis/2026-10-24-代码图谱分析报告.md``）。

**两层防护，都要装**

1. 本模块：进程启动时把 stdout/stderr 换成 UTF-8。
   码换不了就不换（``reconfigure`` 不存在时静默跳过），
   但 ``errors="replace"`` 保证**永远不会因为编码问题崩掉**。
2. CI 里再设 ``PYTHONUTF8: 1``（见 ``.github/workflows/build.yml``）：
   那是环境变量级别的兜底，连**子进程**都覆盖到 ——
   ``tools/run_all_tests.py`` 用 subprocess 拉起其它测试脚本，
   那些脚本的 stdout 编码由它们自己的环境决定。

只有第 1 层时，直接跑单个测试脚本仍可能崩；只有第 2 层时，
本地开发者不设环境变量就仍可能崩。两层都装才稳。
"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8。

    幂等：重复调用无副作用。已经是非 UTF-8 时才动手，
    因为 ``reconfigure`` 会重置缓冲，在已经输出过内容的流上调用有风险。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower()
        if encoding.replace("-", "") in ("utf8", "utf16", "utf32"):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 极老的 Python 或被替换过的流：改不了 encoding 就只能靠 errors
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 编码问题绝不能拖垮程序
            pass


# 导入即生效：这样只要 `import _utf8` 就能拿到保护，
# 不需要调用方记得调 force_utf8()。
force_utf8()
