# -*- coding: utf-8 -*-
"""CherryVoice · 按住/松开全局热键（FR-1）。

两种后端，接口一致，可配置切换：

``polling``（**默认**）
    ``GetAsyncKeyState`` 每 10 ms 轮询 + 边沿检测。
    push-2-talk 的 ``hotkey_service.rs`` 在 Windows 上就是这条路，
    源码注释写明是为了「避免低级 hook 导致的兼容性问题」。
    缺点：**吞不掉按键**，Win 键松手可能弹出开始菜单。

``hook``
    ``WH_KEYBOARD_LL`` 低级键盘钩子。需求文档 FR-1 建议的方案。
    优点：能把 Win 键事件吞掉，``Ctrl+Win`` 不会弹开始菜单。
    缺点：需要自己搭消息泵，且更容易被安全软件拦。

两者都支持：按住触发 ``on_press``、松开触发 ``on_release``、会话中按 Esc 触发 ``on_cancel``。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable, Optional, Sequence

from . import win32 as w32

# --------------------------------------------------------------------------- #
# 键位表
# --------------------------------------------------------------------------- #

VK_MAP: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "tab": 0x09, "capslock": 0x14,
    "enter": 0x0D, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
for _i in range(1, 25):                                   # F1..F24
    VK_MAP["f%d" % _i] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":                   # A..Z
    VK_MAP[_c] = ord(_c.upper())
for _d in "0123456789":                                   # 0..9
    VK_MAP[_d] = ord(_d)

# `win` 要同时匹配左右两个 Win 键
_MULTI = {"win": (w32.VK_LWIN, w32.VK_RWIN)}

# **低级钩子报的是左右分开的修饰键**（VK_LCONTROL=0xA2、VK_LMENU=0xA4 …），
# 而不是通用的 VK_CONTROL=0x11 / VK_MENU=0x12。
# 不做归一化的话，按住 Ctrl+Win 在钩子后端里**永远不会触发** ——
# 因为 `_down` 里始终是 0xA2/0xA4，跟配置里的 0x11/0x12 对不上。
# 这是实测发现的：原始事件是 (code=0, vk=0xA2, down=True)，而事件一个都没触发。
_MODIFIER_ALIAS = {
    0xA0: 0x10, 0xA1: 0x10,      # LSHIFT / RSHIFT   → SHIFT
    0xA2: 0x11, 0xA3: 0x11,      # LCONTROL / RCONTROL → CONTROL
    0xA4: 0x12, 0xA5: 0x12,      # LMENU / RMENU     → MENU(Alt)
}


def canonical_vk(vk: int) -> int:
    """把左右专用修饰键归一化成通用 VK，便于和配置里的键位比对。"""
    return _MODIFIER_ALIAS.get(int(vk), int(vk))


def normalize_keys(keys: Sequence[str]) -> list[tuple[int, ...]]:
    """把 ``["ctrl","win"]`` 变成 ``[(0x11,), (0x5B,0x5C)]``。"""
    out: list[tuple[int, ...]] = []
    for key in keys:
        k = str(key).strip().lower()
        if k in _MULTI:
            out.append(_MULTI[k])
        elif k in VK_MAP:
            out.append((VK_MAP[k],))
        else:
            raise ValueError("不认识的键名：%r（可用：%s）"
                             % (key, ", ".join(sorted(set(VK_MAP) | set(_MULTI)))))
    return out


def _all_down(groups: Sequence[Sequence[int]]) -> bool:
    return all(any(w32.is_key_down(vk) for vk in g) for g in groups)


# --------------------------------------------------------------------------- #
# 轮询后端
# --------------------------------------------------------------------------- #

class PollingHotkey:
    backend = "polling"

    def __init__(self, keys: Sequence[str] = ("ctrl", "win"),
                 on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 on_cancel: Optional[Callable[[], None]] = None,
                 cancel_key: str = "esc", poll_ms: int = 10) -> None:
        self.groups = normalize_keys(keys)
        self.keys = list(keys)
        self.cancel_vk = VK_MAP.get(cancel_key.lower(), w32.VK_ESCAPE)
        self.on_press = on_press or (lambda: None)
        self.on_release = on_release or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.poll_ms = max(2, int(poll_ms))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active = False
        self.pressed_keys = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _fire(self, cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception:  # noqa: BLE001  热键回调不能把轮询线程搞死
            pass

    def _run(self) -> None:
        was_down = False
        esc_was = False
        interval = self.poll_ms / 1000.0
        while not self._stop.is_set():
            down = _all_down(self.groups)
            if down and not was_down:
                self.pressed_keys = True
                self._fire(self.on_press)
            elif not down and was_down and self.pressed_keys:
                self.pressed_keys = False
                self._fire(self.on_release)
            was_down = down

            esc = w32.is_key_down(self.cancel_vk)
            if esc and not esc_was and self.active:
                self._fire(self.on_cancel)
            esc_was = esc
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# 低级钩子后端
# --------------------------------------------------------------------------- #

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
LLKHF_INJECTED = 0x10


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# **这些签名必须在模块级声明**，不能等到 `_run()` 里再设：
# 只要有一次调用发生在签名设置之前，`lparam`（64 位指针）就会被当成 c_int 而溢出，
# 钩子过程抛异常 —— 而钩子过程抛异常等价于返回 0，表示"已处理"，
# Windows 会把**每一个按键都吞掉**。实测就是这样：回调进来了 6 次、每次都崩，
# 一个热键事件都没记下来。（单元测试直接调 `_callback` 时也踩到了同一个坑。）
w32.user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      wt.WPARAM, wt.LPARAM]
w32.user32.CallNextHookEx.restype = ctypes.c_void_p
# 同理，不声明 restype 的话 64 位句柄会被截成 32 位，卸载钩子会失败
w32.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         ctypes.c_void_p, wt.DWORD]
w32.user32.SetWindowsHookExW.restype = ctypes.c_void_p
w32.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
w32.user32.UnhookWindowsHookEx.restype = wt.BOOL
w32.user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                   ctypes.c_uint, ctypes.c_uint]
w32.user32.GetMessageW.restype = ctypes.c_int
w32.user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
w32.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]


class HookHotkey:
    """``WH_KEYBOARD_LL`` 后端。

    除了按住/松开，它还能在组合键成立时**吞掉 Win 键事件**，
    这样 ``Ctrl+Win`` 不会在松手时弹出开始菜单 —— 这是轮询后端做不到的。
    """

    backend = "hook"

    def __init__(self, keys: Sequence[str] = ("ctrl", "win"),
                 on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 on_cancel: Optional[Callable[[], None]] = None,
                 cancel_key: str = "esc", swallow: bool = True) -> None:
        self.groups = normalize_keys(keys)
        self.keys = list(keys)
        self.watch = {vk for g in self.groups for vk in g}
        self.cancel_vk = VK_MAP.get(cancel_key.lower(), w32.VK_ESCAPE)
        self.on_press = on_press or (lambda: None)
        self.on_release = on_release or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.swallow = bool(swallow)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hook = None
        self._proc = None
        self._thread_id = 0
        self.active = False
        self._down: set[int] = set()
        self._combo = False
        # 诊断用：记录钩子实际收到的原始事件（上限 200 条，避免无限增长）
        self.debug_events: list[tuple[int, int, bool]] = []

    # -- 安装/卸载 --------------------------------------------------------- #

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey-hook")
        self._thread.start()
        # 等钩子装好（最多 2 秒）
        for _ in range(200):
            if self._hook:
                return
            time.sleep(0.01)
        raise RuntimeError("低级键盘钩子安装失败（可能被安全软件拦截）")

    def stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            w32.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- 钩子回调 ---------------------------------------------------------- #

    def _callback(self, code, wparam, lparam):  # noqa: ANN001
        try:
            if code == 0:  # HC_ACTION
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                raw_vk = int(kb.vkCode)
                vk = canonical_vk(raw_vk)      # 0xA2 → 0x11，否则组合键永远配不上
                down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if len(self.debug_events) < 200:
                    self.debug_events.append((int(code), raw_vk, bool(down)))
                if down:
                    self._down.add(vk)
                else:
                    self._down.discard(vk)

                combo_now = all(any(v in self._down for v in g) for g in self.groups)
                if combo_now and not self._combo:
                    self._combo = True
                    self._fire(self.on_press)
                elif not combo_now and self._combo:
                    self._combo = False
                    self._fire(self.on_release)

                if (down and vk == self.cancel_vk and self.active
                        and self.cancel_vk not in self._down - {vk}):
                    self._fire(self.on_cancel)

                # 吞掉组合键里的 Win 键，防止开始菜单弹出
                if self.swallow and self._combo and vk in self.watch:
                    return 1
        except Exception:  # noqa: BLE001  钩子回调绝不能抛
            pass
        return w32.user32.CallNextHookEx(None, code, wparam, lparam)

    def _fire(self, cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        self._thread_id = w32.kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._callback)
        self._hook = w32.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            return
        msg = wt.MSG()
        while not self._stop.is_set():
            got = w32.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            w32.user32.TranslateMessage(ctypes.byref(msg))
            w32.user32.DispatchMessageW(ctypes.byref(msg))
        w32.user32.UnhookWindowsHookEx(self._hook)
        self._hook = None


# --------------------------------------------------------------------------- #

def create_hotkey(backend: str = "polling", **kwargs) -> PollingHotkey | HookHotkey:
    b = (backend or "polling").strip().lower()
    if b == "polling":
        return PollingHotkey(**kwargs)
    if b == "hook":
        return HookHotkey(**kwargs)
    raise ValueError("未知的热键后端：%r（可选 polling / hook）" % backend)
