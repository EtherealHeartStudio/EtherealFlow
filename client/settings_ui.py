# -*- coding: utf-8 -*-
"""EtherealFlow · 设置界面（FR-7）。

需求文档规定**只保留这六个分组**，一项都不多加：

    热键 | 识别 | 修正 | 翻译 | 注入 | 词典

``apply()`` / ``collect()`` 与窗口构建是分开的，所以配置往返可以脱离人手自动测
（见 ``tools/test_settings.py``）。
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from . import win32 as w32
from .audio import list_input_devices
from .config import (DEFAULT_CONFIG, DEFAULT_LLM_MAX_TOKENS, DEFAULT_LLM_TIMEOUT,
                      load_config, save_config)
from .correction_modes import (DEFAULT_MODE_ID, all_modes, custom_modes,
                               delete_mode, find_mode, modes_as_config,
                               new_mode, resolve_mode, upsert_mode)
from .llm import PRESETS, apply_preset, default_client

LANGUAGES = {"自动检测": None, "中文": "Chinese", "英文": "English"}
INJECT_MODES = {"剪贴板 + Ctrl+V（兼容性最好）": "clipboard",
                "逐字键入（给拒绝粘贴的应用）": "typing"}
HOTKEY_BACKENDS = {"轮询 GetAsyncKeyState（兼容性好）": "polling",
                   "低级键盘钩子 WH_KEYBOARD_LL（能吞 Win 键）": "hook"}
TARGET_LANGS = {"自动（中→英 / 英→中）": "auto", "英文": "English",
                "中文": "Chinese", "日文": "Japanese", "韩文": "Korean"}


class SettingsWindow:
    def __init__(self, cfg: Optional[dict] = None,
                 config_path: Optional[str | Path] = None,
                 on_save: Optional[Callable[[dict], None]] = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self.config_path = Path(config_path) if config_path else None
        self.on_save = on_save
        w32.enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.title("EtherealFlow 设置")
        # 修正模式是三张并排的卡片，窗口得够宽
        self.root.geometry("860x700")
        self.vars: dict[str, tk.Variable] = {}
        # 自定义模式（可变，不是 Tk 变量）；由 cards_frame 里的卡片反映
        self._modes: list[dict[str, str]] = []
        # Prompt 覆盖（高级后门）。非空则盖住所选模式的 Prompt
        self._prompt_override = ""
        self._build()
        self.apply(self.cfg)

    # -- 构建 -------------------------------------------------------------- #

    def _var(self, key: str, kind=tk.StringVar, **kw) -> tk.Variable:
        v = kind(**kw)
        self.vars[key] = v
        return v

    def _tab(self, nb: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text=title)
        return frame

    def _scroll_tab(self, nb: ttk.Notebook, title: str) -> ttk.Frame:
        """可滚动的页签，返回真正用来装内容的 Frame。

        为什么需要它：修正页的内容会**随用户操作变多变少**（自定义模式每加一个就
        多一张卡片、多一行）。固定高度的窗口装不下时，底部的控件会被直接裁掉 ——
        实测加一个自定义模式后「接口地址」以下就看不见了，而用户根本不知道下面还有东西。
        """
        outer = ttk.Frame(nb)
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=12)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def resize(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())

        inner.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)

        def wheel(event) -> None:
            # 只在指针确实位于本页时才滚动，否则会把别的页签的滚轮也抢走
            under = canvas.winfo_containing(*canvas.winfo_pointerxy())
            if under is not None and (under is canvas
                                      or str(under).startswith(str(inner))):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        canvas.bind_all("<MouseWheel>", wheel)
        return inner

    def _row(self, parent, label: str, build, hint: str = ""):
        """一行 = 左侧标题 + 右侧控件 +（可选）下方灰色说明。

        ``build(container)`` 必须**在行 Frame 里**创建控件，而不是先在页签上建好
        再塞进来。两个原因，都是实测踩出来的：

        1. ``pack(in_=line)`` 只改**几何归属**、不改父子层级，看起来"能用"；
        2. 但只要控件先于行 Frame 创建，**Z 序上就会被后建的 Frame 盖住** ——
           位置算得完全正确，画面上却什么都看不见。

        第 2 条极其隐蔽：几何断言全过、``winfo_ismapped()`` 也是 1，
        只有真去截图像素级看，才发现输入框根本没画出来。所以这里坚持
        让控件从一开始就长在行 Frame 里，不留任何"先建后塞"的余地。
        """
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=4)
        ttk.Label(line, text=label, width=16, anchor="w").pack(side="left")
        widget = build(line)
        widget.pack(side="left", fill="x", expand=True)
        if hint:
            # wraplength 必须有，否则长提示会被窗口右边缘裁掉半句话
            ttk.Label(parent, text=hint, foreground="#777", wraplength=430,
                      justify="left").pack(anchor="w", padx=(130, 0))
        return widget

    # -- 修正模式卡片 ------------------------------------------------------ #

    def _rebuild_cards(self) -> None:
        """（重）画模式卡片。内置三个 + 用户自定义的，一视同仁地排成网格。"""
        for w in self.cards_frame.winfo_children():
            w.destroy()
        active = self.vars["llm_mode"].get()
        modes = all_modes({"modes": self._modes})
        for i, m in enumerate(modes):
            card = self._make_card(self.cards_frame, m, m["id"] == active)
            card.grid(row=i // 3, column=i % 3, sticky="nsew",
                      padx=(0 if i % 3 == 0 else 8, 0), pady=(0, 8))
        for c in range(3):
            self.cards_frame.grid_columnconfigure(c, weight=1, uniform="modecard")

        cur = find_mode({"modes": self._modes}, active)
        if cur is None:
            self.mode_hint.configure(text="")
        elif cur.get("custom"):
            self.mode_hint.configure(text="当前：%s（自定义，可编辑/删除）" % cur["name"])
        else:
            self.mode_hint.configure(text="当前：%s（内置，不可删除）" % cur["name"])

    def _make_card(self, parent, mode: dict, selected: bool) -> tk.Frame:
        """一张模式卡片：名称 + 说明 + 示例框。整张卡片可点。

        用 ``tk``（不是 ``ttk``）是因为需要**自己控制底色与边框色**来表现选中态 ——
        ttk 的默认主题在这种强调表达上很别扭。
        """
        border = "#2f6fed" if selected else "#d5d5d5"
        bg = "#eaf1ff" if selected else "#f7f7f7"
        card = tk.Frame(parent, bg=bg, bd=0, cursor="hand2",
                        highlightbackground=border, highlightcolor=border,
                        highlightthickness=2)
        inner = tk.Frame(card, bg=bg)
        inner.pack(fill="both", expand=True, padx=10, pady=8)
        tk.Label(inner, text=mode["name"], bg=bg, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold"),
                 fg="#2f6fed" if selected else "#222222").pack(fill="x")
        tk.Label(inner, text=mode.get("desc") or "（无说明）", bg=bg, anchor="w",
                 justify="left", wraplength=205, fg="#666666",
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(3, 6))
        if mode.get("example"):
            box = tk.Frame(inner, bg="#ffffff", bd=0,
                           highlightbackground="#e2e2e2", highlightthickness=1)
            box.pack(fill="both", expand=True)
            tk.Label(box, text=mode["example"], bg="#ffffff", anchor="nw",
                     justify="left", wraplength=185, fg="#444444",
                     font=("Microsoft YaHei UI", 9)).pack(fill="both", expand=True,
                                                          padx=6, pady=5)
        if mode.get("custom"):
            tk.Label(inner, text="自定义", bg=bg, fg="#9a6b00",
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(4, 0))

        # 卡片上任何一处都要能点中，否则用户以为没反应
        def bind_all(widget) -> None:
            widget.bind("<Button-1>", lambda _e, mid=mode["id"]: self._select_mode(mid))
            for child in widget.winfo_children():
                bind_all(child)

        bind_all(card)
        return card

    def _select_mode(self, mode_id: str) -> None:
        if self.vars["llm_mode"].get() == mode_id:
            return
        self.vars["llm_mode"].set(mode_id)
        self._rebuild_cards()

    def _selected_mode(self) -> Optional[dict[str, str]]:
        return find_mode({"modes": self._modes}, self.vars["llm_mode"].get())

    def _edit_selected(self) -> None:
        mode = self._selected_mode()
        if mode is None or not mode.get("custom"):
            messagebox.showinfo(
                "只能编辑自定义模式",
                "内置的三个模式是固定的（它们的 Prompt 是产品的默认行为）。\n\n"
                "想改成你习惯的写法，就点「新增模式」做一个你自己的。")
            return
        self._mode_dialog(mode)

    def _delete_selected(self) -> None:
        mode = self._selected_mode()
        if mode is None or not mode.get("custom"):
            messagebox.showinfo("只能删除自定义模式", "内置的三个模式删不掉。")
            return
        if not messagebox.askyesno("删除模式", "确定删除「%s」吗？" % mode["name"]):
            return
        tmp = {"modes": self._modes, "mode": self.vars["llm_mode"].get()}
        delete_mode(tmp, mode["id"])
        self._modes = tmp["modes"]
        self.vars["llm_mode"].set(tmp["mode"])      # 删掉当前模式时已回退到默认
        self._rebuild_cards()

    def _mode_dialog(self, mode: Optional[dict] = None) -> None:
        """新增 / 编辑一个自定义模式。"""
        editing = mode is not None
        dlg = tk.Toplevel(self.root)
        dlg.title("编辑修正模式" if editing else "新增修正模式")
        dlg.transient(self.root)
        dlg.geometry("600x520")
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill="both", expand=True)

        def field(label: str, hint: str, value: str, width: int = 0):
            ttk.Label(body, text=label, anchor="w").pack(anchor="w", pady=(8, 0))
            ttk.Label(body, text=hint, foreground="#777", wraplength=540,
                      justify="left").pack(anchor="w")
            var = tk.StringVar(value=value)
            ttk.Entry(body, textvariable=var).pack(fill="x", pady=(2, 0))
            return var

        name_var = field("模式名", "显示在卡片上，例如「邮件体」「会议纪要」",
                         (mode or {}).get("name", ""))
        desc_var = field("一句话说明", "卡片标题下面那行",
                         (mode or {}).get("desc", ""))
        ex_var = field("示例（可选）", "卡片里灰色框展示的效果示例",
                       (mode or {}).get("example", ""))

        ttk.Label(body, text="Prompt", anchor="w").pack(anchor="w", pady=(8, 0))
        ttk.Label(body, text="用 {text} 表示识别原文（必需）；{hotwords} 可选，"
                             "会被插到「原文：」之前",
                  foreground="#777", wraplength=540, justify="left").pack(anchor="w")
        text = tk.Text(body, height=9, wrap="word")
        text.pack(fill="both", expand=True, pady=(2, 0))
        text.insert("1.0", (mode or {}).get("prompt", ""))

        def save() -> None:
            name = name_var.get().strip()
            prompt = text.get("1.0", "end").strip()
            if not name or not prompt:
                messagebox.showerror("还不完整", "模式名和 Prompt 都不能为空。", parent=dlg)
                return
            if "{text}" not in prompt and not messagebox.askyesno(
                    "Prompt 里没有 {text}",
                    "没有 {text} 占位符的话，识别原文可能不会传给模型。\n仍然保存吗？",
                    parent=dlg):
                return
            tmp = {"modes": self._modes}
            if editing:
                updated = dict(mode)
                updated.update({"name": name, "desc": desc_var.get().strip(),
                                "example": ex_var.get().strip(), "prompt": prompt})
            else:
                try:
                    updated = new_mode(tmp, name, desc_var.get().strip(), prompt,
                                       ex_var.get().strip())
                except ValueError as exc:
                    messagebox.showerror("保存失败", str(exc), parent=dlg)
                    return
            upsert_mode(tmp, updated)
            self._modes = tmp["modes"]
            self.vars["llm_mode"].set(updated["id"])    # 新建/编辑完直接选中它
            dlg.destroy()
            self._rebuild_cards()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="保存", command=save).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=6)

    def _refresh_override_label(self) -> None:
        if self._prompt_override.strip():
            self.override_label.configure(text="已启用（会盖住所选模式）",
                                          foreground="#9a6b00")
        else:
            self.override_label.configure(text="未启用（用所选模式的 Prompt）",
                                          foreground="#777")

    def _edit_override(self) -> None:
        """编辑「Prompt 覆盖」。放进弹窗而不是摊在页面上：
        它是个高级后门，和「自定义模式」功能重叠，不该占着主界面。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("高级：Prompt 覆盖")
        dlg.transient(self.root)
        dlg.geometry("640x440")
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="非空时，它会**完全替代**所选修正模式的 Prompt。",
                  foreground="#777").pack(anchor="w")
        ttk.Label(body, text="留空 = 用所选模式自己的 Prompt（推荐；想固定一套自己的写法，"
                             "用「新增模式」更好）。",
                  foreground="#777", wraplength=580, justify="left").pack(anchor="w", pady=(2, 8))
        text = tk.Text(body, height=10, wrap="word")
        text.pack(fill="both", expand=True)
        text.insert("1.0", self._prompt_override)

        def clear() -> None:
            self._prompt_override = ""
            self._refresh_override_label()
            dlg.destroy()

        def save() -> None:
            self._prompt_override = text.get("1.0", "end").strip()
            self._refresh_override_label()
            dlg.destroy()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="保存", command=save).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=6)
        ttk.Button(btns, text="清空覆盖", command=clear).pack(side="left")

    def _build(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        # --- 热键 --- #
        f = self._tab(nb, "热键")
        self._row(f, "录音快捷键",
                  lambda p: ttk.Entry(p, textvariable=self._var("hotkey_keys")),
                  "用 + 连接，例如 ctrl+win、ctrl+alt、f9、ctrl+shift+space")
        self._row(f, "热键后端",
                  lambda p: ttk.Combobox(p, state="readonly",
                                         values=list(HOTKEY_BACKENDS),
                                         textvariable=self._var("hotkey_backend")),
                  "轮询吞不掉 Win 键；若松手会弹出开始菜单，就换成低级钩子")
        ttk.Label(f, text="说明：按住开始、松开结束；录音中按 Esc 取消本次输入。",
                  foreground="#777", wraplength=560, justify="left").pack(anchor="w", pady=(8, 0))

        # --- 识别 --- #
        f = self._tab(nb, "识别")
        self._row(f, "服务地址",
                  lambda p: ttk.Entry(p, textvariable=self._var("asr_url")),
                  "WSL 里的流式识别服务，默认 ws://127.0.0.1:18300")
        self._row(f, "语言",
                  lambda p: ttk.Combobox(p, state="readonly", values=list(LANGUAGES),
                                         textvariable=self._var("asr_language")))
        self._row(f, "识别上下文",
                  lambda p: ttk.Entry(p, textvariable=self._var("asr_context")),
                  "额外提示（热词另有专门分组）")

        # --- 修正 --- #
        # 内容会随自定义模式增减而变高，所以这一页是可滚动的
        f = self._scroll_tab(nb, "修正")
        ttk.Checkbutton(f, text="启用大模型修正（不可用时自动回退识别原文）",
                        variable=self._var("llm_enabled", tk.BooleanVar)
                        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(f, text="修正模式",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(f, text="同一句话，三种力度。点卡片选择；也可以自己加一种。",
                  foreground="#777").pack(anchor="w")
        # 当前模式 id。卡片的高亮由它决定，collect() 也读它
        self._var("llm_mode")
        self.cards_frame = tk.Frame(f)
        self.cards_frame.pack(fill="x", pady=(8, 2))

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="新增模式",
                   command=lambda: self._mode_dialog(None)).pack(side="left")
        ttk.Button(bar, text="编辑选中", command=self._edit_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="删除选中", command=self._delete_selected).pack(side="left")
        self.mode_hint = ttk.Label(bar, text="", foreground="#777")
        self.mode_hint.pack(side="left", padx=10)

        ttk.Checkbutton(f, text="去除结尾句号（接着说下一句时，不必被硬塞一个句号）",
                        variable=self._var("llm_strip_period", tk.BooleanVar)
                        ).pack(anchor="w", pady=(0, 8))

        def _mk_preset(p):
            self.preset_combo = ttk.Combobox(p, state="readonly",
                                             values=[x[0] for x in PRESETS],
                                             textvariable=self._var("llm_preset"))
            self.preset_combo.bind("<<ComboboxSelected>>", self._apply_preset)
            return self.preset_combo

        self._row(f, "模型接入", _mk_preset,
                  "先选一个预设；本机模型不需要 API Key，选预设不会清掉你已填的 Key")

        def _mk_override(p):
            box = ttk.Frame(p)
            ttk.Button(box, text="编辑…", command=self._edit_override).pack(side="left")
            self.override_label = ttk.Label(box, text="", foreground="#777")
            self.override_label.pack(side="left", padx=8)
            return box

        self._row(f, "Prompt 覆盖", _mk_override,
                  "高级用法：非空时会盖住所选模式的 Prompt。想固定一套自己的写法，"
                  "更推荐用上面的「新增模式」")

        def _mk_addr(p):
            box = ttk.Frame(p)
            ttk.Entry(box, textvariable=self._var("llm_base_url")
                      ).pack(side="left", fill="x", expand=True)
            self.test_btn = ttk.Button(box, text="测试连接", command=self._test_llm)
            self.test_btn.pack(side="left", padx=(6, 0))
            return box

        self._row(f, "接口地址", _mk_addr,
                  "任何 OpenAI 兼容地址。本机 Ollama 填 http://localhost:11434 即可")

        self._row(f, "API Key",
                  lambda p: ttk.Entry(p, textvariable=self._var("llm_api_key"), show="•"),
                  "只存本机配置文件，不会进仓库；本机模型留空")

        def _mk_model(p):
            self.model_combo = ttk.Combobox(p, textvariable=self._var("llm_model"))
            return self.model_combo

        self._row(f, "模型名", _mk_model,
                  "留空用服务端默认；点「测试连接」会把服务端模型拉下来供选择。"
                  "尽量选**非推理**的 instruct 模型：推理模型会先思考，"
                  "修一句话可能要十几秒（本机小模型通常 1 秒内）")
        self._row(f, "超时（秒）",
                  lambda p: ttk.Entry(p, textvariable=self._var("llm_timeout")),
                  "本机小模型首次调用要加载权重，可适当调大")

        self.llm_status = ttk.Label(f, text="", foreground="#777", wraplength=800,
                                    justify="left")
        self.llm_status.pack(anchor="w", pady=(6, 0))

        # --- 翻译 --- #
        f = self._tab(nb, "翻译")
        ttk.Checkbutton(f, text="启用流式翻译（开启后**提交译文**而不是原文）",
                        variable=self._var("translate_enabled", tk.BooleanVar)
                        ).pack(anchor="w", pady=4)
        self._row(f, "目标语言",
                  lambda p: ttk.Combobox(p, state="readonly", values=list(TARGET_LANGS),
                                         textvariable=self._var("translate_target")))
        self._row(f, "静默触发（秒）",
                  lambda p: ttk.Entry(p, textvariable=self._var("translate_silence")),
                  "停顿超过这么久就把当前这段送去翻译")
        self._row(f, "长度触发（字）",
                  lambda p: ttk.Entry(p, textvariable=self._var("translate_chunk")),
                  "累积超过这么多字就送去翻译（句末标点始终会触发）")

        # --- 注入 --- #
        f = self._tab(nb, "注入")
        self._row(f, "注入方式",
                  lambda p: ttk.Combobox(p, state="readonly", values=list(INJECT_MODES),
                                         textvariable=self._var("inject_mode")))
        ttk.Checkbutton(f, text="注入后还原剪贴板（仅当剪贴板内容没被改过）",
                        variable=self._var("inject_restore", tk.BooleanVar)
                        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(f, text="剪贴板里有图片等非文本内容时，改用逐字键入以免破坏它",
                        variable=self._var("inject_preserve", tk.BooleanVar)
                        ).pack(anchor="w", pady=4)

        # --- 词典 --- #
        f = self._tab(nb, "词典")
        ttk.Label(f, text="热词（每行一个）。会作为 context 传给识别引擎，也供修正时纠错。",
                  foreground="#777", wraplength=560, justify="left").pack(anchor="w")
        self.hotword_text = tk.Text(f, height=12, wrap="word")
        self.hotword_text.pack(fill="both", expand=True, pady=(6, 0))

        # --- 底部 --- #
        bar = ttk.Frame(self.root, padding=10)
        bar.pack(fill="x")
        self.path_label = ttk.Label(
            bar, text="配置文件：%s" % (self.config_path or "(默认位置)"),
            foreground="#777")
        self.path_label.pack(side="left")
        ttk.Button(bar, text="恢复默认", command=self._reset).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=self.root.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="保存", command=self._save).pack(side="right", padx=4)

    # -- 绑定数据 ---------------------------------------------------------- #

    @staticmethod
    def _pick(mapping: dict, value: Any, default_key: str) -> str:
        for label, raw in mapping.items():
            if raw == value:
                return label
        return default_key

    def apply(self, cfg: dict) -> None:
        """把配置填进控件。"""
        self.cfg = cfg
        hk = cfg["hotkey"]
        self.vars["hotkey_keys"].set("+".join(hk.get("keys", ["ctrl", "win"])))
        self.vars["hotkey_backend"].set(
            self._pick(HOTKEY_BACKENDS, hk.get("backend", "polling"),
                       list(HOTKEY_BACKENDS)[0]))

        asr = cfg["asr"]
        self.vars["asr_url"].set(asr.get("url", ""))
        self.vars["asr_language"].set(self._pick(LANGUAGES, asr.get("language"), "自动检测"))
        self.vars["asr_context"].set(asr.get("context", "") or "")

        llm = cfg["llm"]
        self.vars["llm_enabled"].set(bool(llm.get("enabled", True)))
        # 预设下拉不预选任何一项：当前配置可能是手填的，硬套一个预设会误导用户
        self.vars["llm_preset"].set("")
        # 修正模式：自定义模式的来源，以及当前选中的是哪一个。
        # 模式 id 要**经过解析**再塞进界面：用户可能在别处删掉了自定义模式、
        # 或手改配置写错 id —— 直接沿用会让界面停"一张卡片都没选中"的状态，
        # 而且保存时会把那个坏 id 原样写回去。
        self._modes = custom_modes(llm)
        self.vars["llm_mode"].set(
            resolve_mode({"modes": self._modes, "mode": llm.get("mode")})["id"])
        self.vars["llm_strip_period"].set(bool(llm.get("strip_trailing_period")))
        self._prompt_override = str(llm.get("prompt") or "")
        self._rebuild_cards()
        self._refresh_override_label()
        self.vars["llm_base_url"].set(llm.get("base_url", ""))
        self.vars["llm_api_key"].set(llm.get("api_key", "") or "")
        self.vars["llm_model"].set(llm.get("model", "") or "")
        self.vars["llm_timeout"].set(str(llm.get("timeout", DEFAULT_LLM_TIMEOUT)))

        tr = cfg["translate"]
        self.vars["translate_enabled"].set(bool(tr.get("enabled", False)))
        self.vars["translate_target"].set(
            self._pick(TARGET_LANGS, tr.get("target_language", "auto"),
                       list(TARGET_LANGS)[0]))
        self.vars["translate_silence"].set(str(tr.get("silence_sec", 1.5)))
        self.vars["translate_chunk"].set(str(tr.get("chunk_chars", 40)))

        inj = cfg["inject"]
        self.vars["inject_mode"].set(
            self._pick(INJECT_MODES, inj.get("mode", "clipboard"), list(INJECT_MODES)[0]))
        self.vars["inject_restore"].set(bool(inj.get("restore_clipboard", True)))
        self.vars["inject_preserve"].set(bool(inj.get("preserve_nontext_clipboard", True)))

        self.hotword_text.delete("1.0", "end")
        self.hotword_text.insert("1.0", "\n".join(cfg.get("hotwords") or []))

    @staticmethod
    def _num(text: str, default: float, cast=float):
        try:
            return cast(str(text).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_keys(text: str) -> list[str]:
        parts = [p.strip().lower() for p in str(text).replace("，", "+").split("+")]
        return [p for p in parts if p] or ["ctrl", "win"]

    def collect(self) -> dict:
        """从控件读出配置（在原有 cfg 上覆盖，不改动其它字段）。"""
        cfg = json.loads(json.dumps(self.cfg))
        cfg["hotkey"]["keys"] = self._parse_keys(self.vars["hotkey_keys"].get())
        cfg["hotkey"]["backend"] = HOTKEY_BACKENDS.get(
            self.vars["hotkey_backend"].get(), "polling")
        cfg["hotkey"]["swallow"] = cfg["hotkey"].get("swallow", True)

        cfg["asr"]["url"] = self.vars["asr_url"].get().strip()
        cfg["asr"]["language"] = LANGUAGES.get(self.vars["asr_language"].get())
        cfg["asr"]["context"] = self.vars["asr_context"].get().strip()

        cfg["llm"]["enabled"] = bool(self.vars["llm_enabled"].get())
        cfg["llm"]["mode"] = self.vars["llm_mode"].get() or DEFAULT_MODE_ID
        cfg["llm"]["strip_trailing_period"] = bool(self.vars["llm_strip_period"].get())
        # 只写自定义模式；内置的三个在代码里，不进配置文件
        cfg["llm"]["modes"] = modes_as_config({"modes": self._modes})
        cfg["llm"]["base_url"] = self.vars["llm_base_url"].get().strip()
        cfg["llm"]["api_key"] = self.vars["llm_api_key"].get().strip()
        cfg["llm"]["model"] = self.vars["llm_model"].get().strip()
        cfg["llm"]["timeout"] = self._num(self.vars["llm_timeout"].get(), DEFAULT_LLM_TIMEOUT)
        cfg["llm"]["prompt"] = self._prompt_override.strip()

        cfg["translate"]["enabled"] = bool(self.vars["translate_enabled"].get())
        cfg["translate"]["target_language"] = TARGET_LANGS.get(
            self.vars["translate_target"].get(), "auto")
        cfg["translate"]["silence_sec"] = self._num(
            self.vars["translate_silence"].get(), 1.5)
        cfg["translate"]["chunk_chars"] = self._num(
            self.vars["translate_chunk"].get(), 40, int)

        cfg["inject"]["mode"] = INJECT_MODES.get(self.vars["inject_mode"].get(), "clipboard")
        cfg["inject"]["restore_clipboard"] = bool(self.vars["inject_restore"].get())
        cfg["inject"]["preserve_nontext_clipboard"] = bool(self.vars["inject_preserve"].get())

        raw = self.hotword_text.get("1.0", "end")
        cfg["hotwords"] = [w.strip() for w in raw.replace("，", "\n").splitlines() if w.strip()]
        return cfg

    # -- 动作 -------------------------------------------------------------- #

    def _apply_preset(self, _event=None) -> None:
        """选了预设就填好地址/模型；Key 只在预设明确要清空时才清。"""
        name = self.vars["llm_preset"].get()
        for label, values in PRESETS:
            if label != name:
                continue
            if "base_url" in values:
                self.vars["llm_base_url"].set(values["base_url"])
            if "model" in values:
                self.vars["llm_model"].set(values["model"])
            if "api_key" in values:
                self.vars["llm_api_key"].set(values["api_key"])
            self.llm_status.configure(
                text="已套用预设「%s」。点「测试连接」确认能连通。\n"
                     "留意：本机服务需要它真的在运行（例如先启动 Ollama）。" % label,
                foreground="#777")
            return

    def _llm_fields(self) -> dict:
        """只取 LLM 那几项，避免为了测试连接去解析整张表单。"""
        return {
            "base_url": self.vars["llm_base_url"].get().strip(),
            "api_key": self.vars["llm_api_key"].get().strip(),
            "model": self.vars["llm_model"].get().strip(),
            "timeout": self._num(self.vars["llm_timeout"].get(), DEFAULT_LLM_TIMEOUT),
        }

    def _test_llm(self) -> None:
        """在后台线程里测连接 —— 请求可能阻塞好几秒，绝不能卡住界面。"""
        fields = self._llm_fields()
        self.test_btn.state(["disabled"])
        self.llm_status.configure(
            text="正在连接 %s …" % (fields["base_url"] or "(未填地址)"),
            foreground="#777")
        self._llm_queue = queue.Queue()

        def work() -> None:
            # **绝不在这里碰 Tk。** tkinter 不是线程安全的，从子线程直接调
            # ``root.after()`` 会抛 ``RuntimeError: main thread is not in main loop``
            # （mainloop 未运行于该线程时），而实测过它还会让 mainloop 永远不退出。
            # overlay.py 早就为同一个问题定下了做法：子线程只往队列里放，
            # 主线程轮询消费。这里保持一致。
            try:
                client = default_client(fields)
                ok, msg = client.test_connection()
                models = client.list_models() if ok else []
            except Exception as exc:  # noqa: BLE001
                ok, msg, models = False, repr(exc), []
            self._llm_queue.put((ok, msg, models))

        threading.Thread(target=work, daemon=True, name="llm-test").start()
        self.root.after(100, self._poll_llm)

    def _poll_llm(self) -> None:
        """在主线程里消费测试结果（只有主线程能碰控件）。"""
        try:
            ok, msg, models = self._llm_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_llm)      # 还没跑完，继续等
            return
        self._llm_result(ok, msg, models)

    def _llm_result(self, ok: bool, msg: str, models: list) -> None:
        self.test_btn.state(["!disabled"])
        self.llm_status.configure(text=("✓ " if ok else "✗ ") + msg,
                                  foreground="#1a7f37" if ok else "#c0392b")
        if models:
            self.model_combo.configure(values=models)

    def _reset(self) -> None:
        if messagebox.askyesno("恢复默认", "确定把所有设置恢复为默认值吗？"):
            self.apply(json.loads(json.dumps(DEFAULT_CONFIG)))

    def _save(self) -> None:
        cfg = self.collect()
        try:
            path = save_config(cfg, self.config_path)
        except OSError as exc:
            messagebox.showerror("保存失败", "写配置失败：%s" % exc)
            return
        self.cfg = cfg
        if self.on_save:
            self.on_save(cfg)
        messagebox.showinfo("已保存", "设置已保存到：\n%s\n\n重启 EtherealFlow 后生效。" % path)

    def run(self) -> None:
        self.root.mainloop()
