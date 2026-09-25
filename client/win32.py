# -*- coding: utf-8 -*-
"""CherryVoice · Windows 原生接口封装（只用 ctypes，零第三方依赖）。

集中放置三块能力：

1. **DPI 感知** —— tkinter 默认 DPI 不感知，缩放屏上既模糊坐标又错。
2. **不抢焦点** —— ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`` + ``SW_SHOWNOACTIVATE``。
3. **前台窗口操作** —— 查询/强制前台（注入前必须确认目标窗口仍是用户那个）。

第 3 项的 ``force_foreground`` 用了 push-2-talk 记录过的三级降级
（``SetForegroundWindow`` → ``AttachThreadInput`` → 敲一下 Alt），
只作**兜底**使用。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Optional

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_ESCAPE = 0x1B
VK_LMENU = 0xA4

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.WindowFromPoint.restype = wt.HWND


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #

def enable_dpi_awareness() -> str:
    """让进程 DPI 感知；必须在创建任何窗口前调用。"""
    try:  # Windows 8.1+ 每显示器 v2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor-v2"
    except Exception:  # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001
        return "none"


# --------------------------------------------------------------------------- #
# 显示器
# --------------------------------------------------------------------------- #

def cursor_monitor_work_area() -> tuple[int, int, int, int]:
    """光标所在显示器的工作区 (left, top, right, bottom)。

    隐藏的窗口用 ``current_monitor()`` 定位会一直停在启动屏，所以取光标所在屏。
    """
    pt = wt.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return (0, 0, 1920, 1080)
    MONITOR_DEFAULTTONEAREST = 2
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return (0, 0, 1920, 1080)
    r = info.rcWork
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


# --------------------------------------------------------------------------- #
# 窗口样式：不抢焦点
# --------------------------------------------------------------------------- #

GA_ROOT = 2

user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]


def top_level_window(hwnd: int) -> int:
    """返回真正的顶层窗口。

    **踩过的坑**：tkinter 的 ``winfo_id()`` 返回的是 ``TkChild`` 子窗口，
    真正会被激活、会抢焦点的是它父级的 ``TkTopLevel``。
    把 ``WS_EX_NOACTIVATE`` 加在子窗口上**完全无效** —— 悬浮窗照样抢焦点。
    """
    if not hwnd:
        return 0
    root = int(user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) or 0)
    return root or hwnd


def window_exstyle(hwnd: int) -> int:
    return int(user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE))


def apply_noactivate(hwnd: int) -> int:
    """给**顶层窗口**加上 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW。

    返回顶层窗口最终的扩展样式。
    """
    top = top_level_window(hwnd)
    ex = window_exstyle(top)
    new = ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if new != ex:
        user32.SetWindowLongW(wt.HWND(top), GWL_EXSTYLE, ctypes.c_long(new))
    return window_exstyle(top)


def has_noactivate(hwnd: int) -> bool:
    ex = window_exstyle(top_level_window(hwnd))
    return bool(ex & WS_EX_NOACTIVATE) and bool(ex & WS_EX_TOOLWINDOW)


def show_without_activating(hwnd: int) -> None:
    """显示窗口但不激活它（作用于顶层窗口）。"""
    top = top_level_window(hwnd)
    user32.ShowWindow(wt.HWND(top), SW_SHOWNOACTIVATE)
    user32.SetWindowPos(wt.HWND(top), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# --------------------------------------------------------------------------- #
# 前台窗口
# --------------------------------------------------------------------------- #

def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int, limit: int = 200) -> str:
    n = int(user32.GetWindowTextLengthW(wt.HWND(hwnd)))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(min(n, limit) + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, len(buf))
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(hwnd), buf, 256)
    return buf.value


def force_foreground(hwnd: int) -> bool:
    """把窗口弄到前台；返回是否成功（**兜底手段**，正常路径不该需要）。

    顺序很重要：Windows 有"前台锁"——不是前台的进程直接调
    ``SetForegroundWindow`` 会被静默忽略。所以**先敲一下 Alt** 解开锁，
    再逐级降级（push-2-talk 记录过的三级方案 + SwitchToThisWindow）。
    """
    if not hwnd:
        return False
    if foreground_window() == hwnd:
        return True

    def _alt_tap() -> None:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, 2, 0)

    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)

    # 1) 先解开前台锁再设前台
    _alt_tap()
    user32.SetForegroundWindow(wt.HWND(hwnd))
    if foreground_window() == hwnd:
        return True

    # 2) AttachThreadInput 绕过前台锁
    fg = foreground_window()
    target_thread = user32.GetWindowThreadProcessId(wt.HWND(hwnd), None)
    cur_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(wt.HWND(fg), None) if fg else 0
    attached = []
    try:
        for th in (fg_thread, target_thread):
            if th and th != cur_thread:
                if user32.AttachThreadInput(cur_thread, th, True):
                    attached.append(th)
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
    finally:
        for th in attached:
            user32.AttachThreadInput(cur_thread, th, False)
    if foreground_window() == hwnd:
        return True

    # 3) SwitchToThisWindow（未公开但一直在用，能穿透前台锁）
    try:
        user32.SwitchToThisWindow(wt.HWND(hwnd), True)
    except Exception:  # noqa: BLE001
        pass
    if foreground_window() == hwnd:
        return True

    # 4) 最后再试一次 SetForegroundWindow + 置顶
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    _alt_tap()
    user32.SetForegroundWindow(wt.HWND(hwnd))
    return foreground_window() == hwnd


def find_window_by_pid(pid: int) -> Optional[int]:
    """枚举顶层可见窗口，返回属于该 pid 的第一个。"""
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and window_pid(hwnd) == pid:
            if window_title(hwnd):
                found.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# --------------------------------------------------------------------------- #
# 键盘焦点（判断有没有抢焦点的**精确**依据）
# --------------------------------------------------------------------------- #

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                ("rcCaret", wt.RECT)]


def gui_thread_info(thread_id: int) -> Optional[GUITHREADINFO]:
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return info
    return None


def foreground_focus_state() -> tuple[int, int, int]:
    """返回 (前台窗口, 前台窗口所属线程, 该线程当前持有键盘焦点的窗口)。

    这是判断"有没有抢焦点"最准的指标：即使我们没法把某个窗口设为前台
    （Windows 前台锁），也能看出悬浮窗出现后**键盘焦点有没有被挪走**。
    """
    fg = foreground_window()
    if not fg:
        return (0, 0, 0)
    tid = int(user32.GetWindowThreadProcessId(wt.HWND(fg), None))
    info = gui_thread_info(tid)
    focus = int(info.hwndFocus or 0) if info else 0
    return (fg, tid, focus)
