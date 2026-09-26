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
        r.attributes("-topmost", True)
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

        r.update_idletasks()
        self._hwnd = int(r.winfo_id())
        # 注意：真正会被激活的是顶层窗口（winfo_id 给的是 TkChild 子窗口），
        # WS_EX_NOACTIVATE 必须加在顶层窗口上，否则悬浮窗照样抢焦点。
        self._top_hwnd = w32.top_level_window(self._hwnd)
        self.exstyle = w32.apply_noactivate(self._hwnd)   # 显示之前就加上
        self.noactivate_ok = w32.has_noactivate(self._hwnd)

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
                w32.show_without_activating(self._hwnd)
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
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        left, _top, right, _bottom = w32.cursor_monitor_work_area()
        x = left + max(0, (right - left - self.width) // 2)
        self._store_rect(x, self._target_y(h), h)

    def _reheight(self) -> None:
        """文字变多时**底部固定、向上生长**，避免视觉跳动。"""
        self.root.update_idletasks()
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
