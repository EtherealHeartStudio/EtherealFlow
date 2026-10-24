#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：悬浮窗为什么会成为前台窗口？

问题：`tools/test_closed_loop.py` 稳定报「前台是悬浮窗自己（0x…），跳过注入」。
需要区分两种可能：

  A. **代码错了**：悬浮窗真的把 WS_EX_NOACTIVATE 弄丢了，或加晚了
     （先被激活，再加样式）—— 那就是真缺陷，必须修。
  B. **环境限制**：Windows 的「前台锁」规则允许**由前台进程启动的子进程**
     设置前台。本测试正好是「测试进程（前台）→ 启动客户端子进程」，
     子进程创建窗口时系统会激活它。这是 Windows 的既定行为，
     真实使用场景（用户从启动器/快捷方式启动程序）不会这样。

判据：打印悬浮窗**成为前台那一刻**的扩展样式。
  - 若 WS_EX_NOACTIVATE 已置位 → 样式没丢，是激活发生在加样式**之前**
    或环境允许子进程激活 → 偏 B，但要看时序。
  - 若未置位 → 样式丢了 → A，真缺陷。

用法：python tools/repro/overlay_focus_diag.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from client import win32 as w32          # noqa: E402
from client.overlay import Overlay      # noqa: E402


def describe(hwnd: int) -> str:
    if not hwnd:
        return "0x0"
    ex = w32.window_exstyle(hwnd)
    return "0x%X ex=0x%X NOACTIVATE=%s TOOLWINDOW=%s" % (
        hwnd, ex, bool(ex & w32.WS_EX_NOACTIVATE), bool(ex & w32.WS_EX_TOOLWINDOW))


def main() -> int:
    print("=" * 74)
    print("诊断：悬浮窗的前台行为")
    print("=" * 74)

    # 记录创建前的前台
    base_fg = w32.foreground_window()
    print("创建前前台        :", describe(base_fg))

    ov = Overlay(width=600, opacity=0.9, font_size=14, translate=False)

    print("创建后（未显示）:")
    print("  child hwnd       :", describe(ov.hwnd))
    print("  top   hwnd       :", describe(ov.top_hwnd))
    print("  noactivate_ok    :", ov.noactivate_ok)
    print("  显示前的前台     :", describe(w32.foreground_window()))
    print("  前台是否变成悬浮窗:", w32.foreground_window() in (ov.hwnd, ov.top_hwnd))

    # 显示它
    ov.show_listening()
    time.sleep(0.4)
    fg = w32.foreground_window()
    print("显示后:")
    print("  前台             :", describe(fg))
    print("  前台是悬浮窗吗   :", fg in (ov.hwnd, ov.top_hwnd))
    print("  noactivate_ok    :", ov.noactivate_ok)

    ov.post("hide")
    time.sleep(0.3)
    ov.stop()

    # ---- 第二组：模拟 test_closed_loop 的场景（前台进程启动子进程） ----
    print()
    print("=" * 74)
    print("场景 B：前台进程启动子进程（Windows 前台锁允许子进程激活窗口）")
    print("=" * 74)

    code = (
        "import sys, time;"
        "sys.path.insert(0, %r);"
        "from client import win32 as w32;"
        "from client.overlay import Overlay;"
        "ov = Overlay(width=600, opacity=0.9, font_size=14);"
        "print('子进程：创建后前台 =', hex(w32.foreground_window()),"
        " 'top =', hex(ov.top_hwnd),"
        " 'noactivate_ok =', ov.noactivate_ok, flush=True);"
        "ov.show_listening(); time.sleep(0.5);"
        "fg = w32.foreground_window();"
        "print('子进程：显示后前台 =', hex(fg),"
        " '是悬浮窗吗 =', fg in (ov.hwnd, ov.top_hwnd), flush=True);"
        "ov.stop()"
    ) % str(REPO)
    subprocess.run([sys.executable, "-c", code], cwd=str(REPO))

    print()
    print("=" * 74)
    print("读法：")
    print("  · 若「显示后前台」仍是**原窗口**→ 悬浮窗不抢焦点，")
    print("    closed_loop 的失败是**场景 B**（前台锁），属环境限制。")
    print("  · 若「显示后前台」变成悬浮窗 → 悬浮窗真抢了焦点，是**代码缺陷**。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
