# -*- coding: utf-8 -*-
"""审计：所有用到的 Win32 函数是否都**显式声明了 ctypes 签名**。

为什么值得单独写个测试：上一轮刚在低级钩子上踩过一次 ——
``CallNextHookEx`` 没声明 ``argtypes``，64 位的 ``lparam`` 被当成 ``c_int`` 溢出，
钩子过程抛异常等价于返回 0，**把每一个按键都吞掉**。

这类 bug 的共同点是：

* 在 64 位下才发作；
* 句柄/指针值大于 2^31 时才发作（所以本机大多"看起来正常"）；
* 不报错、只是**行为诡异**（定位错、抢不到前台、钩子不触发）。

所以这里不靠人眼 review，而是列一张清单自动查。

退出码 0 = 清单里每个函数都声明齐全。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client.hotkey as hk          # noqa: E402
import client.inject as inj         # noqa: E402
import client.win32 as w32          # noqa: E402

# 接受句柄/指针参数的函数：不声明 argtypes 就会把 64 位指针截成 c_int
NEEDS_ARGTYPES = {
    "user32": [
        "OpenClipboard", "EmptyClipboard", "CloseClipboard", "EnumClipboardFormats",
        "SetClipboardData", "GetClipboardData", "GetWindowLongW", "SetWindowLongW",
        "ShowWindow", "SetWindowPos", "SetForegroundWindow", "BringWindowToTop",
        "AttachThreadInput", "SwitchToThisWindow", "GetWindowThreadProcessId",
        "IsWindowVisible", "GetAsyncKeyState", "GetCursorPos", "MonitorFromPoint",
        "GetMonitorInfoW", "EnumWindows", "GetWindowTextW", "GetWindowTextLengthW",
        "GetClassNameW", "CallNextHookEx", "SetWindowsHookExW", "UnhookWindowsHookEx",
        "GetMessageW", "TranslateMessage", "DispatchMessageW", "PostThreadMessageW",
        "PostMessageW", "SendInput", "keybd_event",
    ],
    "kernel32": ["GlobalAlloc", "GlobalLock", "GlobalUnlock", "GetCurrentThreadId"],
}

# 返回值是句柄/指针/句柄数组的函数：restype 不声明就会被截成 32 位
NEEDS_POINTER_RESTYPE = {
    "user32": {
        "GetForegroundWindow": (wt.HWND, ctypes.c_void_p),
        "MonitorFromPoint": (ctypes.c_void_p,),
        "SetClipboardData": (wt.HANDLE, ctypes.c_void_p),
        "GetClipboardData": (wt.HANDLE, ctypes.c_void_p),
        "SetWindowsHookExW": (ctypes.c_void_p,),
        "CallNextHookEx": (ctypes.c_void_p, ctypes.c_ssize_t),
        "WindowFromPoint": (wt.HWND, ctypes.c_void_p),
    },
    "kernel32": {
        "GlobalAlloc": (wt.HGLOBAL, ctypes.c_void_p),
        "GlobalLock": (ctypes.c_void_p,),
    },
}


def check_module(name: str, module) -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    for func_name in NEEDS_ARGTYPES.get(name, []):
        func = getattr(module, func_name, None)
        if func is None:
            rows.append(("%s.%s" % (name, func_name), False, "模块里找不到这个函数"))
            continue
        declared = getattr(func, "argtypes", None) is not None
        rows.append(("%s.%s 已声明 argtypes" % (name, func_name), declared,
                     "argtypes=%s" % (list(func.argtypes) if declared else None)))
    for func_name, allowed in NEEDS_POINTER_RESTYPE.get(name, {}).items():
        func = getattr(module, func_name, None)
        if func is None:
            rows.append(("%s.%s" % (name, func_name), False, "模块里找不到这个函数"))
            continue
        ok = func.restype in allowed
        rows.append(("%s.%s 的 restype 是句柄/指针类型" % (name, func_name), ok,
                     "restype=%s（期望 %s）" % (func.restype, allowed)))
    return rows


def main() -> int:
    rows: list[tuple[str, bool, str]] = []
    rows += check_module("user32", w32.user32)
    rows += check_module("kernel32", w32.kernel32)

    # 钩子过程本身：签名错了会吞键，必须单独钉一下
    rows.append(("hotkey 的 HOOKPROC 回调类型已声明",
                 hk.HOOKPROC is not None, str(hk.HOOKPROC)))
    rows.append(("hotkey 模块级已设 CallNextHookEx.argtypes",
                 w32.user32.CallNextHookEx.argtypes is not None,
                 str(w32.user32.CallNextHookEx.argtypes)))
    rows.append(("注入模块导入后 SendInput 有签名",
                 inj.user32.SendInput.argtypes is not None,
                 str(inj.user32.SendInput.argtypes)))

    # 修饰键归一化（上一轮另一个真 bug）
    rows.append(("修饰键归一化：0xA2/0xA3 → 0x11",
                 hk.canonical_vk(0xA2) == 0x11 and hk.canonical_vk(0xA3) == 0x11,
                 "%s" % [hk.canonical_vk(v) for v in (0xA2, 0xA3)]))
    rows.append(("修饰键归一化：0xA4/0xA5 → 0x12",
                 hk.canonical_vk(0xA4) == 0x12 and hk.canonical_vk(0xA5) == 0x12,
                 "%s" % [hk.canonical_vk(v) for v in (0xA4, 0xA5)]))
    rows.append(("修饰键归一化：0xA0/0xA1 → 0x10",
                 hk.canonical_vk(0xA0) == 0x10 and hk.canonical_vk(0xA1) == 0x10,
                 "%s" % [hk.canonical_vk(v) for v in (0xA0, 0xA1)]))

    print("=" * 76)
    print("审计：Win32 ctypes 签名是否声明齐全")
    print("=" * 76)
    for name, ok, detail in rows:
        if ok and "--all" not in sys.argv:
            continue
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail and not ok:
            print("        %s" % detail)
    fails = [n for n, ok, _d in rows if not ok]
    print("-" * 76)
    print("检查项 %d，通过 %d" % (len(rows), len(rows) - len(fails)))
    if fails:
        print("未声明（会有 64 位截断风险）：")
        for f in fails:
            print("  ! %s" % f)
    print("结论：%s" % ("签名齐全" if not fails else "有 %d 项待补" % len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
