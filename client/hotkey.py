# -*- coding: utf-8 -*-
"""EtherealFlow · 按住/松开全局热键（FR-1）。

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

本文件额外实现需求文档 FR-1 的两项：

* **FR-1.3 严格修饰键匹配** —— 触发条件是「配置里的键**全**按下
  **且**没有按下任何配置之外的修饰键」。旧实现只查「全按下」，
  所以配置 ``Ctrl+Win`` 时，用户按 ``Ctrl+Shift+Win`` **也会**误触发录音。
  判定逻辑抽成纯函数（``StrictComboMatcher``），两个后端**共用同一个状态机**，
  因此「轮询后端与钩子后端行为一致」是结构上保证的，而不是靠两边各写一遍。
* **FR-1.5 热键冲突检测** —— 用 ``RegisterHotKey`` 试探配置的组合键是否已被
  别的程序占用（``ERROR_HOTKEY_ALREADY_REGISTERED`` / 1409）。
  「注册」这一步是可注入的（``HotkeyRegistrar``），所以自动化测试能用假实现
  覆盖成功/失败两条路径，不必依赖真实系统状态。
  注册与注销**严格配对**：探测成功就立刻注销，绝不把用户的组合键据为己有。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable, Iterable, Optional, Sequence

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


# --------------------------------------------------------------------------- #
# FR-1.3 · 严格修饰键匹配（纯函数，两个后端共用）
# --------------------------------------------------------------------------- #
#
# 为什么按「类别」而不是按「VK」判断修饰键：
#   * 用户按**右** Ctrl（0xA3）也算「按下了 Ctrl」——配置写的是通用 VK 0x11；
#   * 配置里本来就有 Ctrl 时，按住 Ctrl **不算**「额外修饰键」；
#   * 左右两边的同一个修饰键**不能**被算成「额外的修饰键」。
# 所以每个 VK 会被映射成一个「匹配记号」：修饰键 → 类别名（ctrl/alt/shift/win），
# 普通键 → ``vk:<code>``。记号集合一致，后端行为就必然一致。

MODIFIER_CATEGORIES: dict[str, tuple[int, ...]] = {
    "ctrl": (0x11, 0xA2, 0xA3),      # CONTROL / LCONTROL / RCONTROL
    "alt": (0x12, 0xA4, 0xA5),       # MENU / LMENU / RMENU
    "shift": (0x10, 0xA0, 0xA1),     # SHIFT / LSHIFT / RSHIFT
    "win": (0x5B, 0x5C),             # LWIN / RWIN
}
MODIFIER_CATEGORY_NAMES = frozenset(MODIFIER_CATEGORIES)

# VK → 修饰键类别（普通键不在表里）
_VK_TO_CATEGORY: dict[int, str] = {
    vk: name for name, vks in MODIFIER_CATEGORIES.items() for vk in vks
}


def key_token(vk: int) -> str:
    """把一个 VK 映射成「匹配记号」。

    修饰键（Ctrl/Alt/Shift/Win，左右都算同一个）→ 类别名，例如 ``"ctrl"``；
    普通键 → ``"vk:<code>"``，例如 ``"vk:32"``。
    """
    vk = int(vk)
    category = _VK_TO_CATEGORY.get(vk)
    return category if category else "vk:%d" % vk


class StrictComboMatcher:
    """把「配置的键组 + 当前按下的键」变成 press / release 边沿。

    **纯逻辑**（不做任何 Win32 调用），所以测试可以直接喂集合，
    不需要真实按键（自动化环境里合成按键投递不到桌面，纯函数是唯一可靠的测法）。

    判定规则（FR-1.3）：

    1. **所有**配置的键组都要被按住（一个键组内多个候选键，任意一个即可）；
    2. 按住修饰键的类别**不能**超出配置里已有的类别
       （配置里有 Ctrl，按住 Ctrl 不算「额外」；配置里没有 Shift，
       按住 Shift 就**不算命中**）。

    「命中」= 规则 1 **且** 规则 2，成立时热键处于**活动**状态。
    press 在「从不活动 → 活动」时发出，release 在「从活动 → 不活动」时发出。

    ⚠️ **多按了修饰键会立刻取消录音**，而不是让它继续录下去：
    用户按 ``Ctrl+Shift+Win`` 时，热键在按住 ``Ctrl+Win`` 的那一瞬间
    确实命中过（此刻还没按 Shift），但 Shift 一按下就立刻判定为「不命中」
    → 发出 release → 录音中止。这样「多按修饰键」不会录进一整段垃圾。

    ⚠️ **上锁（latched）**：因为多按修饰键而取消之后，即使把多余的修饰键
    松开，热键也**不会**立刻重新触发 —— 必须先把配置的组合键完全松开。
    否则用户「Ctrl+Win 按住 → 多按一下 Shift → 松开 Shift」会被
    触发两次（一次误触发 + 一次重新触发）。

    两个后端共用**同一个**状态机（``update``），所以它们的行为一致是
    结构性的，而不是靠两份实现「碰巧一样」。
    """

    __slots__ = ("groups", "_group_tokens", "allowed_modifiers", "armed", "combo",
                 "latched")

    def __init__(self, groups: Sequence[Sequence[int]]) -> None:
        self.groups: list[tuple[int, ...]] = [
            tuple(int(v) for v in g) for g in groups
        ]
        self.allowed_modifiers: set[str] = set()
        tokens: list[frozenset[str]] = []
        for g in self.groups:
            group_tokens: set[str] = set()
            for vk in g:
                category = _VK_TO_CATEGORY.get(vk)
                if category is not None:
                    self.allowed_modifiers.add(category)
                    group_tokens.add(category)
                else:
                    group_tokens.add(key_token(vk))
            tokens.append(frozenset(group_tokens))
        self._group_tokens = tokens
        # armed = 「热键当前处于活动状态」
        self.armed = False
        # combo = 「配置里的键组当前都按住了」（不看有没有多余修饰键）
        self.combo = False
        # latched = 「因为多按了修饰键而取消过，等组合键完全松开才允许再触发」
        self.latched = False

    # -- 判据 -------------------------------------------------------------- #

    def groups_satisfied(self, tokens: Iterable[str]) -> bool:
        """配置的每个键组是否都被按住（不看有没有多余修饰键）。"""
        pressed = set(tokens)
        return all(t & pressed for t in self._group_tokens)

    def strict(self, tokens: Iterable[str]) -> bool:
        """严格命中：键组全按住 **且** 没有配置之外的修饰键。"""
        pressed = set(tokens)
        if not self.groups_satisfied(pressed):
            return False
        extra = {t for t in pressed
                 if t in MODIFIER_CATEGORY_NAMES} - self.allowed_modifiers
        return not extra

    # -- 状态机 ------------------------------------------------------------ #

    def update(self, tokens: Iterable[str]) -> tuple[bool, bool]:
        """喂入当前按下的键，返回 ``(按下边沿, 松开边沿)``。"""
        pressed = set(tokens)
        strict_now = self.strict(pressed)
        loose_now = self.groups_satisfied(pressed)
        self.combo = loose_now

        # 组合键完全松开 → 解除上锁，允许下一次触发
        if not loose_now:
            self.latched = False

        fired_press = False
        fired_release = False
        if strict_now and not self.armed and not self.latched:
            self.armed = True
            fired_press = True
        if self.armed and not strict_now:
            self.armed = False
            fired_release = True
            # 组合键还按着、只是多按了修饰键 → 上锁，免得松手后立刻又触发
            self.latched = loose_now
        return fired_press, fired_release


# --------------------------------------------------------------------------- #
# FR-1.5 · 热键冲突检测
# --------------------------------------------------------------------------- #
#
# Windows 的标准做法是 ``RegisterHotKey``：注册失败并返回
# ``ERROR_HOTKEY_ALREADY_REGISTERED``（1409）就说明这个组合键已经被别的程序占着。
#
# ⚠️ 两个必须记住的点：
#   1. **注册成功 = 独占**。所以探测必须在「注册 → 立刻注销」之间闭环，
#      绝不能把用户的组合键扣在自己手里不放（否则用户换个软件就按不了了）。
#   2. 我们的**低级钩子后端本来就不靠 ``RegisterHotKey``**（它监听全局按键），
#      所以这个检测只是「给用户提个醒」，不是后端的前置条件。
#      探测过程本身也不会影响钩子后端的行为。

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 不同后端对「被占用」这件事的解读不一样，结论里要说清楚
_BACKEND_NOTE: dict[str, str] = {
    "hook": "（注意：低级钩子后端监听全局按键，不靠 RegisterHotKey，"
            "所以热键本身仍然可用，只是别的程序也会收到这个组合键。）",
    "polling": "（轮询后端靠轮询按键状态，同样不受影响。）",
}

# 键名 → RegisterHotKey 的修饰键标志
_NAME_TO_MOD_FLAG: dict[str, int] = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "menu": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

# RegisterHotKey / UnregisterHotKey 的 ctypes 签名（漏声明会截断句柄）
w32.user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
w32.user32.RegisterHotKey.restype = wt.BOOL
w32.user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
w32.user32.UnregisterHotKey.restype = wt.BOOL


def _pretty_keys(keys: Sequence[str]) -> str:
    """把 ``["ctrl","win"]`` 变成人看的 ``"Ctrl+Win"``。"""
    parts = [str(key).strip().lower().capitalize() for key in keys]
    return "+".join(parts) if parts else "(空)"


def hotkey_register_plan(keys: Sequence[str]) -> Optional[tuple[int, int]]:
    """把配置的键翻译成 ``RegisterHotKey`` 的 ``(修饰键标志, 基础 VK)``。

    ``RegisterHotKey`` 只能表达「若干修饰键 + **一个**普通键」。
    因此：

    * 全是修饰键（``Ctrl+Win``）→ 基础 VK = 0（Windows 支持纯修饰键热键）；
    * 一个普通键（``Ctrl+Alt+Space``）→ 修饰键标志 + 那个普通键；
    * **两个以上普通键**（``Ctrl+A+B``）→ Windows 表达不了，返回 ``None``
      （调用方要如实说「检测不了」，不能假装没冲突）。
    """
    mods = 0
    base: Optional[int] = None
    for key in keys:
        k = str(key).strip().lower()
        flag = _NAME_TO_MOD_FLAG.get(k)
        if flag is not None:
            mods |= flag
            continue
        vk = VK_MAP.get(k)
        if vk is None:
            return None
        if base is not None:
            return None          # 多个普通键：RegisterHotKey 表达不了
        base = vk
    return mods, (base or 0)


class HotkeyConflict:
    """冲突检测的结果。"""

    __slots__ = ("keys", "checked", "occupied", "error_code", "message", "backend")

    def __init__(self, keys: Sequence[str], checked: bool, occupied: bool,
                 error_code: int = 0, message: str = "",
                 backend: str = "") -> None:
        self.keys = list(keys)
        self.checked = bool(checked)
        self.occupied = bool(occupied)
        self.error_code = int(error_code)
        self.message = message
        self.backend = backend

    @property
    def available(self) -> bool:
        """「这个组合键确定能用」：检测过、没被占用、**且没有出错**。

        注意区分「确认没被占用」（``occupied is False``）和「确定能用」
        （本属性）：``RegisterHotKey`` 返回了别的错误码时，我们**不知道**
        到底有没有被占用 —— 这种情况下不能报「可用」，否则是骗用户。
        """
        return self.checked and not self.occupied and self.error_code == 0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return ("HotkeyConflict(keys=%r, checked=%s, occupied=%s, error_code=%s)"
                % (self.keys, self.checked, self.occupied, self.error_code))


class HotkeyRegistrar:
    """注册/注销一对热键的抽象 —— 测试用假实现替换它。

    之所以抽一层：真实的 ``RegisterHotKey`` 在自动化环境里结果不确定
    （取决于用户机器上谁注册了什么），没法用来验证「成功」和「失败」
    两条路径。测试注入假实现，就能把两条路径都钉死。
    """

    def register(self, mods: int, vk: int) -> int:
        """尝试注册，返回 Win32 错误码；0 表示成功。"""
        raise NotImplementedError

    def unregister(self, mods: int, vk: int) -> None:
        """注销。必须与 ``register`` **配对**调用。"""
        raise NotImplementedError


class Win32HotkeyRegistrar(HotkeyRegistrar):
    """真的去调 ``RegisterHotKey`` / ``UnregisterHotKey``。

    ⚠️ ``RegisterHotKey(hWnd=NULL)`` 绑定的是**调用线程**，
    所以注册和注销必须在**同一个线程**里成对完成 ——
    ``check_hotkey_conflict`` 就在一个函数里做完这两步。
    """

    def __init__(self, hotkey_id: int = 0xE710) -> None:
        self.hotkey_id = int(hotkey_id)

    def register(self, mods: int, vk: int) -> int:
        if w32.user32.RegisterHotKey(None, self.hotkey_id, mods, vk):
            return 0
        return int(ctypes.get_last_error())

    def unregister(self, mods: int, vk: int) -> None:
        w32.user32.UnregisterHotKey(None, self.hotkey_id)


def check_hotkey_conflict(keys: Sequence[str], *,
                           registrar: Optional[HotkeyRegistrar] = None,
                           backend: str = "") -> HotkeyConflict:
    """检测配置的组合键是否已被别的程序占用（FR-1.5）。

    返回的 ``HotkeyConflict`` 会明确回答三件事：

    * ``checked`` —— 到底检测了没有（Windows 表达不了的组合 = 没检测）；
    * ``occupied`` —— 是不是**已经被占用**；
    * ``message``  —— 人能看懂的一句话结论（哪个键、是不是被占用）。

    ⚠️ **这个检测能查到什么、查不到什么**（别把它当万能的）：

    * 查得到：别的程序用 ``RegisterHotKey`` 注册了**同一个组合键**。
    * 查不到：别的程序用**低级键盘钩子**（``WH_KEYBOARD_LL``）之类的手段
      抢了这个组合键 —— 那类程序不走 ``RegisterHotKey`` 这张表，
      ``RegisterHotKey`` 看不见它们。**我们自己的钩子后端就是这一类。**
    * 查不到：Windows 自己保留的组合键（比如某些系统快捷键）——
      它们同样表现为 1409，但结论里会写「已被别的程序**或系统**占用」。
    """
    key_list = [str(k).strip().lower() for k in keys]
    label = _pretty_keys(key_list)

    plan = hotkey_register_plan(key_list)
    if plan is None:
        return HotkeyConflict(
            key_list, checked=False, occupied=False, backend=backend,
            message=("组合键 %s 里含有多个普通按键，Windows 的 RegisterHotKey "
                     "表达不了这种组合，无法检测是否被占用。" % label))
    mods, vk = plan

    reg = registrar or Win32HotkeyRegistrar()
    error = int(reg.register(mods, vk))
    if error == 0:
        # 注册成功 = 我们短暂占住了它 → **立刻注销**，绝不能扣着用户的键不放
        try:
            reg.unregister(mods, vk)
        except Exception:  # noqa: BLE001
            pass
        return HotkeyConflict(
            key_list, checked=True, occupied=False, backend=backend,
            message="组合键 %s 当前没有被别的程序占用。" % label)

    if error == ERROR_HOTKEY_ALREADY_REGISTERED:
        return HotkeyConflict(
            key_list, checked=True, occupied=True, error_code=error,
            backend=backend,
            message=("组合键 %s 已被别的程序（或系统）占用（Windows 错误 %d："
                     "ERROR_HOTKEY_ALREADY_REGISTERED），请换个组合键。%s"
                     % (label, error, _BACKEND_NOTE.get(backend, ""))))

    return HotkeyConflict(
        key_list, checked=True, occupied=False, error_code=error, backend=backend,
        message=("无法判断组合键 %s 是否被占用：RegisterHotKey 返回错误 %d。%s"
                 % (label, error, _BACKEND_NOTE.get(backend, ""))))


# --------------------------------------------------------------------------- #
# 轮询后端
# --------------------------------------------------------------------------- #

class PollingHotkey:
    backend = "polling"

    def __init__(self, keys: Sequence[str] = ("ctrl", "win"),
                 on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 on_cancel: Optional[Callable[[], None]] = None,
                 cancel_key: str = "esc", poll_ms: int = 10,
                 check_conflict: bool = False,
                 on_conflict: Optional[Callable[["HotkeyConflict"], None]] = None,
                 registrar: Optional[HotkeyRegistrar] = None
                 ) -> None:
        self.groups = normalize_keys(keys)
        self.keys = list(keys)
        self.matcher = StrictComboMatcher(self.groups)
        self.cancel_vk = VK_MAP.get(cancel_key.lower(), w32.VK_ESCAPE)
        self.on_press = on_press or (lambda: None)
        self.on_release = on_release or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.poll_ms = max(2, int(poll_ms))
        self.check_conflict = bool(check_conflict)
        self.on_conflict = on_conflict
        self.registrar = registrar
        self.conflict: Optional[HotkeyConflict] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.active = False
        self.pressed_keys = False

    # -- 冲突检测（FR-1.5，可选） ------------------------------------------ #

    def detect_conflict(self, registrar: Optional[HotkeyRegistrar] = None
                        ) -> HotkeyConflict:
        """探测配置的组合键是否已被别的程序占用，结果存进 ``self.conflict``。

        ``registrar`` 可注入（测试用假实现）；不传就用构造时给的
        ``self.registrar``，再没有才走真实的 ``RegisterHotKey``。
        """
        self.conflict = check_hotkey_conflict(
            self.keys, registrar=registrar or self.registrar,
            backend=self.backend)
        return self.conflict

    # -- 运行 -------------------------------------------------------------- #

    def start(self) -> None:
        if self.check_conflict:
            self.detect_conflict()
            if self.on_conflict is not None:
                try:
                    self.on_conflict(self.conflict)
                except Exception:  # noqa: BLE001
                    pass
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

    def _pressed_tokens(self) -> set[str]:
        """当前按下的键的记号集合（FR-1.3）。

        * 修饰键按**类别**统计（Ctrl/Alt/Shift/Win，左右都算同一个）；
        * 配置里的普通键（空格、F 键、字母…）按精确 VK 统计。

        修饰键要查**通用 VK + 左右分开的 VK**：``GetAsyncKeyState(VK_CONTROL)``
        与 ``GetAsyncKeyState(VK_LCONTROL)`` 的覆盖范围在不同 Windows 版本上
        并不一致，两种都查才稳（查到就短路，省下后面的调用）。
        """
        tokens: set[str] = set()
        for category, vks in MODIFIER_CATEGORIES.items():
            for vk in vks:
                if w32.is_key_down(vk):
                    tokens.add(category)
                    break
        # 配置里的普通键（空格、F 键、字母…）也要看
        for g in self.groups:
            for vk in g:
                if vk in _VK_TO_CATEGORY:
                    continue
                if w32.is_key_down(vk):
                    tokens.add(key_token(vk))
        return tokens

    def _run(self) -> None:
        esc_was = False
        interval = self.poll_ms / 1000.0
        while not self._stop.is_set():
            fired_press, fired_release = self.matcher.update(self._pressed_tokens())
            if fired_press:
                self.pressed_keys = True
                # ⚠️ ``active`` = 「当前正在录音」。
                # 它决定 Esc 会不会触发取消 —— 必须由热键的按下/松开驱动。
                # 之前这个字段**从来没被设成 True**（只有 tools/test_hotkey.py
                # 手工设过），于是 Esc 取消在真实使用中**永不触发**：
                # 需求文档 FR-1 写的「录音中按 Esc 取消本次输入」根本不存在。
                self.active = True
                self._fire(self.on_press)
            elif fired_release:
                self.pressed_keys = False
                self.active = False
                self._fire(self.on_release)

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
                 cancel_key: str = "esc", swallow: bool = True,
                 check_conflict: bool = False,
                 on_conflict: Optional[Callable[["HotkeyConflict"], None]] = None,
                 registrar: Optional[HotkeyRegistrar] = None
                 ) -> None:
        self.groups = normalize_keys(keys)
        self.keys = list(keys)
        self.watch = {vk for g in self.groups for vk in g}
        self.matcher = StrictComboMatcher(self.groups)
        self.cancel_vk = VK_MAP.get(cancel_key.lower(), w32.VK_ESCAPE)
        self.on_press = on_press or (lambda: None)
        self.on_release = on_release or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.swallow = bool(swallow)
        self.check_conflict = bool(check_conflict)
        self.on_conflict = on_conflict
        self.registrar = registrar
        self.conflict: Optional[HotkeyConflict] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hook = None
        self._proc = None
        self._thread_id = 0
        self.active = False
        self._down: set[int] = set()
        # 诊断用：记录钩子实际收到的原始事件（上限 200 条，避免无限增长）
        self.debug_events: list[tuple[int, int, bool]] = []

    # -- 冲突检测（FR-1.5，可选） ------------------------------------------ #

    def detect_conflict(self, registrar: Optional[HotkeyRegistrar] = None
                        ) -> HotkeyConflict:
        """探测配置的组合键是否已被别的程序占用，结果存进 ``self.conflict``。

        ⚠️ **钩子后端本来就不靠 ``RegisterHotKey``**（它监听全局按键），
        所以这里的结论只是「给用户提个醒」，不影响钩子能不能用。
        """
        self.conflict = check_hotkey_conflict(
            self.keys, registrar=registrar or self.registrar,
            backend=self.backend)
        return self.conflict

    # -- 安装/卸载 --------------------------------------------------------- #

    def start(self) -> None:
        if self.check_conflict:
            self.detect_conflict()
            if self.on_conflict is not None:
                try:
                    self.on_conflict(self.conflict)
                except Exception:  # noqa: BLE001
                    pass
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

    def _pressed_tokens(self) -> set[str]:
        """钩子目前知道的「按下的键」的记号集合（FR-1.3）。

        ``self._down`` 里存的是**原始** VK（0xA2 / 0xA3 是左右分开的），
        但 ``key_token`` 会把左右键都归到同一个类别（``"ctrl"``），
        跟轮询后端按类别统计的口径一致 —— 两条路径因此给出同样的判定。

        保留原始 VK（而不是归一化后��）是为了不丢信息：左右手同时按住 Ctrl 时，
        松开其中一个不应该让「Ctrl 仍按着」这个事实消失。
        """
        return {key_token(vk) for vk in self._down}

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
                    self._down.add(raw_vk)
                else:
                    self._down.discard(raw_vk)

                fired_press, fired_release = self.matcher.update(self._pressed_tokens())
                if fired_press:
                    # ⚠️ ``active`` = 「当前正在录音」，由组合键的按下/松开驱动。
                    # 不设它的话 Esc 取消在真实使用中永不触发（详见 PollingHotkey._run）。
                    self.active = True
                    self._fire(self.on_press)
                elif fired_release:
                    self.active = False
                    self._fire(self.on_release)

                if (down and vk == self.cancel_vk and self.active
                        and self.cancel_vk not in self._down - {raw_vk}):
                    self._fire(self.on_cancel)

                # 吞掉组合键里的 Win 键，防止开始菜单弹出
                # （用「键组是否都按着」而不是严格匹配 —— 组合键成立就该吞，
                #   否则 Ctrl+Shift+Win 这种多按了修饰键的情况会漏掉 Win 键，
                #   松手时开始菜单就弹出来了）
                if self.swallow and self.matcher.combo and vk in self.watch:
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

def create_hotkey(backend: str = "polling", *,
                   check_conflict: bool = False,
                   on_conflict: Optional[Callable[[HotkeyConflict], None]] = None,
                   registrar: Optional[HotkeyRegistrar] = None,
                   **kwargs) -> PollingHotkey | HookHotkey:
    """创建热键后端。

    ``check_conflict`` / ``on_conflict`` 是 FR-1.5 的**可选**开关：
    默认 ``False`` —— 现有调用方（``client/app.py``）的行为一个字都不变。
    打开后 ``start()`` 时会做一次冲突探测，结果放在 ``hotkey.conflict``，
    并通过 ``on_conflict`` 回调交给设置界面去提示用户。

    ``registrar`` 是给测试用的注入点（换成假的注册实现）。
    """
    kwargs["check_conflict"] = bool(check_conflict)
    kwargs["on_conflict"] = on_conflict
    kwargs["registrar"] = registrar
    b = (backend or "polling").strip().lower()
    if b == "polling":
        return PollingHotkey(**kwargs)
    if b == "hook":
        return HookHotkey(**kwargs)
    raise ValueError("未知的热键后端：%r（可选 polling / hook）" % backend)
