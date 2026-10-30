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

另外新增两块**不需要真实按键**的确定性验证（FR-1.3 / FR-1.5）：

D. **严格修饰键匹配**（纯函数）—— 抽成 ``StrictComboMatcher`` 之后，
   判定逻辑可以脱离 Win32 单测。同时验证**轮询后端与钩子后端行为一致**。
E. **热键冲突检测**（FR-1.5）—— 「注册」这一步可注入，所以成功/失败
   两条路径都能覆盖，不必依赖真实系统状态。

退出码 0 = A/D/E 全过（B 允许 SKIP）。
"""

from __future__ import annotations

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

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
VK_LSHIFT, VK_SPACE = 0xA0, 0x20

# 修饰键类别（FR-1.3 的判定单位）
T_CTRL, T_ALT, T_SHIFT, T_WIN = "ctrl", "alt", "shift", "win"


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


# --------------------------------------------------------------------------- #
# D. FR-1.3 · 严格修饰键匹配（纯函数，可离线断言）
# --------------------------------------------------------------------------- #

# 判定用例：(用例名, 键组, 按下的记号集合, 期望是否严格命中)
STRICT_CASES: list[tuple[str, list[str], set[str], bool]] = [
    ("配置键全按住 → 命中", ["ctrl", "win"], {T_CTRL, T_WIN}, True),
    ("少按一个 → 不命中", ["ctrl", "win"], {T_CTRL}, False),
    ("多按 Shift → 不命中（FR-1.3 的核心）", ["ctrl", "win"],
     {T_CTRL, T_WIN, T_SHIFT}, False),
    ("多按 Alt → 不命中", ["ctrl", "win"], {T_CTRL, T_WIN, T_ALT}, False),
    ("多按 Ctrl（配置里已有）→ 仍命中", ["ctrl", "win"], {T_CTRL, T_WIN}, True),
    ("配置含 Shift 时，多按 Shift → 命中", ["ctrl", "shift", "win"],
     {T_CTRL, T_SHIFT, T_WIN}, True),
    ("配置含 Shift 时，多按 Alt → 不命中", ["ctrl", "shift", "win"],
     {T_CTRL, T_SHIFT, T_WIN, T_ALT}, False),
    ("只按 Shift → 不命中", ["ctrl", "win"], {T_SHIFT}, False),
    ("配置 Ctrl+Alt 时只按 Ctrl → 不命中", ["ctrl", "alt"], {T_CTRL}, False),
    ("配置 Ctrl+Alt 时两个都按 → 命中", ["ctrl", "alt"], {T_CTRL, T_ALT}, True),
    ("配置 Ctrl+Alt 时多按 Shift → 不命中", ["ctrl", "alt"],
     {T_CTRL, T_ALT, T_SHIFT}, False),
    ("配置单个 Win → 按 Win 命中", ["win"], {T_WIN}, True),
    ("配置单个 Win 时多按 Ctrl → 不命中", ["win"], {T_WIN, T_CTRL}, False),
    ("普通键也算键组：Ctrl+Space 全按住 → 命中", ["ctrl", "space"],
     {T_CTRL, "vk:%d" % VK_SPACE}, True),
    ("普通键没按 → 不命中", ["ctrl", "space"], {T_CTRL}, False),
]


def _run_strict_cases(matcher) -> list[str]:
    """跑一遍严格匹配的用例表，返回**失败的用例名**列表。"""
    failures: list[str] = []
    for name, keys, pressed, expected in STRICT_CASES:
        groups = normalize_keys(keys)
        m = matcher(groups)
        got = m.strict(pressed)
        if got != expected:
            failures.append("%s：期望 %s，实际 %s" % (name, expected, got))
    return failures


def test_strict_matching() -> tuple[bool, str]:
    """FR-1.3：严格修饰键匹配的纯函数判定。"""
    failures = _run_strict_cases(hk.StrictComboMatcher)
    ok = not failures
    detail = ("%d 条用例全部符合预期；%s"
              % (len(STRICT_CASES),
                 "无失败" if ok else "失败：" + "；".join(failures)))
    return ok, detail


def test_strict_matching_positive_control() -> tuple[bool, str]:
    """**阳性对照**：故意把判定改坏（只查「键组全按住」，不看额外修饰键），
    断言用例**必须失败**。

    没有这一条，就无法证明上面的断言真的有鉴别力 —— 也许它对任何输入
    都返回 True，测试照样「通过」。
    """
    class BrokenMatcher(hk.StrictComboMatcher):
        """故意漏掉「无额外修饰键」这一半判据。"""

        def strict(self, tokens):  # noqa: ANN001, ANN201
            return self.groups_satisfied(tokens)

    failures = _run_strict_cases(BrokenMatcher)
    ok = bool(failures)      # 阳性对照：坏实现**必须**被抓出来
    return ok, ("坏实现（忽略额外修饰键）被判定失败的用例数=%d（期望 >0）：%s"
                % (len(failures), "；".join(failures[:4]) or "（无）"))


def _polling_events(keys: list[str], states: list[set[int]]) -> list[str]:
    """用假的按键状态源驱动轮询后端，返回事件序列。

    ``states`` 是一串「当前按下的通用 VK 集合」，相邻两步之间留出轮询时间，
    所以轮询后端看到的**状态序列**就是它。
    """
    real_is_down = w32.is_key_down
    state: set[int] = set()
    w32.is_key_down = lambda vk: vk in state         # type: ignore[assignment]
    events: list[str] = []
    try:
        hot = hk.PollingHotkey(keys=keys, poll_ms=5,
                               on_press=lambda: events.append("press"),
                               on_release=lambda: events.append("release"))
        hot.start()
        time.sleep(0.10)
        for s in states:
            state.clear()
            state.update(s)
            time.sleep(0.09)
        hot.stop()
    finally:
        w32.is_key_down = real_is_down                # type: ignore[assignment]
    return events


def _hook_run(keys: list[str], states: list[set[int]]
              ) -> tuple[list[str], list[set[int]]]:
    """用构造的 ``KBDLLHOOKSTRUCT`` 驱动钩子后端。

    返回 ``(事件序列, 钩子实际观察到的状态序列)``。

    ⚠️ 钩子后端是**事件驱动**的：它看到的是每个按键事件之间的中间状态，
    而轮询后端是**采样**的。所以「两个后端行为一致」要检验的是：
    **给它们同一条状态序列时，它们给出同样的事件序列** ——
    这正是两个后端共用同一个判定状态机（``StrictComboMatcher``）的意义。
    """
    from client.hotkey import (KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP,  # noqa: E402
                               HookHotkey)

    events: list[str] = []
    hot = HookHotkey(keys=keys, swallow=False,
                     on_press=lambda: events.append("press"),
                     on_release=lambda: events.append("release"))
    down: set[int] = set()
    observed: list[set[int]] = []

    def feed(vk: int, is_down: bool) -> None:
        kb = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=None)
        hot._callback(0, WM_KEYDOWN if is_down else WM_KEYUP,  # noqa: SLF001
                      ctypes.addressof(kb))

    for s in states:
        for vk in sorted(down - s):
            feed(vk, False)
        for vk in sorted(s - down):
            feed(vk, True)
        down.clear()
        down.update(s)
        observed.append(set(s))
    for vk in sorted(down):
        feed(vk, False)
    return events, observed


def test_backends_consistent() -> tuple[bool, str]:
    """轮询后端与钩子后端**行为必须一致**（FR-1.3）。

    做法：先用钩子后端跑一遍，记录它**实际观察到的状态序列**，
    再把**同一条状态序列**喂给轮询后端 —— 两者的事件序列必须完全相同。
    这比「各跑各的」严格得多：它直接检验了「两个后端共用同一个判定」。
    """
    hook_states = [
        {VK_LCONTROL},                       # 只按 Ctrl
        {VK_LCONTROL, VK_LWIN},              # Ctrl+Win → press
        {VK_LCONTROL, VK_LWIN, VK_LSHIFT},   # 多按左 Shift → release（录音中止）
        {VK_LCONTROL, VK_LWIN},              # Shift 松开 → 不得重新触发（上锁）
        set(),                               # 全松开 → 解除上锁
        {VK_LCONTROL, VK_LWIN},              # 再按一次 → 应该能再次触发
        set(),
    ]
    # 轮询后端用通用 VK 表示同样的状态
    pol_states = [
        {VK_CONTROL},
        {VK_CONTROL, VK_LWIN},
        {VK_CONTROL, VK_LWIN, 0xA0},
        {VK_CONTROL, VK_LWIN},
        set(),
        {VK_CONTROL, VK_LWIN},
        set(),
    ]
    hk_ev, observed = _hook_run(["ctrl", "win"], hook_states)
    pol = _polling_events(["ctrl", "win"], pol_states)

    ok = (pol == hk_ev and hk_ev == ["press", "release", "press", "release"])
    return ok, ("钩子后端=%s（观察到的状态序列 %d 步）；轮询后端=%s；两者必须相同"
                % (hk_ev or "（无）", len(observed), pol or "（无）"))


def test_strict_no_false_trigger() -> tuple[bool, str]:
    """FR-1.3 的核心：**多按一个修饰键就不算命中**。

    场景：配置 ``Ctrl+Win``，用户**同时**按住 ``Ctrl+Shift+Win``。
    两个后端都**不得**触发录音 —— 这正是旧实现（只查「配置里的键全按住」）
    会误触发的情况。

    ⚠️ 关于「先按 Ctrl+Win、之后才补按 Shift」：那一刻组合键确实成立过，
    严格匹配只能保证「**按住 Shift 的那一下就立刻中止录音**」，
    不可能凭空知道用户下一秒要按 Shift（详见 StrictComboMatcher 的注释）。
    """
    # 额外的修饰键**从头就按住** → 组合键从未严格成立 → 不得有任何事件
    pol = _polling_events(["ctrl", "win"], [
        {VK_CONTROL, VK_LWIN, 0xA0},        # Ctrl+Shift+Win
        set(),
    ])
    hk_ev, _ = _hook_run(["ctrl", "win"], [
        {VK_LCONTROL, VK_LWIN, VK_LSHIFT},  # Ctrl+Shift+Win
        set(),
    ])
    ok = (pol == [] and hk_ev == [])
    return ok, ("轮询后端在 Ctrl+Shift+Win 下的事件=%s；钩子后端=%s（都应为空）"
                % (pol or "（无）", hk_ev or "（无）"))


def test_strict_extra_modifier_aborts() -> tuple[bool, str]:
    """组合键成立之后**补按**一个额外修饰键 → 必须立刻中止录音。

    否则用户按住 Ctrl+Shift+Win 说话，会把 Shift 也按着的那整段都录进去。
    同时验证「补按的修饰键松开后不得重新触发」（上锁），
    必须把组合键完全松开才允许下一次触发。
    """
    hk_ev, _ = _hook_run(["ctrl", "win"], [
        {VK_LCONTROL, VK_LWIN},              # Ctrl+Win → press
        {VK_LCONTROL, VK_LWIN, VK_LSHIFT},   # 补按 Shift → release（中止）
        {VK_LCONTROL, VK_LWIN},              # 松开 Shift → 不得再次触发
        {VK_LCONTROL, VK_LWIN, VK_LSHIFT},   # 再补按一次 → 也不该触发
        set(),                               # 全松开 → 解除上锁
        {VK_LCONTROL, VK_LWIN},              # 再按一次 → 应该能触发
        set(),
    ])
    ok = hk_ev == ["press", "release", "press", "release"]
    return ok, ("钩子后端事件=%s（期望 press→release→press→release："
                "补按修饰键必须中止录音，且松开后不得立刻重触发）"
                % (hk_ev or "（无）"))


def test_end_to_end_positive_control() -> tuple[bool, str]:
    """**阳性对照（端到端）**：把判定改坏后，D4 那条端到端用例**必须失败**。

    D2 只证明了纯函数 ``strict()`` 有鉴别力；这一条证明**端到端**的用例
    （真的驱动两个后端跑一遍）也有鉴别力 —— 否则「端到端用例恒过」
    的测试等于没测。

    做法：临时把 ``hk.StrictComboMatcher`` 换成一个忽略额外修饰键的坏实现，
    跑一遍 D4 的场景，断言此时**出现了本不该出现的 press 事件**。
    """
    class BrokenMatcher(hk.StrictComboMatcher):
        """旧实现：只看「配置里的键组全按着」，不看额外修饰键。"""

        def strict(self, tokens):  # noqa: ANN001, ANN201
            return self.groups_satisfied(tokens)

    original = hk.StrictComboMatcher
    hk.StrictComboMatcher = BrokenMatcher       # type: ignore[misc]
    try:
        pol = _polling_events(["ctrl", "win"], [
            {VK_CONTROL, VK_LWIN, 0xA0},        # Ctrl+Shift+Win
            set(),
        ])
        hk_ev, _ = _hook_run(["ctrl", "win"], [
            {VK_LCONTROL, VK_LWIN, VK_LSHIFT},  # Ctrl+Shift+Win
            set(),
        ])
    finally:
        hk.StrictComboMatcher = original         # type: ignore[misc]

    ok = bool(pol) and bool(hk_ev)   # 阳性对照：坏实现必须产生本不该有的 press
    return ok, ("坏实现下 Ctrl+Shift+Win 的事件：轮询=%s，钩子=%s"
                "（期望都非空 —— 说明 D4 的断言确实能抓到回退）"
                % (pol or "（无）", hk_ev or "（无）"))


# --------------------------------------------------------------------------- #
# E. FR-1.5 · 热键冲突检测
# --------------------------------------------------------------------------- #

class FakeRegistrar(hk.HotkeyRegistrar):
    """假的「注册」实现：返回指定的 Win32 错误码，并记录调用序列。"""

    def __init__(self, error: int) -> None:
        self.error = int(error)
        self.calls: list[tuple[str, int, int]] = []

    def register(self, mods: int, vk: int) -> int:
        self.calls.append(("register", mods, vk))
        return self.error

    def unregister(self, mods: int, vk: int) -> None:
        self.calls.append(("unregister", mods, vk))


def test_conflict_registered_ok() -> tuple[bool, str]:
    """注册成功 → 判定为「没被占用」，并且**注册/注销严格配对**。"""
    reg = FakeRegistrar(0)
    res = hk.check_hotkey_conflict(["ctrl", "alt", "space"], registrar=reg)
    kinds = [c[0] for c in reg.calls]
    paired = kinds == ["register", "unregister"]
    ok = (res.checked and not res.occupied and res.available and paired
          and "没有被别的程序占用" in res.message)
    return ok, ("checked=%s occupied=%s 调用序列=%s；结论=%s"
                % (res.checked, res.occupied, kinds, res.message))


def test_conflict_already_registered() -> tuple[bool, str]:
    """注册失败（1409 = ERROR_HOTKEY_ALREADY_REGISTERED）→ 判定为「被占用」。"""
    reg = FakeRegistrar(hk.ERROR_HOTKEY_ALREADY_REGISTERED)
    res = hk.check_hotkey_conflict(["ctrl", "win"], registrar=reg)
    kinds = [c[0] for c in reg.calls]
    # 注册失败 → 没有拿到注册权 → 不该去注销
    ok = (res.checked and res.occupied and not res.available
          and res.error_code == hk.ERROR_HOTKEY_ALREADY_REGISTERED
          and kinds == ["register"]
          and "已被别的程序" in res.message
          and "占用" in res.message
          and "Ctrl+Win" in res.message)
    return ok, ("checked=%s occupied=%s error=%s 调用序列=%s；结论=%s"
                % (res.checked, res.occupied, res.error_code, kinds, res.message))


def test_conflict_other_error() -> tuple[bool, str]:
    """其它 Win32 错误 → 「检测了，但结论未知」，不能谎报「没被占用」。"""
    reg = FakeRegistrar(5)                        # ERROR_ACCESS_DENIED
    res = hk.check_hotkey_conflict(["ctrl", "alt"], registrar=reg)
    ok = (res.checked and not res.occupied and not res.available
          and res.error_code == 5 and "无法判断" in res.message)
    return ok, ("checked=%s occupied=%s error=%s；结论=%s"
                % (res.checked, res.occupied, res.error_code, res.message))


def test_conflict_undetectable_combo() -> tuple[bool, str]:
    """RegisterHotKey 表达不了的组合（两个以上普通键）→ 如实说「检测不了」。"""
    reg = FakeRegistrar(0)
    res = hk.check_hotkey_conflict(["ctrl", "a", "b"], registrar=reg)
    ok = (not res.checked and not res.occupied and not res.available
          and reg.calls == [] and "无法检测" in res.message)
    return ok, ("checked=%s 调用序列=%s；结论=%s"
                % (res.checked, reg.calls, res.message))


def test_conflict_positive_control() -> tuple[bool, str]:
    """**阳性对照**：把「注册成功」的假实现换成「已被占用」，
    断言上一条的结论**必须翻转**。

    没有这一条，就无法证明冲突检测真的在区分两条路径（而不是无论
    返回什么都报同一句话）。
    """
    ok_res = hk.check_hotkey_conflict(["ctrl", "win"], registrar=FakeRegistrar(0))
    busy_res = hk.check_hotkey_conflict(
        ["ctrl", "win"], registrar=FakeRegistrar(hk.ERROR_HOTKEY_ALREADY_REGISTERED))
    flipped = (not ok_res.occupied) and busy_res.occupied
    ok = flipped and (ok_res.message != busy_res.message)
    return ok, ("注册成功→occupied=%s；已被占用→occupied=%s（必须相反且文案不同）"
                % (ok_res.occupied, busy_res.occupied))


def test_conflict_wired_into_backends() -> tuple[bool, str]:
    """冲突检测能接到两个后端上（``check_conflict=True`` → ``start()`` 时探测）。"""
    results = {}
    for backend in ("polling", "hook"):
        reg = FakeRegistrar(hk.ERROR_HOTKEY_ALREADY_REGISTERED)
        seen: list[hk.HotkeyConflict] = []
        hot = create_hotkey(backend, keys=["ctrl", "win"],
                            check_conflict=True,
                            on_conflict=seen.append,
                            registrar=reg)
        hot.start()
        time.sleep(0.15)
        hot.stop()
        results[backend] = (hot.conflict, seen, reg.calls)
    ok = True
    detail = []
    for backend, (conflict, seen, calls) in results.items():
        good = (conflict is not None and conflict.occupied
                and len(seen) == 1 and seen[0] is conflict
                and [c[0] for c in calls] == ["register"])
        ok = ok and good
        detail.append("%s：occupied=%s 回调次数=%d 调用=%s"
                      % (backend, conflict.occupied if conflict else None,
                         len(seen), [c[0] for c in calls]))
    return ok, "；".join(detail)


def test_conflict_default_off() -> tuple[bool, str]:
    """默认**不做**冲突检测 —— 现有调用方式（``client/app.py``）行为不变。"""
    hot = create_hotkey("polling", keys=["ctrl", "win"])
    ok = (hot.check_conflict is False and hot.conflict is None)
    return ok, ("check_conflict=%s conflict=%s（默认应分别是 False / None）"
                % (hot.check_conflict, hot.conflict))


def test_conflict_real_registerhotkey() -> tuple[bool, str]:
    """**真实** ``RegisterHotKey`` 路径：先占住一个组合键，再检测。

    这是端到端验证「注册失败 = 被别人占了」这个前提确实成立：
    同一个线程里先注册成功，再用另一个 id 注册同一个组合键 → 必然 1409。
    """
    keys = ["ctrl", "alt", "f9"]
    plan = hk.hotkey_register_plan(keys)
    if plan is None:
        return True, "[SKIP] 该组合无法用 RegisterHotKey 表达"
    mods, vk = plan

    holder = hk.Win32HotkeyRegistrar(hotkey_id=0xE711)
    err = holder.register(mods, vk)
    if err != 0:
        return True, ("[SKIP] 组合键 %s 在本机已被占用（错误 %d），无法构造对照"
                      % ("+".join(keys), err))
    try:
        res = hk.check_hotkey_conflict(keys)
        ok = (res.checked and res.occupied
              and res.error_code == hk.ERROR_HOTKEY_ALREADY_REGISTERED
              and "已被别的程序" in res.message and "占用" in res.message)
        detail = ("先注册 Ctrl+Alt+F9 成功，再检测 → occupied=%s error=%s；结论=%s"
                  % (res.occupied, res.error_code, res.message))
    finally:
        holder.unregister(mods, vk)

    # 释放之后，同一个组合键应当重新变成「可用」
    after = hk.check_hotkey_conflict(keys)
    ok = ok and (not after.occupied)
    detail += "；释放后再检测 → occupied=%s" % after.occupied
    return ok, detail


# --------------------------------------------------------------------------- #

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

    ok_d, detail_d = test_strict_matching()
    rows.append(("D1. FR-1.3 严格修饰键匹配（纯函数）", ok_d, detail_d))

    ok_d2, detail_d2 = test_strict_matching_positive_control()
    rows.append(("D2. 阳性对照：坏实现必须被判失败", ok_d2, detail_d2))

    ok_d3, detail_d3 = test_backends_consistent()
    rows.append(("D3. 轮询/钩子两个后端行为一致", ok_d3, detail_d3))

    ok_d4, detail_d4 = test_strict_no_false_trigger()
    rows.append(("D4. Ctrl+Shift+Win 不得误触发（FR-1.3）", ok_d4, detail_d4))

    ok_d5, detail_d5 = test_strict_extra_modifier_aborts()
    rows.append(("D5. 补按额外修饰键 → 立刻中止录音且不重触发", ok_d5, detail_d5))

    ok_d6, detail_d6 = test_end_to_end_positive_control()
    rows.append(("D6. 阳性对照（端到端）：坏实现必须被 D4 抓到", ok_d6, detail_d6))

    ok_e1, detail_e1 = test_conflict_registered_ok()
    rows.append(("E1. FR-1.5 冲突检测：注册成功 → 未被占用（且注册/注销配对）",
                 ok_e1, detail_e1))

    ok_e2, detail_e2 = test_conflict_already_registered()
    rows.append(("E2. FR-1.5 冲突检测：1409 → 已被占用", ok_e2, detail_e2))

    ok_e3, detail_e3 = test_conflict_other_error()
    rows.append(("E3. FR-1.5 冲突检测：其它错误 → 结论未知", ok_e3, detail_e3))

    ok_e4, detail_e4 = test_conflict_undetectable_combo()
    rows.append(("E4. FR-1.5 冲突检测：无法表达的组合 → 如实报告", ok_e4, detail_e4))

    ok_e5, detail_e5 = test_conflict_positive_control()
    rows.append(("E5. 阳性对照：占用结论必须随注册结果翻转", ok_e5, detail_e5))

    ok_e6, detail_e6 = test_conflict_wired_into_backends()
    rows.append(("E6. 冲突检测接入两个后端（check_conflict=True）", ok_e6, detail_e6))

    ok_e7, detail_e7 = test_conflict_default_off()
    rows.append(("E7. 默认不检测（现有调用方式行为不变）", ok_e7, detail_e7))

    ok_e8, detail_e8 = test_conflict_real_registerhotkey()
    rows.append(("E8. 真实 RegisterHotKey：占用 → 1409", ok_e8, detail_e8))

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
