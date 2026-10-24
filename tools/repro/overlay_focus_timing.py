#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验：悬浮窗在**哪一步**抢走了前台？

诊断脚本 `overlay_focus_diag.py` 查出：
  · 悬浮窗的顶层窗口**确实**带 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
  · 但创建 Overlay 对象的过程中，前台窗口**变成了悬浮窗自己**

假设：`tk.Tk()` 创建窗口时 Tk 会主动映射并激活它，
而 `WS_EX_NOACTIVATE` 是在 `update_idletasks()` **之后**才加的 ——
加得太晚，窗口已经被激活过了。

本脚本对三种顺序做对照，每种都测「创建 Overlay 前后前台窗口变没变」。

用法：python tools/repro/overlay_focus_timing.py
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from client import win32 as w32   # noqa: E402


def fg_desc() -> str:
    fg = w32.foreground_window()
    return "0x%X" % fg


def scenario_a_current() -> int:
    """现状：tk.Tk() → withdraw() → build → update_idletasks → 加样式。"""
    base = w32.foreground_window()
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    tk.Label(root, text="x").pack()
    root.update_idletasks()               # ← 窗口在这里被映射
    top = w32.top_level_window(int(root.winfo_id()))
    w32.apply_noactivate(int(root.winfo_id()))
    after = w32.foreground_window()
    root.destroy()
    return base, after, top


def scenario_b_early_style() -> int:
    """改进：tk.Tk() 后**立刻**加样式（先 withdraw 再加，再 update_idletasks）。"""
    base = w32.foreground_window()
    root = tk.Tk()
    root.withdraw()
    top = w32.top_level_window(int(root.winfo_id()))
    w32.apply_noactivate(int(root.winfo_id()))   # ← 提前到映射之前
    root.overrideredirect(True)
    tk.Label(root, text="x").pack()
    root.update_idletasks()
    after = w32.foreground_window()
    root.destroy()
    return base, after, top


def scenario_c_no_update() -> int:
    """再试：干脆不调 update_idletasks，看映射本身是不是元凶。"""
    base = w32.foreground_window()
    root = tk.Tk()
    root.withdraw()
    top = w32.top_level_window(int(root.winfo_id()))
    w32.apply_noactivate(int(root.winfo_id()))
    root.overrideredirect(True)
    tk.Label(root, text="x").pack()
    # 不调 update_idletasks
    after = w32.foreground_window()
    root.destroy()
    return base, after, top


def main() -> int:
    print("=" * 74)
    print("对照实验：悬浮窗在创建流程的哪一步抢走前台")
    print("=" * 74)
    for name, fn in (("A 现状（先 update_idletasks，后加样式）", scenario_a_current),
                     ("B 提前加样式（映射之前）", scenario_b_early_style),
                     ("C 提前加样式 + 不调 update_idletasks", scenario_c_no_update)):
        # 注意：**必须在窗口 destroy 之前**取「创建后」的前台，
        # 否则 destroy() 会把前台还回去，测出来永远是「没抢」。
        result = fn()
        base, after, top = result
        stolen = after != base
        verdict = "抢了焦点 ❌" if stolen else "没抢 ✅"
        print("%-42s 前台 %s → %s  顶层=%s  %s"
              % (name, hex(base), hex(after), hex(top), verdict))
        print()
    print("=" * 74)
    print("结论：若 B/C 通过，则根因是「WS_EX_NOACTIVATE 加得太晚」——")
    print("      窗口在 update_idletasks() 时已被 Tk 映射并激活，")
    print("      之后再加 NOACTIVATE 就来不及了。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
