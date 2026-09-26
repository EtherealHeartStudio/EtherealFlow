# -*- coding: utf-8 -*-
"""EtherealFlow · 设置界面（FR-7）。

需求文档规定**只保留这六个分组**，一项都不多加：

    热键 | 识别 | 修正 | 翻译 | 注入 | 词典

``apply()`` / ``collect()`` 与窗口构建是分开的，所以配置往返可以脱离人手自动测
（见 ``tools/test_settings.py``）。
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

from . import win32 as w32
from .audio import list_input_devices
from .config import DEFAULT_CONFIG, load_config, save_config

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
        self.root.geometry("640x560")
        self.vars: dict[str, tk.Variable] = {}
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

    def _row(self, parent, label: str, widget, hint: str = "") -> None:
        line = ttk.Frame(parent)
        line.pack(fill="x", pady=4)
        ttk.Label(line, text=label, width=16, anchor="w").pack(side="left")
        widget.pack(side="left", fill="x", expand=True)
        if hint:
            ttk.Label(parent, text=hint, foreground="#777").pack(anchor="w", padx=(130, 0))

    def _build(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        # --- 热键 --- #
        f = self._tab(nb, "热键")
        self._row(f, "录音快捷键", ttk.Entry(f, textvariable=self._var("hotkey_keys")),
                  "用 + 连接，例如 ctrl+win、ctrl+alt、f9、ctrl+shift+space")
        self._row(f, "热键后端", ttk.Combobox(f, state="readonly",
                                          values=list(HOTKEY_BACKENDS),
                                          textvariable=self._var("hotkey_backend")),
                  "轮询吞不掉 Win 键；若松手会弹出开始菜单，就换成低级钩子")
        ttk.Label(f, text="说明：按住开始、松开结束；录音中按 Esc 取消本次输入。",
                  foreground="#777", wraplength=560, justify="left").pack(anchor="w", pady=(8, 0))

        # --- 识别 --- #
        f = self._tab(nb, "识别")
        self._row(f, "服务地址", ttk.Entry(f, textvariable=self._var("asr_url")),
                  "WSL 里的流式识别服务，默认 ws://127.0.0.1:18300")
        self._row(f, "语言", ttk.Combobox(f, state="readonly", values=list(LANGUAGES),
                                        textvariable=self._var("asr_language")))
        self._row(f, "识别上下文", ttk.Entry(f, textvariable=self._var("asr_context")),
                  "额外提示（热词另有专门分组）")

        # --- 修正 --- #
        f = self._tab(nb, "修正")
        ttk.Checkbutton(f, text="启用大模型修正（不可用时自动回退识别原文）",
                        variable=self._var("llm_enabled", tk.BooleanVar)
                        ).pack(anchor="w", pady=4)
        self._row(f, "接口地址", ttk.Entry(f, textvariable=self._var("llm_base_url")),
                  "OpenAI 兼容的 /v1/chat/completions")
        self._row(f, "API Key", ttk.Entry(f, textvariable=self._var("llm_api_key"), show="•"),
                  "只保存在本机配置文件里，不会进仓库")
        self._row(f, "模型名", ttk.Entry(f, textvariable=self._var("llm_model")),
                  "留空则用服务端默认模型")
        self._row(f, "超时（秒）", ttk.Entry(f, textvariable=self._var("llm_timeout")))
        ttk.Label(f, text="Prompt 模板（留空用内置；{text} 会被替换成识别原文）",
                  foreground="#777").pack(anchor="w", pady=(8, 0))
        self.prompt_text = tk.Text(f, height=7, wrap="word")
        self.prompt_text.pack(fill="both", expand=True)

        # --- 翻译 --- #
        f = self._tab(nb, "翻译")
        ttk.Checkbutton(f, text="启用流式翻译（开启后**提交译文**而不是原文）",
                        variable=self._var("translate_enabled", tk.BooleanVar)
                        ).pack(anchor="w", pady=4)
        self._row(f, "目标语言", ttk.Combobox(f, state="readonly",
                                          values=list(TARGET_LANGS),
                                          textvariable=self._var("translate_target")))
        self._row(f, "静默触发（秒）", ttk.Entry(f, textvariable=self._var("translate_silence")),
                  "停顿超过这么久就把当前这段送去翻译")
        self._row(f, "长度触发（字）", ttk.Entry(f, textvariable=self._var("translate_chunk")),
                  "累积超过这么多字就送去翻译（句末标点始终会触发）")

        # --- 注入 --- #
        f = self._tab(nb, "注入")
        self._row(f, "注入方式", ttk.Combobox(f, state="readonly", values=list(INJECT_MODES),
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
        self.vars["llm_base_url"].set(llm.get("base_url", ""))
        self.vars["llm_api_key"].set(llm.get("api_key", "") or "")
        self.vars["llm_model"].set(llm.get("model", "") or "")
        self.vars["llm_timeout"].set(str(llm.get("timeout", 8.0)))
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", llm.get("prompt", "") or "")

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
        cfg["llm"]["base_url"] = self.vars["llm_base_url"].get().strip()
        cfg["llm"]["api_key"] = self.vars["llm_api_key"].get().strip()
        cfg["llm"]["model"] = self.vars["llm_model"].get().strip()
        cfg["llm"]["timeout"] = self._num(self.vars["llm_timeout"].get(), 8.0)
        cfg["llm"]["prompt"] = self.prompt_text.get("1.0", "end").strip()

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
