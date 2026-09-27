# -*- coding: utf-8 -*-
"""EtherealFlow · 配置：默认值、加载、保存、路径解析。

配置**不放在仓库里**，而是放在用户的 AppData 下 —— 因为它可能含 API Key、
个人热词、以及本机路径，这些都不该进版本库（需求文档 §11.3）。

优先级：``--config 指定路径`` > ``%APPDATA%/EtherealFlow/config.json`` > 内置默认值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

APP_NAME = "EtherealFlow"

# LLM 请求超时默认值。抽成常量是因为它散落在客户端、设置界面、配置三处，
# 各写一个 8.0 迟早会漂移。取 15 s 的理由：本机小模型第一次调用要加载权重，
# 8 秒偏紧；而超时只会**静默降级为识别原文**，所以宁可比模型慢一点，也别把结果丢了。
DEFAULT_LLM_TIMEOUT = 15.0

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
        # 本机小模型第一次调用要加载权重，8 秒偏紧；15 秒是「够用又不至于让人干等」的折中。
        # 超时会静默降级为识别原文，所以宁可比模型慢一点，也别把结果丢了。
        "timeout": DEFAULT_LLM_TIMEOUT,
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


# 改过名。旧名字要留着做**配置迁移探测**，否则老用户升级后设置全丢
# （麦克风选择、热词、LLM 地址……）。见 _migrate_legacy()。
LEGACY_APP_NAMES = ("CherryVoice",)


def _base_config_root() -> Path:
    """配置根目录的父级。

    Windows 上先问系统要「漫游 AppData」的**真实路径**，而不是只看
    ``%APPDATA%`` 环境变量 —— 变量缺失时（受限进程/自动化环境）会掉到
    ``~/.etherealflow``，导致**同一个程序在不同启动方式下读写两份配置**。
    """
    if os.name == "nt":
        try:
            from .win32 import roaming_appdata
            found = roaming_appdata()
            if found:
                return Path(found)
        except Exception:  # noqa: BLE001
            pass
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base)
    return Path.home() / (".config" if os.name != "nt" else "." + APP_NAME.lower())


def _migrate_legacy(target: Path) -> None:
    """把老名字的配置目录整体搬到新名字下（只在目标不存在时搬一次）。"""
    if target.exists():
        return
    for old in LEGACY_APP_NAMES:
        old_dir = target.parent / old
        if not old_dir.exists():
            continue
        try:
            import shutil
            shutil.move(str(old_dir), str(target))
            print("已把配置目录从 %s 迁移到 %s" % (old_dir, target))
        except OSError:
            pass          # 迁移失败不该让程序起不来，大不了用默认值
        return


def default_config_dir() -> Path:
    target = _base_config_root() / APP_NAME
    _migrate_legacy(target)
    return target


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
