# -*- coding: utf-8 -*-
"""EtherealFlow · 不抢焦点的悬浮窗（tkinter + Win32 扩展样式）。

关键约束（需求文档 FR-3 / 风险 #1 —— 「最该先验证的」）：

    悬浮窗**必须**设置 ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW``，
    否则会抢走前台焦点，导致文本注入到错误的窗口。

本模块把这个约束做实，并额外处理 Windows 特有的两个坑：

* **DPI**：tkinter 默认 DPI 不感知，缩放屏上既模糊、坐标又是错的 —— 建窗前先设。
* **定位**：取**光标所在显示器**的工作区底部居中（隐藏的窗口用
  ``current_monitor()`` 定位会一直停在启动屏）。

线程模型：tkinter 只能跑在主线程。其它线程通过 ``post()`` 投递消息，
主线程用 ``after()`` 轮询消费。
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from typing import Any, Optional

from . import win32 as w32

BG = "#1c1f26"
FG = "#f2f5fa"
FG_DIM = "#9aa4b5"
ACCENT = "#7cc4ff"
SEP = "#39404d"

STATE_LABEL = {
    "listening": "🎙 正在听…",
    "thinking": "✨ 修正中…",
    "translating": "🌐 翻译中…",
    "done": "✅ 完成",
}


class Overlay:
    """置顶、半透明、**不抢焦点**的流式识别悬浮窗。"""

    def __init__(self, width: int = 760, opacity: float = 0.94,
                 font_size: int = 15, translate: bool = False,
                 bottom_margin: int = 64) -> None:
        self.dpi_mode = w32.enable_dpi_awareness()
        self.width = width
        self.opacity = opacity
        self.font_size = font_size
        self.translate = translate
        self.bottom_margin = bottom_margin

        self._q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._state = "idle"
        self._text = ""
        self._translation = ""
        self._started_at: Optional[float] = None
        self._hwnd: Optional[int] = None
        self.visible = False

        self.root = tk.Tk()
        self.root.withdraw()  # 先隐藏：定位完成前不显示，避免闪一下
        self._build()
        self.root.after(30, self._drain)

    # -- 构建 -------------------------------------------------------------- #

    def _build(self) -> None:
        r = self.root
        r.overrideredirect(True)          # 无边框、不进任务栏
        # ⚠️ ``-topmost`` 和 ``-alpha`` **必须等 WS_EX_NOACTIVATE 加好之后再设**。
        # 实测：先设 ``-topmost``（它会触发窗口重新激活）会让悬浮窗抢走前台，
        # 症状是「说了话，但什么都没进去」（注入被「前台是悬浮窗自己」跳过）。
        # 所以这里只做无害的属性设置，置顶/透明度挪到样式设置之后（见文件末尾）。
        try:
            r.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass
        r.configure(bg=BG)

        pad = 16
        self.frame = tk.Frame(r, bg=BG, highlightthickness=1, highlightbackground=SEP)
        self.frame.pack(fill="both", expand=True)

        head = tk.Frame(self.frame, bg=BG)
        head.pack(fill="x", padx=pad, pady=(12, 0))
        self.status = tk.Label(head, text="", bg=BG, fg=ACCENT, anchor="w",
                               font=("Microsoft YaHei UI", self.font_size - 2))
        self.status.pack(side="left")
        self.timer = tk.Label(head, text="", bg=BG, fg=FG_DIM, anchor="e",
                              font=("Consolas", self.font_size - 2))
        self.timer.pack(side="right")

        wrap = self.width - 2 * pad - 4
        self.text_label = tk.Label(self.frame, text="", bg=BG, fg=FG, justify="left",
                                   anchor="w", wraplength=wrap,
                                   font=("Microsoft YaHei UI", self.font_size))
        self.text_label.pack(fill="x", padx=pad, pady=(8, 10))

        # 译文区（P4 才真正用上，先把位置留好）
        self.sep = tk.Frame(self.frame, bg=SEP, height=1)
        self.trans_label = tk.Label(self.frame, text="", bg=BG, fg=FG_DIM, justify="left",
                                    anchor="w", wraplength=wrap,
                                    font=("Microsoft YaHei UI", self.font_size - 1))
        if self.translate:
            self.sep.pack(fill="x", padx=pad)
            self.trans_label.pack(fill="x", padx=pad, pady=(8, 10))

        # ⚠️ 这里的顺序是踩了三次坑才定下来的，别轻易改：
        #
        # 1. Tk 的窗口层级是**延迟建立**的：``winfo_id()`` 立刻能拿到句柄，
        #    但它的父级 TkTopLevel 要等到 ``update_idletasks()`` 之后才出现
        #    （实测：在此之前 GetParent 返回 0，GA_ROOT 也只返回 TkChild 自己）。
        # 2. Tk 在 ``update_idletasks()`` 时会映射窗口，映射那一刻 Windows
        #    会激活它 —— 事后再加 NOACTIVATE 就来不及了。
        # 3. ``overrideredirect(True)`` 之后，GA_ROOT 会失效（返回 TkChild 自己），
        #    必须改用 GetParent 链找真正的 TkTopLevel。
        #
        # 所以顺序是：**先给当前句柄加 NOACTIVATE**（Tk 建顶层时会继承它，
        # 因而不被激活）→ update_idletasks() 建立层级 → 给真正的顶层补加（双保险）。
        #
        # 加错了会怎样：悬浮窗抢走前台 → 注入被「前台是悬浮窗自己」的保护逻辑
        # 跳过 → **「说了话，但什么都没进去」**。
        # 对照实验见 tools/repro/overlay_focus_timing.py。
        #
        # ⚠️ 同一个句柄**只能加一次**样式：对 child 调第二次 SetWindowLongW
        # 会让窗口重新激活（实测）。
        self._hwnd = int(r.winfo_id())
        # 记住「创建前谁在前台」——Tk 首次映射窗口时会激活它，
        # 事后要把焦点还回去（见 _restore_focus）。
        prev_fg = w32.foreground_window()
        prev_focus = w32.foreground_focus_state()[2]
        r.update_idletasks()                  # 层级在这里建立（TkChild → TkTopLevel）

        # 层级建立后才能拿到真正的顶层窗口 —— 用 GetParent 链，
        # GA_ROOT 在 overrideredirect 之后会失效（见 win32.top_level_window）。
        self._top_hwnd = w32.top_level_window(self._hwnd)
        w32.set_noactivate_style(self._top_hwnd or self._hwnd)

        # 样式齐了再设置顶 —— ``-topmost`` 会触发窗口重新激活，
        # 放在样式之前就会把前台抢走。
        r.attributes("-topmost", True)

        # ⚠️ 立刻把窗口**隐藏回去**。
        # update_idletasks() 已经把窗口映射过一次了 —— 那次映射会激活它
        # （Tk 首次映射必然激活，不受 WS_EX_NOACTIVATE 保护）。
        # 隐藏回去之后，「首次真正的映射」就发生在 show 的时候，
        # 而 show 走的是 Win32 的 SetWindowPos(SWP_NOACTIVATE)，
        # 那条路径**不会**激活窗口（实测 3/3 不抢焦点）。
        # 少了这一步，悬浮窗会在创建时就把用户的键盘焦点夺走。
        r.withdraw()
        w32.set_noactivate_style(self._top_hwnd or self._hwnd)
        self.noactivate_ok = w32.has_noactivate(self._hwnd)

        # exstyle 是**快照**，必须在 topmost 设好之后再取，
        # 否则读到的样式里没有 WS_EX_TOPMOST（实测踩过）。
        self.exstyle = w32.window_exstyle(self._top_hwnd or self._hwnd)

        # 首次映射可能抢了前台 → 立刻还回去
        self._restore_focus(prev_fg, prev_focus)

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    @property
    def top_hwnd(self) -> Optional[int]:
        return getattr(self, "_top_hwnd", None)

    # -- 对外接口（线程安全：任意线程可调） -------------------------------- #

    def post(self, kind: str, value: Any = None) -> None:
        self._q.put((kind, value))

    def show_listening(self) -> None:
        self.post("show", "listening")

    def set_text(self, text: str) -> None:
        self.post("text", text)

    def set_translation(self, text: str) -> None:
        self.post("translation", text)

    def set_state(self, state: str) -> None:
        self.post("state", state)

    def hide(self) -> None:
        self.post("hide", None)

    def hide_in(self, seconds: float) -> None:
        """延迟隐藏（线程安全：真正的 after 调度发生在主线程里）。"""
        self.post("hide_in", float(seconds))

    def stop(self) -> None:
        self.post("quit", None)

    # -- 主线程内部 -------------------------------------------------------- #

    def _drain(self) -> None:
        """主线程的消费循环。

        **这个循环绝对不能死。** 它靠 ``root.after`` 把自己排回去；
        如果处理某一条消息时抛了别的东西（几何计算、Tcl 调用……），
        异常会直接冲出函数、**跳过重排**，从此再也没有人消费队列 ——
        表现为：文字不再更新、``hide()`` 也永远不生效、窗口僵在屏幕上。

        实测踩到过：测试里工作线程已经跑完全部断言，主循环却再也不退出。
        所以这里逐条兜异常，并且**保证无论如何都重排自己**。
        """
        while True:
            try:
                kind, value = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                self._apply(kind, value)
            except tk.TclError:
                pass          # 窗口已被销毁，正常现象
            except Exception:  # noqa: BLE001  单条更新出错绝不能拖死循环
                pass
        try:
            self._tick_timer()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.after(30, self._drain)
        except tk.TclError:
            pass

    def _restore_focus(self, prev_fg: int, prev_focus: int) -> None:
        """把焦点还给显示之前的窗口。

        为什么需要：``WS_EX_NOACTIVATE`` 只能保证「**点击**这个窗口不会激活它」，
        但 Tk 首次映射窗口时 Windows 会主动激活它 —— 这一步不受 NOACTIVATE 保护，
        而且实测**有随机性**（同样代码有时抢有时不抢）。

        所以这里做兜底：显示前记下前台窗口与焦点控件，显示后如果**焦点**
        跑到悬浮窗自己身上，就**主动还回去**。

        ⚠️ 判断条件看的是**键盘焦点**，不是前台窗口 ——
        实测存在这种情况：前台窗口**从头到尾都没变**（还是用户的窗口），
        但**键盘焦点**被 Tk 首次映射夺走（变成 0 或落到我们窗口上）。
        这时候去调 SetForegroundWindow 反而会把键盘焦点改成
        「前台窗口自己」—— 而用户原本可能**没有**键盘焦点（原值是 0），
        于是「焦点没被抢走」被打破，比不还更糟。

        所以分两种情况：
        · 前台窗口被我们抢走了 → 必须还回去（连前台一起）
        · 只有键盘焦点被夺走 → 只还键盘焦点，而且**只还原来的那个控件**
          （原值是 0 就别造一个出来）
        """
        if not prev_fg:
            return
        fg = w32.foreground_window()
        fg_stolen = (fg == self.top_hwnd or fg == self.hwnd)
        cur_focus = w32.foreground_focus_state()[2]
        focus_stolen = (cur_focus != prev_focus)

        if not fg_stolen and not focus_stolen:
            return                      # 没抢过，别碰

        if not w32.user32.IsWindow(w32.wt.HWND(prev_fg)):
            return                      # 原窗口没了 → 不抢用户的当前窗口

        if fg_stolen:
            # 前台被抢走：连前台一起还。
            # ⚠️ 必须用 alt_tap=False —— 敲 Alt 解前台锁那一下会**改变键盘焦点**，
            # 而我们正要保证「键盘焦点没被抢走」。
            # 第 2 级的 AttachThreadInput 不需要敲 Alt，足够穿过前台锁。
            w32.force_foreground(prev_fg, alt_tap=False)
            if w32.foreground_window() != prev_fg:
                # 还不行 → 用完整兜底（含 Alt tap）
                w32.force_foreground(prev_fg)

        # 键盘焦点还回**原来那个控件**。
        # ⚠️ prev_focus == 0 表示「原本就没有键盘焦点」，这时**不能**凭空
        #   SetFocus(前台窗口) —— 那会把焦点状态从「无」变成「有」，
        #   反而制造出「焦点被抢走」的假象。
        #   反过来，原焦点**就是**前台窗口本身时（很常见：没有子控件被聚焦
        #   时焦点就落在窗口上），必须照原样还回去 —— 实测 Tk 首次映射会把它
        #   变成 0，不还就等于把用户的键盘焦点弄丢了。
        if prev_focus:
            try:
                # ⚠️ 必须跨线程设置：焦点控件属于**用户那个应用的线程**，
                # 我们在自己的 Tk 线程里直接 SetFocus 是无效的
                # （实测焦点会停在 0 —— 等于用户的键盘输入全落空）。
                w32.set_focus_across_threads(prev_focus)
            except Exception:  # noqa: BLE001 - 焦点还原失败不该拖垮显示
                pass

    def _apply(self, kind: str, value: Any) -> None:
        if kind == "show":
            self._started_at = time.monotonic()
            self._state = value or "listening"
            self._text = ""
            self._translation = ""
            self._render()
            self._place()
            self.visible = True
            if self._hwnd:
                # 显示前先记住「谁是前台」——Tk **首次映射**窗口时会激活它，
                # 这一步不可预测（实测有时抢有时不抢，与代码无关）。
                # 光靠 WS_EX_NOACTIVATE 挡不住首次映射，
                # 所以显示后把焦点**主动还回去**。
                # 做法参考 typeflux 的记录：显示时记焦点，显示后还原。
                prev_fg = w32.foreground_window()
                prev_focus = w32.foreground_focus_state()[2]
                w32.show_without_activating(self._hwnd)
                self._restore_focus(prev_fg, prev_focus)
        elif kind == "text":
            self._text = value or ""
            self._render()
            self._reheight()
        elif kind == "translation":
            self._translation = value or ""
            self._render()
            self._reheight()
        elif kind == "state":
            self._state = value
            self._render()
        elif kind == "hide":
            self.root.withdraw()
            self.visible = False
        elif kind == "hide_in":
            self.root.after(max(0, int(value * 1000)), self.hide)
        elif kind == "call":
            # 在 Tk 主线程上执行一个回调。
            # 存在的理由：tkinter **不是线程安全的**，从别的线程碰 Tk 控件
            # （哪怕只是 Toplevel().update()）都可能死锁。需要从后台线程操作
            # 窗口时，把动作包成函数投进来，交给主线程做。
            value()
        elif kind == "quit":
            if not getattr(self, "_closing", False):
                self._closing = True
                try:
                    self.root.quit()
                    self.root.destroy()
                except tk.TclError:
                    pass

    def _render(self) -> None:
        self.status.configure(text=STATE_LABEL.get(self._state, ""))
        placeholder = "…" if self._state in ("listening", "thinking", "translating") else ""
        self.text_label.configure(text=self._text or placeholder)
        if self.translate:
            self.trans_label.configure(text=self._translation)

    def _tick_timer(self) -> None:
        if self._state != "listening" or self._started_at is None:
            return
        el = int(time.monotonic() - self._started_at)
        self.timer.configure(text="%02d:%02d" % (el // 60, el % 60))

    def _target_y(self, height: int) -> int:
        _left, top, _right, bottom = w32.cursor_monitor_work_area()
        return max(top, bottom - height - self.bottom_margin)

    def _place(self) -> None:
        # ⚠️ 窗口必须保持**隐藏**状态做这件事。
        # ``update_idletasks()`` 在窗口可见时会把 Tk 的几何算完，
        # 但那一算会让 Tk 映射/激活窗口 → 抢走用户的键盘焦点
        # （实测有时抢有时不抢，与代码无关 —— 是 Windows 首次映射的行为）。
        # 窗口隐藏时调 update_idletasks 不会触发映射，实测 3/3 不抢焦点。
        hidden = not self.visible
        if hidden:
            self.root.withdraw()
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        left, _top, right, _bottom = w32.cursor_monitor_work_area()
        x = left + max(0, (right - left - self.width) // 2)
        self._store_rect(x, self._target_y(h), h)

    def _reheight(self) -> None:
        """文字变多时**底部固定、向上生长**，避免视觉跳动。"""
        h = self.root.winfo_reqheight()
        x = getattr(self, "_rect", (100, 100, 0, 0))[0]
        self._store_rect(x, self._target_y(h), h)

    def _store_rect(self, x: int, y: int, h: int) -> None:
        self._rect = (x, y, self.width, h)
        self.root.geometry("%dx%d+%d+%d" % (self.width, h, x, y))

    def screen_rect(self) -> tuple[int, int, int, int]:
        """悬浮窗当前的屏幕矩形 (x, y, w, h)，供截图/调试用。"""
        return getattr(self, "_rect", (0, 0, self.width, 0))

    # -- 运行 -------------------------------------------------------------- #

    def run(self) -> None:
        self.root.mainloop()
