# -*- coding: utf-8 -*-
"""EtherealFlow · 文本注入（FR-6）。

主方案 **剪贴板 + 模拟 Ctrl+V**（兼容性最好），备选 **SendInput 逐字键入**
（给拒绝粘贴的应用兜底）。全部用 ctypes，零第三方依赖。

四个容易做坏的地方，这里都处理了：

1. **剪贴板保护**。注入前把用户原来的剪贴板内容存下来，注入后还原 —— 而且
   **只在剪贴板内容仍等于本次注入文本时才还原**（VoiceX 的做法）。否则用户在
   这期间复制了新东西，还原反而把人家刚复制的内容冲掉。
2. **非文本剪贴板**。图片等格式没法用纯 ctypes 完整备份。默认检测到非文本内容就
   走「逐字键入」，不动用户的剪贴板；可用 ``preserve_nontext_clipboard=False``
   强制用剪贴板方案（并如实告警会丢内容）。
3. **剪贴板写入确认**。写完先轮询确认真的落地了再发 Ctrl+V（VoiceX 用 5×20ms）。
4. **注入目标**。注入前重新解析当前前台窗口并记录标题/PID —— 类型 flux 的经验是
   在**生成结束后**再解析当前活动输入，而不是绑定按热键那一刻的窗口。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import win32 as w32

CF_UNICODETEXT = 13
CF_TEXT = 1
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL, VK_V = 0x11, 0x56

user32 = w32.user32
kernel32 = w32.kernel32

user32.OpenClipboard.argtypes = [wt.HWND]
user32.SetClipboardData.argtypes = [ctypes.c_uint, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = wt.HANDLE
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]


# --------------------------------------------------------------------------- #
# 键盘事件
# --------------------------------------------------------------------------- #

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


def _send(*events: tuple[int, int, int]) -> int:
    """events: (vk, scan, flags) 列表，一次 SendInput 全发出去。"""
    arr = (INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1
        arr[i].u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                 time=0, dwExtraInfo=None)
    return int(user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)))


def press_ctrl_v() -> int:
    return _send((VK_CONTROL, 0, 0), (VK_V, 0, 0),
                 (VK_V, 0, KEYEVENTF_KEYUP), (VK_CONTROL, 0, KEYEVENTF_KEYUP))


def type_unicode(text: str) -> int:
    """逐字键入（KEYEVENTF_UNICODE），代理对也能正确处理。"""
    sent = 0
    for ch in text:
        code = ord(ch)
        units = [code] if code <= 0xFFFF else [
            ((code - 0x10000) >> 10) + 0xD800, ((code - 0x10000) & 0x3FF) + 0xDC00]
        for unit in units:
            sent += _send((0, unit, KEYEVENTF_UNICODE),
                          (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return sent


# --------------------------------------------------------------------------- #
# 剪贴板
# --------------------------------------------------------------------------- #

@dataclass
class ClipboardSnapshot:
    text: Optional[str] = None
    has_text: bool = False
    has_other: bool = False          # 含非文本格式（图片等），无法完整备份

    def describe(self) -> str:
        kinds = []
        if self.has_text:
            kinds.append("文本(%d 字)" % len(self.text or ""))
        if self.has_other:
            kinds.append("非文本格式")
        return "、".join(kinds) or "空"


def _open_clipboard(retries: int = 10, delay: float = 0.02) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def clipboard_list_formats() -> list[int]:
    """在已打开的剪贴板上枚举格式（调用方负责 Open/Close）。"""
    formats: list[int] = []
    fmt = 0
    while True:
        fmt = int(user32.EnumClipboardFormats(fmt))
        if fmt == 0:
            break
        formats.append(fmt)
    return formats


#: 「**能安全备份并还原**」的剪贴板格式白名单。
#:
#: ⚠️ 白名单只列**纯文本类**格式。白名单之外的任何格式（位图、文件列表、
#: 自定义格式……）我们**备份不了**，一旦 EmptyClipboard 就会**永久销毁**
#: 用户的数据。
#:
#: 之前的白名单里混进了 CF_BITMAP(2) / CF_DIB(8) / CF_DIBV5(17) ——
#: 那是「这些格式**不会**被当成非文本」的意思，于是**图片剪贴板检测不到**，
#: 注入时直接被 EmptyClipboard 销毁（实测会丢用户的截图）。
#:
#: 这里改成：只有 CF_UNICODETEXT / CF_TEXT / CF_OEMTEXT / CF_LOCALE
#: 算「可安全备份」，其余一律算 has_other → 触发「改用逐字键入」。
CF_OEMTEXT = 7
CF_LOCALE = 16

#: 可安全备份的文本类格式
_SAFE_TEXT_FORMATS = frozenset({CF_UNICODETEXT, CF_TEXT, CF_OEMTEXT, CF_LOCALE})


def clipboard_snapshot() -> ClipboardSnapshot:
    snap = ClipboardSnapshot()
    if not _open_clipboard():
        return snap
    try:
        formats = clipboard_list_formats()
        # 只有「可安全备份的文本类」之外的格式才算 has_other。
        # 私有/无编号格式（0x0200+）、位图、文件列表……全算。
        snap.has_other = any(f not in _SAFE_TEXT_FORMATS for f in formats)
        if CF_UNICODETEXT in formats:
            snap.text = clipboard_get_text_locked()
            snap.has_text = snap.text is not None
    finally:
        user32.CloseClipboard()
    return snap


def clipboard_get_text_locked() -> Optional[str]:
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        return ctypes.wstring_at(ptr)
    finally:
        kernel32.GlobalUnlock(handle)


def clipboard_get_text() -> Optional[str]:
    if not _open_clipboard():
        return None
    try:
        return clipboard_get_text_locked()
    finally:
        user32.CloseClipboard()


def clipboard_set_text(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        # 成功后内存所有权归系统，绝不能再 GlobalFree
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        return True
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #

#: 注入结果的三态。
#:
#: 为什么不是 bool —— 「**发出去了**」和「**确认写进去了**」是两件事。
#: 参考 typeflux 的 ``TextDeliveryResult{ delivered, unconfirmed, notApplied }``：
#: 把「派发成功」与「确认写入」分开记录，否则用户会看到「注入成功」
#: 但目标框里什么都没有。
#:
#: · ``delivered``    —— 已投递，且有证据表明目标接受了（回读到了内容）
#: · ``unconfirmed``  —— 已投递，但**无法确认**目标是否接受
#:                        （剪贴板路径拿不到回读证据时属于这一档）
#: · ``not_applied``  —— 明确没进去（SendInput 被拒、目标不是输入框等）
DELIVERED = "delivered"
UNCONFIRMED = "unconfirmed"
NOT_APPLIED = "not_applied"

_STATUS_LABEL = {
    DELIVERED: "已注入",
    UNCONFIRMED: "已投递（未能确认目标是否接收）",
    NOT_APPLIED: "未注入",
}


@dataclass
class InjectResult:
    #: 三态之一（见上面的 DELIVERED / UNCONFIRMED / NOT_APPLIED）
    status: str = NOT_APPLIED
    method: str = ""
    target_title: str = ""
    target_class: str = ""
    target_pid: int = 0
    restored: bool = False
    #: 剪贴板还原是否**已经结束**（无论成功与否）。
    #: 调用方靠它判断「结果已经完整了，可以打最终日志了」——
    #: 还原是延迟执行的（默认 500 ms 后），日志打得早就看不到还原结果。
    restore_done: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """兼容旧字段：只有「明确进去了」才算 ok。

        ``unconfirmed`` **不算** ok —— 宁可让调用方知道「不确定」，
        也不要给它一个假的成功。旧的 ``res.ok = True`` 赋值会走
        ``__post_init__`` 之外，所以这里保留一个 setter 兼容。
        """
        return self.status == DELIVERED

    @ok.setter
    def ok(self, value: bool) -> None:
        # 兼容旧的 `res.ok = True` 写法：True 记为「已投递但未确认」
        # （因为旧代码无从确认），False 记为「未注入」。
        self.status = UNCONFIRMED if value else NOT_APPLIED

    def describe(self) -> str:
        bits = ["%s → %s [%s] pid=%d" % (_STATUS_LABEL.get(self.status, self.status),
                                         self.target_title or "(无标题)",
                                         self.target_class, self.target_pid)]
        if self.method:
            bits.append("方式=%s" % self.method)
        if self.restored:
            bits.append("剪贴板已还原")
        if self.warnings:
            bits.append("告警: " + "; ".join(self.warnings))
        if self.error:
            bits.append("错误: %s" % self.error)
        return " | ".join(bits)


class Injector:
    def __init__(self, mode: str = "clipboard", restore_clipboard: bool = True,
                 restore_delay_ms: int = 500, typing_max_chars: int = 500,
                 preserve_nontext_clipboard: bool = True) -> None:
        self.mode = (mode or "clipboard").lower()
        self.restore_clipboard = bool(restore_clipboard)
        self.restore_delay_ms = int(restore_delay_ms)
        self.typing_max_chars = int(typing_max_chars)
        self.preserve_nontext_clipboard = bool(preserve_nontext_clipboard)
        # 还原是延迟执行的；若期间又注入了一次，旧的那次**不许**再动剪贴板，
        # 否则会把新内容冲掉（这是实测踩到的竞态）。
        self._generation = 0
        self._gen_lock = threading.Lock()

    def _next_generation(self) -> int:
        with self._gen_lock:
            self._generation += 1
            return self._generation

    # -- 对外 -------------------------------------------------------------- #

    def inject(self, text: str) -> InjectResult:
        res = InjectResult()
        if not text:
            res.error = "文本为空"
            return res

        target = w32.foreground_window()
        res.target_title = w32.window_title(target)
        res.target_class = w32.class_name(target)
        res.target_pid = w32.window_pid(target)

        mode = self.mode
        snap = ClipboardSnapshot()
        if mode in ("clipboard", "auto"):
            snap = clipboard_snapshot()
            if snap.has_other and self.preserve_nontext_clipboard:
                res.warnings.append("检测到非文本剪贴板内容（%s），改用逐字键入以免破坏它"
                                    % snap.describe())
                mode = "typing"
            elif snap.has_other:
                res.warnings.append("剪贴板里有非文本内容，本次注入后会丢失（无法备份）")
            if mode == "clipboard" and len(text) > self.typing_max_chars:
                pass  # 剪贴板方案对长文本更合适
        if mode == "typing" and len(text) > self.typing_max_chars * 20:
            res.warnings.append("文本很长（%d 字），逐字键入会比较慢" % len(text))

        if mode == "typing":
            return self._inject_typing(text, res)
        return self._inject_clipboard(text, res, snap)

    # -- 剪贴板 + Ctrl+V --------------------------------------------------- #

    def _inject_clipboard(self, text: str, res: InjectResult,
                          snap: ClipboardSnapshot) -> InjectResult:
        res.method = "剪贴板+Ctrl+V"
        if not clipboard_set_text(text):
            res.warnings.append("写剪贴板失败，退回逐字键入")
            return self._inject_typing(text, res)

        if not self._wait_clipboard_text(text):
            # 回读不一致 —— 剪贴板里**不是**我们要粘的内容。
            # 继续按 Ctrl+V 会粘到旧内容（用户看到的是莫名其妙的另一段话），
            # 比不粘更糟。改走逐字键入。
            res.warnings.append("剪贴板写入后回读不一致（可能有剪贴板拦截层），"
                                "改用逐字键入以免粘错内容")
            return self._inject_typing(text, res)

        # ⚠️ SendInput 的返回值**必须检查**：它返回实际插入的事件数，
        # 返回 0 表示**一个都没插进去** —— 典型原因是目标窗口以更高权限运行
        # （UIPI 拦截）。以前忽略返回值，无条件报「成功」，
        # 结果是「报了成功但目标框里什么都没有」。
        sent = press_ctrl_v()
        if sent <= 0:
            res.warnings.append("模拟 Ctrl+V 被系统拒绝（SendInput 返回 0，"
                                "常见于目标程序以管理员权限运行）")
            res.status = NOT_APPLIED
            res.error = "SendInput 被拒绝"
            # 剪贴板里还留着我们的内容 → 还原掉，别污染用户剪贴板
            if self.restore_clipboard:
                gen = self._next_generation()
                threading.Thread(target=self._restore_later,
                                 args=(text, snap, res, gen), daemon=True).start()
            return res

        time.sleep(0.08)
        # 剪贴板路径拿不到「目标真的接收了」的证据 → 记为「已投递但未确认」
        res.status = UNCONFIRMED

        if self.restore_clipboard:
            gen = self._next_generation()
            threading.Thread(target=self._restore_later, args=(text, snap, res, gen),
                             daemon=True).start()
        return res

    @staticmethod
    def _wait_clipboard_text(text: str, tries: int = 5, delay: float = 0.02) -> bool:
        for _ in range(tries):
            if clipboard_get_text() == text:
                return True
            time.sleep(delay)
        return False

    def _restore_later(self, injected: str, snap: ClipboardSnapshot,
                       res: InjectResult, generation: int) -> None:
        time.sleep(self.restore_delay_ms / 1000.0)
        res.restored = self._restore_now(injected, snap, generation)
        if not res.restored:
            # 还原失败**必须让用户知道** —— 否则剪贴板里会一直留着
            # 我们注入的那段文字，而用户完全不知情。
            # 两种情况：① 用户期间自己复制了别的（那不算失败，是正确的保护）
            #            ② 真的还原失败（剪贴板被锁/拦截层）
            if clipboard_get_text() == injected:
                res.warnings.append("剪贴板未能还原（仍是你这次输入的内容）")
            else:
                res.warnings.append("剪贴板已恢复成你复制的内容（还原期间你复制了新东西）")
        # 标记「结果已完整」——调用方据此决定何时打最终日志
        res.restore_done = True

    def _restore_now(self, injected: str, snap: ClipboardSnapshot,
                     generation: int) -> bool:
        """返回是否**确认**还原成功。

        实测教训：``SetClipboardData`` 返回成功并不等于剪贴板真变了
        （某些环境有剪贴板拦截层，写成功后回读仍是旧内容）。
        所以这里写完**必须回读确认**，否则 ``restored`` 就是假的。
        """
        with self._gen_lock:
            if generation != self._generation:
                return False        # 已经又注入过一次，别动剪贴板
        current = clipboard_get_text()
        if current != injected:
            # 用户在这期间自己复制了别的东西 —— 还原会冲掉它，所以放弃还原
            return False
        if snap.has_text:
            target = snap.text or ""
            if not clipboard_set_text(target):
                return False
            return clipboard_get_text() == target
        # 原本没有文本（可能为空，也可能只有图片等）—— 至少把我们写进去的清掉
        if not user32.OpenClipboard(None):
            return False
        try:
            if not user32.EmptyClipboard():
                return False
        finally:
            user32.CloseClipboard()
        return clipboard_get_text() is None

    # -- 逐字键入 ---------------------------------------------------------- #

    def _inject_typing(self, text: str, res: InjectResult) -> InjectResult:
        res.method = "SendInput 逐字键入"
        try:
            sent = type_unicode(text)
        except Exception as exc:  # noqa: BLE001
            res.error = "逐字键入失败：%r" % exc
            res.status = NOT_APPLIED
            return res
        # type_unicode 返回成功插入的事件数；每个字符 2 个事件（按下+抬起）。
        # 一半都没进去 = 被系统拒绝了。
        if sent < len(text) * 2:
            res.warnings.append("逐字键入只送出了 %d/%d 个事件"
                                "（SendInput 可能被系统拦截，"
                                "常见于目标程序以管理员权限运行）"
                                % (sent, len(text) * 2))
            res.status = NOT_APPLIED
            res.error = "SendInput 被拒绝"
            return res
        # 逐字键入同样拿不到「目标接收了」的证据 → 未确认
        res.status = UNCONFIRMED
        return res


# --------------------------------------------------------------------------- #

def default_injector(cfg: Optional[dict] = None) -> Injector:
    cfg = cfg or {}
    return Injector(mode=cfg.get("mode", "clipboard"),
                    restore_clipboard=cfg.get("restore_clipboard", True),
                    restore_delay_ms=cfg.get("restore_delay_ms", 500),
                    typing_max_chars=cfg.get("typing_max_chars", 500),
                    preserve_nontext_clipboard=cfg.get("preserve_nontext_clipboard", True))
