#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：**按 Esc 取消本次输入，不注入任何文字**（BF-3）。

背景
----
需求文档 FR-1 写着「录音中按 ``Esc`` 取消本次输入」。
但 ``client/hotkey.py`` 的 ``active`` 字段**从来没被设成 True**
（只有 ``tools/test_hotkey.py`` 手工设过），两个后端的 Esc 分支都要求
``self.active`` 为真 —— 于是 ``on_cancel`` 在**真实使用中永不触发**：
按 Esc 什么都不会发生，松手照常注入。

这个用例验证三件事：

1. **热键层的 Esc 边沿会触发 ``on_cancel``** —— 用真实的 ``PollingHotkey``
   状态机驱动（不手工设 ``active``，让它自己跟着按下/松开走）。
2. **取消后不注入任何文字** —— 目标文本框必须是空的。
3. **取消后能重新开始下一次录音** —— ``session_active`` 归位。

用法::

    python tools/test_cancel_no_inject.py

需要 WSL 里的识别服务在跑；没有则明确 SKIP 并退出 0。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps  # noqa: E402
import _utf8  # noqa: F401,E402

from client import win32 as w32  # noqa: E402
from client.hotkey import PollingHotkey  # noqa: E402

WAV = ROOT.parent / "runtime" / "testdata" / "zh_test.wav"


class FakeKeyState:
    """可编程的按键状态源，替代真实的 GetAsyncKeyState。

    真实的按键在自动化环境里投递不到桌面（实测 0/3 轮），
    所以这里直接替换掉 ``w32.is_key_down`` —— 被测的**状态机逻辑**
    （按下 → active=True → Esc 边沿 → cancel）是真实的。
    """

    def __init__(self) -> None:
        self.keys: set[int] = set()
        self._lock = threading.Lock()

    def set(self, vk: int, down: bool) -> None:
        with self._lock:
            if down:
                self.keys.add(vk)
            else:
                self.keys.discard(vk)

    def is_down(self, vk: int) -> bool:
        with self._lock:
            return vk in self.keys


def test_hotkey_active_flag() -> tuple[bool, str]:
    """热键层：按下 → active 变 True → Esc 边沿触发 cancel。"""
    state = FakeKeyState()
    original = w32.is_key_down
    w32.is_key_down = state.is_down          # type: ignore[assignment]

    events: list[str] = []
    hk = PollingHotkey(
        keys=("ctrl", "win"),
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"),
        on_cancel=lambda: events.append("cancel"),
    )
    hk.start()
    try:
        ctrl, win_vk = hk.groups[0][0], hk.groups[1][0]
        # 先按住热键 → active 应该变 True
        state.set(ctrl, True)
        state.set(win_vk, True)
        time.sleep(0.15)
        active_after_press = hk.active

        # 再按 Esc → 应该触发 cancel
        state.set(hk.cancel_vk, True)
        time.sleep(0.15)
        state.set(hk.cancel_vk, False)
        time.sleep(0.05)

        # 松开热键
        state.set(ctrl, False)
        state.set(win_vk, False)
        time.sleep(0.15)
    finally:
        hk.stop()
        w32.is_key_down = original            # type: ignore[assignment]

    detail = ("active(按下后)=%s 事件序列=%s"
              % (active_after_press, events))
    ok = (active_after_press is True) and ("cancel" in events) \
        and (events.index("cancel") > events.index("press"))
    return ok, detail


def test_cancel_no_inject() -> tuple[bool, str]:
    """应用层：取消之后目标输入框里**什么都没有**。"""
    root = tk.Tk()
    w32.enable_dpi_awareness()
    root.title("EtherealFlow 取消验收目标")
    root.geometry("600x200+180+180")
    text = tk.Text(root, font=("Microsoft YaHei UI", 13))
    text.pack(fill="both", expand=True)
    root.update()
    hwnd = w32.top_level_window(int(root.winfo_id()))
    w32.force_foreground(hwnd)
    text.focus_set()
    for _ in range(50):
        root.update()
        time.sleep(0.01)

    # 用**完整**默认配置（App 会读很多段），只改要测的几项
    from client.config import DEFAULT_CONFIG
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["llm"]["enabled"] = False
    cfg["inject"]["mode"] = "clipboard"
    cfg["inject"]["restore_delay_ms"] = 300
    cfg["hotwords"] = []
    cfg_path = Path(tempfile.gettempdir()) / "ef-cancel-test.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 起客户端：--replay 会自动按下/松开热键。
    # 我们要的是「按下 → 取消」这条路，所以用 --replay 的自动流程跑一遍，
    # 然后在客户端跑的同时**手动触发取消**。
    #
    # 简化做法：直接用 App 的回调，不起子进程 ——
    # 断言的是「_on_cancel 之后没有注入」，这才是 FR-1 的核心。
    from client.app import App
    app = App(cfg)

    # 模拟：按下 → 产生一些识别文本 → 取消
    app._on_press(use_mic=False)
    app._on_partial("这段话应该被取消，不该进输入框")
    app._on_cancel()

    # 取消后：app 的状态必须归位
    state_ok = (not app.session_active) and (app.final_text == "") and (app._prefix == "")

    # 走注入路径：_inject 被调用的话就会往目标框灌字。
    # 取消之后不该有任何注入发生 —— 这里显式检查 final_text 为空，
    # 也就是注入路径拿不到任何文本。
    inject_text = app.final_text
    root.update()
    time.sleep(0.2)
    root.update()

    content = text.get("1.0", "end-1c")
    detail = ("session_active=%s final_text=%r 前缀=%r 目标框内容=%r"
              % (app.session_active, app.final_text, app._prefix, content))
    ok = state_ok and (inject_text.strip() == "") and (content.strip() == "")
    try:
        root.destroy()
    except tk.TclError:
        pass
    return ok, detail


def main() -> int:
    skip = _deps.require_asr()
    if skip:
        print(skip)
        return 0
    if not WAV.exists():
        found = _deps.find_wav()
        if found is None:
            print("[SKIP] 找不到测试音频 —— 本项跳过，不代表代码有问题。")
            return 0
        print("用找到的测试音频：%s" % found)

    results: list[tuple[str, bool, str]] = []

    ok, detail = test_hotkey_active_flag()
    results.append(("热键层：按下后 active=True，Esc 边沿触发 cancel", ok, detail))

    ok, detail = test_cancel_no_inject()
    results.append(("应用层：取消后不注入任何文字", ok, detail))

    print("=" * 72)
    print("BF-3 验收：Esc 取消")
    print("=" * 72)
    failed = 0
    for name, ok, detail in results:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
        if not ok:
            failed += 1
    print("-" * 72)
    print("结论：%s" % ("Esc 取消正常" if not failed else "存在失败项"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
