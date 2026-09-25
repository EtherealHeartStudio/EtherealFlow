# -*- coding: utf-8 -*-
"""CherryVoice · 配置：默认值、加载、保存、路径解析。

配置**不放在仓库里**，而是放在用户的 AppData 下 —— 因为它可能含 API Key、
个人热词、以及本机路径，这些都不该进版本库（需求文档 §11.3）。

优先级：``--config 指定路径`` > ``%APPDATA%/CherryVoice/config.json`` > 内置默认值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

APP_NAME = "CherryVoice"

DEFAULT_CONFIG: dict[str, Any] = {
    "hotkey": {"keys": ["ctrl", "win"], "backend": "polling", "swallow": True},
    "asr": {"url": "ws://127.0.0.1:18300", "language": None, "context": "",
            "connect_hint": True, "max_utterance_sec": 30},
    "audio": {"device": None, "chunk_ms": 160, "gain": 1.0},
    "overlay": {"width": 780, "opacity": 0.94, "font_size": 15, "translate": False},
    "llm": {
        "enabled": True,
        "base_url": "http://127.0.0.1:8317/v1",
        "api_key": "",
        "model": "",
        "timeout": 8.0,
        "max_tokens": 1024,
        "prompt": "",
        "translate_prompt": "",
    },
    "inject": {"mode": "clipboard", "restore_clipboard": True, "restore_delay_ms": 500,
               "typing_max_chars": 500, "preserve_nontext_clipboard": True},
    "translate": {"enabled": False, "target_language": "auto",
                  "silence_sec": 1.5, "chunk_chars": 40, "min_chars": 2},
    # FR-7 词典：热词既传给识别引擎当 context，也给 LLM 做同音纠错参考
    "hotwords": [],
    "history": {"max_items": 20},
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并，``override`` 覆盖 ``base``。返回新字典，不改动入参。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / (".config" if os.name != "nt" else "." + APP_NAME.lower())


def default_config_path() -> Path:
    return default_config_dir() / "config.json"


def load_config(path: Optional[str | Path] = None) -> dict[str, Any]:
    """读配置。文件不存在或损坏时**回退默认值**，绝不因为配置坏了就用不了。"""
    target = Path(path) if path else default_config_path()
    if not target.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置根节点必须是对象")
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: dict, path: Optional[str | Path] = None) -> Path:
    """写配置。只写用户改过的键，避免把默认值也固化进去。"""
    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    diff = _diff(DEFAULT_CONFIG, cfg)
    target.write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


def _diff(base: dict, cur: dict) -> dict:
    """只保留与默认值不同的部分（嵌套）；``hotwords`` 这类列表整体比较。"""
    out: dict[str, Any] = {}
    for key, value in cur.items():
        if key not in base:
            out[key] = value
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            sub = _diff(base[key], value)
            if sub:
                out[key] = sub
        elif value != base.get(key):
            out[key] = value
    return out


def mask_secret(value: str, keep: int = 4) -> str:
    """给界面显示用的打码：``sk-abcd…wxyz``。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return "%s…%s" % (value[:keep], value[-keep:])
