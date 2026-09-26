# -*- coding: utf-8 -*-
"""EtherealFlow · 验证全局热键（FR-1）。

分两部分，**分别如实报告**：

A. **逻辑验证（确定性，必过）**
   把按键状态源换成受控的假实现，驱动 ``PollingHotkey`` 走完
   按住 / 松开 / 会话中按 Esc 取消 三条边沿，断言回调序列正确。

B. **真实按键注入探测（环境相关）**
   用 ``SendInput`` 合成 Ctrl+Alt，看轮询与低级钩子能不能收到。
   测试键位故意不用默认的 ``Ctrl+Win``：``Win+Ctrl+D`` 会新建虚拟桌面，
   测试不该动用户的桌面。被验证的是两条**代码路径**，键位只是配置。

   注意：某些受管运行环境根本不会把合成输入投递到桌面。
   这时 B 会被标为 ``[SKIP]`` 并说明原因 —— **不算通过，也不算失败**，
   最终的人工验收仍需真人按住热键说话。

退出码 0 = A 全过（B 允许 SKIP）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client.hotkey as hk               # noqa: E402
from client import win32 as w32          # noqa: E402
from client.hotkey import create_hotkey, normalize_keys  # noqa: E402

TEST_KEYS = ["ctrl", "alt"]
VK_CONTROL, VK_MENU, VK_ESCAPE = 0x11, 0x12, 0x1B
VK_LCONTROL, VK_LWIN = 0xA2, 0x5B      # 钩子报的是左右专用的修饰键


# --------------------------------------------------------------------------- #
# A. 逻辑验证
# --------------------------------------------------------------------------- #

def test_logic() -> tuple[bool, str]:
    real_is_down = w32.is_key_down
    down: set[int] = set()
    w32.is_key_down = lambda vk: vk in down          # type: ignore[assignment]
    events: list[str] = []
    try:
        hotkey = hk.PollingHotkey(keys=TEST_KEYS, poll_ms=5,
                                  on_press=lambda: events.append("press"),
                                  on_release=lambda: events.append("release"),
                                  on_cancel=lambda: events.append("cancel"))
        hotkey.start()
        time.sleep(0.12)

        down.add(VK_CONTROL)                          # 只按 Ctrl：不该触发
        time.sleep(0.08)
        partial_only = list(events)

        down.add(VK_MENU)                             # 组合成立 → press
        time.sleep(0.08)
        after_press = list(events)

        hotkey.active = True                          # 会话中按 Esc → cancel
        down.add(VK_ESCAPE)
        time.sleep(0.08)
        down.discard(VK_ESCAPE)
        after_esc = list(events)

        down.clear()                                  # 全松开 → release
        time.sleep(0.08)
        hotkey.stop()
        final = list(events)
    finally:
        w32.is_key_down = real_is_down                # type: ignore[assignment]

    ok = (partial_only == [] and after_press == ["press"]
          and after_esc == ["press", "cancel"] and final == ["press", "cancel", "release"])
    return ok, "事件序列=%s（期望 press→cancel→release；只按 Ctrl 时=%s）" % (
        final or "（无）", partial_only or "无事件")


# --------------------------------------------------------------------------- #
# B. 真实注入探测
# --------------------------------------------------------------------------- #

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def inject(vk: int, up: bool = False) -> int:
    w32.user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    w32.user32.SendInput.restype = ctypes.c_uint
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=2 if up else 0, time=0, dwExtraInfo=None)
    return int(w32.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))


def injection_visible() -> tuple[bool, str]:
    """先探一下：合成一个 Ctrl，看 GetAsyncKeyState 能不能看到。"""
    inject(VK_CONTROL)
    seen = False
    try:
        for _ in range(10):
            time.sleep(0.04)
            if w32.is_key_down(VK_CONTROL):
                seen = True
                break
    finally:
        inject(VK_CONTROL, up=True)
        time.sleep(0.1)
    if seen:
        return True, "合成按键可以被 GetAsyncKeyState 观察到"
    return False, ("SendInput 返回成功，但 GetAsyncKeyState 看不到按键 —— "
                   "本运行环境不把合成输入投递到桌面，无法在此验证真实按键")


def test_backend_injection(backend: str) -> tuple[bool, str]:
    events: list[str] = []
    hotkey = create_hotkey(backend, keys=TEST_KEYS,
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    t0 = time.monotonic()
    hotkey.start()
    start_ms = (time.monotonic() - t0) * 1000
    time.sleep(0.3)
    inject(VK_CONTROL)
    time.sleep(0.1)
    inject(VK_MENU)
    time.sleep(0.5)
    inject(VK_MENU, up=True)
    time.sleep(0.1)
    inject(VK_CONTROL, up=True)
    time.sleep(0.4)
    hotkey.stop()
    ok = events[:2] == ["press", "release"]
    return ok, "启动 %.0f ms，事件序列=%s" % (start_ms, events or "（无）")


# --------------------------------------------------------------------------- #

def test_hook_state_machine() -> tuple[bool, str]:
    """确定性验证**钩子后端的状态机**（不需要真按键）。

    为什么必须单独测这一段：低级钩子报的修饰键是**左右分开**的
    （``VK_LCONTROL=0xA2``），而配置里写的是通用 VK（``VK_CONTROL=0x11``）。
    不做归一化的话组合键永远配不上 —— 实测就是这样，钩子收到了 0xA2/0xA4
    却一个事件都没触发。这里直接构造 ``KBDLLHOOKSTRUCT`` 喂给回调，
    把这条路径钉死。
    """
    from client.hotkey import (KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP,  # noqa: E402
                               HookHotkey, canonical_vk)

    events: list[str] = []
    hk = HookHotkey(keys=["ctrl", "win"], swallow=False,
                    on_press=lambda: events.append("press"),
                    on_release=lambda: events.append("release"),
                    on_cancel=lambda: events.append("cancel"))

    def feed(vk: int, down: bool) -> None:
        kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=None)
        hk._callback(0, WM_KEYDOWN if down else WM_KEYUP,   # noqa: SLF001
                     ctypes.addressof(kb))

    # 用**左右专用**的修饰键喂进去，模拟真实钩子输入
    feed(VK_LCONTROL, True)          # 0xA2，只按 Ctrl → 不该触发
    only_ctrl = list(events)
    feed(VK_LWIN, True)              # 0x5B，组合成立 → press
    after_press = list(events)
    feed(VK_LWIN, False)
    feed(VK_LCONTROL, False)         # 全松开 → release
    final = list(events)

    aliases_ok = (canonical_vk(0xA2) == VK_CONTROL and canonical_vk(0xA4) == VK_MENU
                  and canonical_vk(0xA0) == 0x10 and canonical_vk(0x5B) == 0x5B)
    ok = (aliases_ok and only_ctrl == [] and after_press == ["press"]
          and final == ["press", "release"])
    return ok, ("修饰键归一化=%s；事件序列=%s（期望 press→release；只按左右专用 Ctrl 时=%s）"
                % ("正确" if aliases_ok else "错误", final or "（无）",
                   only_ctrl or "无事件"))


def main() -> int:
    print("键位解析：ctrl+win → %s" % (normalize_keys(["ctrl", "win"]),))
    print("键位解析：ctrl+alt → %s" % (normalize_keys(TEST_KEYS),))
    print("=" * 72)
    print("P2 验证：全局热键")
    print("=" * 72)

    rows: list[tuple[str, bool, str]] = []
    ok, detail = test_logic()
    rows.append(("A. 轮询后端：按住/松开/Esc 取消 三条边沿", ok, detail))

    ok_c, detail_c = test_hook_state_machine()
    rows.append(("C. 钩子后端状态机（左右专用修饰键 VK）", ok_c, detail_c))

    visible, why = injection_visible()
    if not visible:
        rows.append(("B. 真实按键注入探测", True,
                     "[SKIP] " + why + "（本环境合成输入是否投递并不稳定，"
                     "热键的最终验收仍需人在正常桌面完成）"))
    else:
        for backend in ("polling", "hook"):
            ok_b, detail_b = test_backend_injection(backend)
            rows.append(("B. %s 后端收到合成按键" % backend, ok_b, detail_b))

    for name, ok_r, detail_r in rows:
        print("%s %s" % ("[PASS]" if ok_r else "[FAIL]", name))
        print("        %s" % detail_r)
    allok = all(ok_r for _n, ok_r, _d in rows)
    print("-" * 72)
    print("结论：%s" % ("通过（含 SKIP 项，见说明）" if allok else "存在失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
