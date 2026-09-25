# -*- coding: utf-8 -*-
"""探测：**合成按键能不能驱动两种热键后端**？

背景：之前只验证了 `GetAsyncKeyState`（轮询后端）看不到合成按键，就据此判定
"本环境无法自动验证热键"，把整块测试标成 SKIP。后来发现那次失败是**暂时的**
（当时前台被一个模态对话框占着），合成输入本身是能投递的。

这个探测同时暴露了钩子后端的两个真 bug（已修）：

1. `CallNextHookEx` 没声明签名 → `lparam` 被当成 c_int 溢出 → 钩子过程抛异常。
   而钩子过程返回 0 表示"已处理"，**会把每一个按键都吞掉**。
2. 钩子报的是左右分开的修饰键（`VK_LCONTROL=0xA2`），跟配置里的通用 VK
   （`VK_CONTROL=0x11`）对不上 → 组合键**永远不触发**。

退出码 0 = 至少一种后端能被合成按键驱动。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import win32 as w32                                  # noqa: E402
from client.hotkey import PollingHotkey, HookHotkey, canonical_vk  # noqa: E402

VK_CONTROL, VK_MENU = 0x11, 0x12
VK_LCONTROL, VK_LMENU = 0xA2, 0xA4


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def inject(vk: int, up: bool = False) -> int:
    w32.user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    w32.user32.SendInput.restype = ctypes.c_uint
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=2 if up else 0, time=0, dwExtraInfo=None)
    return int(w32.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))


def synth_combo(hold: float = 0.45) -> None:
    inject(VK_MENU, up=True)
    inject(VK_CONTROL, up=True)
    time.sleep(0.08)
    inject(VK_CONTROL)
    time.sleep(0.12)
    inject(VK_MENU)
    time.sleep(hold)
    inject(VK_MENU, up=True)
    time.sleep(0.08)
    inject(VK_CONTROL, up=True)


def test_backend(name: str, factory) -> tuple[bool, str, list]:
    events: list[str] = []
    lock = threading.Lock()

    def rec(kind: str) -> None:
        with lock:
            events.append(kind)

    hk = factory(rec)
    try:
        hk.start()
    except Exception as exc:  # noqa: BLE001
        return False, "启动失败：%r" % exc, []
    time.sleep(0.35)
    synth_combo()
    time.sleep(0.5)
    hk.stop()
    with lock:
        got = list(events)
    ok = got[:2] == ["press", "release"]
    return ok, "事件序列=%s" % (got or "（无）"), getattr(hk, "debug_events", [])


def focus_line() -> str:
    fg = w32.foreground_window()
    return "前台=%r [%s] pid=%d" % (w32.window_title(fg)[:34], w32.class_name(fg),
                                    w32.window_pid(fg))


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print("=" * 72)
    print("探测：合成按键能否驱动热键后端（%d 轮，观察是否稳定）" % rounds)
    print("=" * 72)
    print("修饰键归一化检查：0x%02X→0x%02X  0x%02X→0x%02X  0x11→0x%02X"
          % (VK_LCONTROL, canonical_vk(VK_LCONTROL), VK_LMENU, canonical_vk(VK_LMENU),
             canonical_vk(0x11)))
    print("初始 %s\n" % focus_line())

    tally = {"polling": 0, "hook": 0, "hook_raw": 0}
    for i in range(rounds):
        ok_p, _dp, _ = test_backend(
            "polling", lambda rec: PollingHotkey(keys=["ctrl", "alt"], poll_ms=5,
                                                 on_press=lambda: rec("press"),
                                                 on_release=lambda: rec("release")))
        ok_h, _dh, raw_hook = test_backend(
            "hook", lambda rec: HookHotkey(keys=["ctrl", "alt"], swallow=False,
                                           on_press=lambda: rec("press"),
                                           on_release=lambda: rec("release")))
        tally["polling"] += int(ok_p)
        tally["hook"] += int(ok_h)
        tally["hook_raw"] += int(bool(raw_hook))
        print("第 %d 轮：polling=%s  hook=%s  钩子收到原始事件=%d 条 | %s"
              % (i + 1, "触发" if ok_p else "无", "触发" if ok_h else "无",
                 len(raw_hook), focus_line()))

    print("\n" + "=" * 72)
    print("汇总（共 %d 轮）：polling 触发 %d 次；hook 触发 %d 次；"
          "钩子收到过原始输入 %d 次" % (rounds, tally["polling"], tally["hook"],
                                        tally["hook_raw"]))
    print("-" * 72)
    if tally["polling"] == rounds and tally["hook"] == rounds:
        print("结论：合成按键**稳定可用**，热键可自动验证。")
    elif tally["polling"] or tally["hook"] or tally["hook_raw"]:
        print("结论：合成按键**时通时不通** —— 不能作为稳定的自动化验收手段。")
        print("      热键的最终验收仍需人在正常桌面会话完成。")
    else:
        print("结论：本轮合成按键完全没投递到桌面，热键验收只能人工完成。")
    return 0 if (tally["polling"] or tally["hook"]) else 1


if __name__ == "__main__":
    sys.exit(main())
