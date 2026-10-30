# -*- coding: utf-8 -*-
"""EtherealFlow · 配置：默认值、加载、保存、路径解析。

配置**不放在仓库里**，而是放在用户的 AppData 下 —— 因为它可能含 API Key、
个人热词、以及本机路径，这些都不该进版本库（需求文档 §11.3）。

优先级：``--config 指定路径`` > ``%APPDATA%/EtherealFlow/config.json`` > 内置默认值。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

APP_NAME = "EtherealFlow"

# LLM 请求超时默认值。抽成常量是因为它散落在客户端、设置界面、配置三处，
# 各写一个 8.0 迟早会漂移。取 15 s 的理由：本机小模型第一次调用要加载权重，
# 8 秒偏紧；而超时只会**静默降级为识别原文**，所以宁可比模型慢一点，也别把结果丢了。
DEFAULT_LLM_TIMEOUT = 15.0

#: 单次生成的 token 上限。**必须把推理模型的思考量算进去** —— 推理模型会先花
#: max_tokens 做内部思考再产出答案，给 1024 时思考就把预算吃光了，content 为空，
#: 用户看到的现象是"修正一直失败"（详见 llm.LlmTruncatedError）。
DEFAULT_LLM_MAX_TOKENS = 4096

DEFAULT_CONFIG: dict[str, Any] = {
    "hotkey": {"keys": ["ctrl", "win"], "backend": "polling", "swallow": True},
    "asr": {"url": "ws://127.0.0.1:18300", "language": None, "context": "",
            "connect_hint": True, "max_utterance_sec": 30},
    # ``device`` = 输入设备序号，``device_name`` = 设备名。
    # 两者都存是因为**序号会漂移**（插拔声卡/换耳机后同一支麦可能换号），
    # 启动时按名称重解析、序号兜底。见 client/audio.resolve_input_device。
    "audio": {"device": None, "device_name": "", "chunk_ms": 160, "gain": 1.0},
    "overlay": {"width": 780, "opacity": 0.94, "font_size": 15, "translate": False},
    "llm": {
        "enabled": True,
        "base_url": "http://127.0.0.1:8317/v1",
        "api_key": "",
        "model": "",
        # 修正力度：none（不整理）/ light（轻度整理）/ deep（深度整理），
        # 也可以是用户自定义模式的 id。见 client/correction_modes.py
        "mode": "light",
        # 很多人说话是接着上一句说的，硬补一个结尾句号反而碍事
        "strip_trailing_period": False,
        # 用户自己新增的修正模式：[{id, name, desc, prompt, example}]
        "modes": [],
        # 本机小模型第一次调用要加载权重，8 秒偏紧；15 秒是「够用又不至于让人干等」的折中。
        # 超时会静默降级为识别原文，所以宁可比模型慢一点，也别把结果丢了。
        "timeout": DEFAULT_LLM_TIMEOUT,
        # 4096 不是随手写的：deepseek-flash 这类**推理模型**会先把 max_tokens 花在
        # 内部思考上，1024 时思考就吃光了预算、content 为空，表现成"修正永远失败"。
        # 修正本身的输出很短，但思考可能很长，所以预算要按思考+答案一起给。
        "max_tokens": DEFAULT_LLM_MAX_TOKENS,
        # 非空 = 完全覆盖所选模式的 Prompt（高级后门，界面上会提示）
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


def _copy_value(value: Any) -> Any:
    """递归复制 dict / list；标量按值复制（不可变，直接返回即可）。

    自己写而不用 ``copy.deepcopy``：配置里只可能出现 JSON 能表达的东西，
    显式写出来更可控 —— 也更容易看出「哪些结构被复制了」。
    """
    if isinstance(value, dict):
        return {k: _copy_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_value(v) for v in value]
    return value


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并，``override`` 覆盖 ``base``。返回新字典，不改动入参。

    ⚠️ 返回的是**深拷贝**（BF-5）：结果里每个嵌套 dict / list 都是新对象，
    跟 ``base`` / ``override`` 都不共享引用。
    以前只做了 ``dict(base)`` 浅拷贝，用户配置里**缺省**的那一段
    直接引用 ``DEFAULT_CONFIG`` 的同一个对象 —— 于是改一句
    ``cfg["audio"]["device"] = 3`` 就把**全局默认值**也改了，
    后面 ``save_config`` 拿被污染的基线做 diff，会把这次改动
    当成「跟默认值一样」而丢掉。
    """
    out = _copy_value(base)
    _merge_into(out, override)
    return out


def _merge_into(dst: dict, src: Any) -> None:
    """把 ``src`` **就地**合并进 ``dst``。

    ``dst`` 必须已经是副本；合并进来的值也会被复制，
    所以结果跟 ``src`` 同样不共享引用。
    """
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_into(dst[key], value)
        else:
            dst[key] = _copy_value(value)


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


# --------------------------------------------------------------------------- #
# 字段校验（BF-6）
# --------------------------------------------------------------------------- #
#
# 坏配置的杀伤力不在「值不对」，在**类型不对**：
#   cfg["overlay"]["width"] = "abc"  → overlay.py 里 TypeError
#   cfg["hotkey"]["keys"]  = "ctrl"  → hotkey.py 的 ValueError（uncaught → 崩）
# 而这两处都在**启动路径**上 —— 崩了就是「配置坏了所以程序起不来」。
#
# 所以这里逐字段校验：不合法的**回退该项的默认值**，程序照常起来，
# 同时把「哪些字段被回退了」告诉调用方，让它能提示用户。
#
# ⚠️ bool 是 int 的子类（``isinstance(True, int)`` 为 True），
# 所以「必须是数字」的字段必须显式排除 bool，「必须是 bool」的字段
# 必须显式写 ``isinstance(v, bool)`` —— 否则 True 会当成 1 混过去。

def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """数字（**不含 bool** —— True 不是数字，是布尔）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    return _is_number(value) and value > 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_int_or_none(value: Any) -> bool:
    return value is None or _is_int(value)


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _is_list_of_str(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _is_ratio(value: Any) -> bool:
    """0~1 之间的数字（悬浮窗透明度）。"""
    return _is_number(value) and 0.0 <= float(value) <= 1.0


def _is_hotkey_backend(value: Any) -> bool:
    """热键后端只能是 polling / hook —— 别的名字 create_hotkey 直接抛 ValueError。"""
    return isinstance(value, str) and value.strip().lower() in ("polling", "hook")


def _is_hotkey_keys(value: Any) -> bool:
    """快捷键必须是**认识的**键名 —— 不认识的话 hotkey.normalize_keys 会抛 ValueError。

    键表在 ``client.hotkey`` 里（它还 import 了 win32，只能在 Windows 上跑），
    所以这里**按需 import，失败就跳过名字检查** —— 类型检查照做，
    但不该因为环境问题把配置判成坏的。
    """
    if not _is_list_of_str(value):
        return False
    try:
        from .hotkey import normalize_keys
    except Exception:  # noqa: BLE001 - 拿不到键表就不校验名字
        return True
    try:
        normalize_keys(value)
    except (ValueError, TypeError):
        return False
    return True


#: 每一段的字段规则：段名 → {字段名: (判定函数, 期望描述)}
#: 描述会出现在给用户的提示里，所以写成人话。
_SECTION_RULES: dict[str, dict[str, tuple[Any, str]]] = {
    "hotkey": {
        "keys": (_is_hotkey_keys, "快捷键列表（如 [\"ctrl\", \"win\"]）"),
        "backend": (_is_hotkey_backend, "热键后端（polling 或 hook）"),
        "swallow": (_is_bool, "开关（true/false）"),
    },
    "asr": {
        "url": (_is_str, "识别服务地址（字符串）"),
        "language": (_is_str_or_none, "识别语言（字符串或 null）"),
        "context": (_is_str, "识别上下文（字符串）"),
        "max_utterance_sec": (_is_positive_number, "单次最长录音秒数（大于 0 的数字）"),
        "connect_hint": (_is_bool, "开关（true/false）"),
    },
    "audio": {
        "device": (_is_int_or_none, "麦克风序号（整数或 null）"),
        "device_name": (_is_str, "麦克风名称（字符串）"),
        "chunk_ms": (_is_positive_number, "音频块长毫秒（大于 0 的数字）"),
        "gain": (_is_number, "增益（数字）"),
    },
    "overlay": {
        "width": (_is_positive_number, "悬浮窗宽度（大于 0 的数字）"),
        "opacity": (_is_ratio, "悬浮窗透明度（0~1 的数字）"),
        "font_size": (_is_positive_number, "字号（大于 0 的数字）"),
        "translate": (_is_bool, "开关（true/false）"),
    },
    "llm": {
        "enabled": (_is_bool, "开关（true/false）"),
        "base_url": (_is_str, "模型服务地址（字符串）"),
        "api_key": (_is_str, "API Key（字符串）"),
        "model": (_is_str, "模型名（字符串）"),
        "mode": (_is_str, "修正模式 id（字符串）"),
        "strip_trailing_period": (_is_bool, "开关（true/false）"),
        "modes": (_is_list, "自定义修正模式列表"),
        "timeout": (_is_positive_number, "超时秒数（大于 0 的数字）"),
        "max_tokens": (_is_positive_number, "token 上限（大于 0 的数字）"),
        "prompt": (_is_str, "Prompt（字符串）"),
        "translate_prompt": (_is_str, "翻译 Prompt（字符串）"),
    },
    "inject": {
        "mode": (_is_str, "注入方式（字符串）"),
        "restore_clipboard": (_is_bool, "开关（true/false）"),
        "preserve_nontext_clipboard": (_is_bool, "开关（true/false）"),
        "restore_delay_ms": (_is_positive_number, "剪贴板还原延迟毫秒（大于 0 的数字）"),
        "typing_max_chars": (_is_positive_number, "逐字键入上限（大于 0 的数字）"),
    },
    "translate": {
        "enabled": (_is_bool, "开关（true/false）"),
        "target_language": (_is_str, "目标语言（字符串）"),
        "silence_sec": (_is_positive_number, "静默判定秒数（大于 0 的数字）"),
        "chunk_chars": (_is_positive_number, "分段字数（大于 0 的数字）"),
        "min_chars": (_is_positive_number, "最短字数（大于 0 的数字）"),
    },
    "history": {
        "max_items": (_is_positive_number, "历史条数（大于 0 的数字）"),
    },
}

#: 顶层（不在任何段里）的字段规则
_TOP_RULES: dict[str, tuple[Any, str]] = {
    "hotwords": (_is_list_of_str, "热词列表（字符串列表）"),
}


def _type_name(value: Any) -> str:
    """给提示用的类型名（中文）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, int):
        return "整数"
    if isinstance(value, float):
        return "小数"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "列表"
    if isinstance(value, dict):
        return "对象"
    return type(value).__name__


def validate_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把一份原始配置校验成**能安全使用**的配置。

    参数 ``raw`` 是从 JSON 解析出来的东西 —— **什么形状都收**
    （不是对象、字段类型全错、值是 null、嵌套是列表……）。

    返回 ``(配置, 回退说明列表)``：

    * 配置 = ``DEFAULT_CONFIG`` 合并 ``raw`` 之后、逐字段校验过的结果。
      合法字段**原样保留**，非法字段**回退成该项的默认值**，
      ``raw`` 里的**未知键也保留**（老行为，别破坏）。
    * 回退说明列表 = 每个被回退的字段一条中文说明，
      调用方可以拿去提示用户「有 N 项配置无效，已回退默认值」。

    **本函数不抛异常**：任何畸形输入都安全回退。
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return _copy_value(DEFAULT_CONFIG), [
            "整份配置不是对象（实际是%s），已整体回退默认值" % _type_name(raw)]

    cfg = deep_merge(DEFAULT_CONFIG, raw)

    # 段级：整段不是对象 → 整段回退（段里的字段没法逐个校验）
    for section, default in DEFAULT_CONFIG.items():
        if not isinstance(default, dict):
            continue
        if not isinstance(cfg.get(section), dict):
            cfg[section] = _copy_value(default)
            notes.append("%s：不是对象，已整段回退默认值" % section)

    # 字段级
    for section, rules in _SECTION_RULES.items():
        values = cfg.get(section)
        if not isinstance(values, dict):
            continue                      # 段级已经处理过了
        for field, (ok, expect) in rules.items():
            if field not in values:
                continue
            value = values[field]
            if ok(value):
                continue
            values[field] = _copy_value(DEFAULT_CONFIG[section][field])
            notes.append("%s.%s：应为%s，实际是%s，已回退默认值"
                         % (section, field, expect, _type_name(value)))

    # 顶层字段
    for field, (ok, expect) in _TOP_RULES.items():
        if field not in cfg:
            continue
        value = cfg[field]
        if ok(value):
            continue
        cfg[field] = _copy_value(DEFAULT_CONFIG[field])
        notes.append("%s：应为%s，实际是%s，已回退默认值"
                     % (field, expect, _type_name(value)))

    return cfg, notes


def load_config(path: Optional[str | Path] = None, *,
                warnings: Optional[list[str]] = None) -> dict[str, Any]:
    """读配置。文件不存在或损坏时**回退默认值**，绝不因为配置坏了就用不了。

    损坏的配置文件会被另存一份 ``<同名>.bak``（原文件不动）——
    见 ``_backup_original`` 里的原因。

    ``warnings``：可选的列表，传入后会把「哪些字段被回退了、为什么」写进去，
    供调用方提示用户。**不传就什么都不收集**（老调用方不受影响）。
    """
    target = Path(path) if path else default_config_path()
    if not target.exists():
        return _copy_value(DEFAULT_CONFIG)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return _copy_value(DEFAULT_CONFIG)
    try:
        raw = json.loads(text)
    except ValueError as exc:
        _backup_original(target)
        _note(warnings, "配置文件不是合法 JSON（%s），已整体回退默认值" % exc)
        return _copy_value(DEFAULT_CONFIG)
    cfg, notes = validate_config(raw)
    if notes:
        _backup_original(target)
        for note in notes:
            _note(warnings, note)
        _say("配置有 %d 项无效，已回退默认值：%s" % (len(notes), "；".join(notes)))
    return cfg


def _say(message: str) -> None:
    """往控制台说一句话。

    打包成**无控制台**的窗口程序后 ``sys.stdout`` 可能是 None，
    print 会直接抛异常 —— 那就等于「配置坏了 → 程序起不来」，
    正是 BF-6 要消灭的那种崩溃。所以这里必须吞掉所有异常。
    """
    try:
        print(message, flush=True)
    except Exception:  # noqa: BLE001 - 打印失败绝不能拖垮程序
        pass


def load_config_with_warnings(path: Optional[str | Path] = None
                               ) -> tuple[dict[str, Any], list[str]]:
    """``load_config`` + 回退说明。给需要提示用户的调用方用。"""
    notes: list[str] = []
    cfg = load_config(path, warnings=notes)
    return cfg, notes


def _note(warnings: Optional[list[str]], message: str) -> None:
    """把一条提示写进调用方给的列表；没给列表就丢弃（不打印、不抛异常）。"""
    if warnings is not None:
        warnings.append(message)


def _backup_original(target: Path) -> Optional[Path]:
    """把「损坏的」原文件另存成 ``<同名>.bak``，**原文件本身不动**。

    为什么必须留：``load_config`` 回退默认值后，用户只要在设置界面点一次
    「保存」，``save_config`` 就会拿默认值**覆盖**原文件 ——
    用户的 API Key、热词就此消失。先存一份 .bak，至少还能手工捞回来。

    已存在的 .bak 会被覆盖 —— 它始终代表「最近一次发现损坏时的原文件」，
    这样「.bak 内容 == 当时那个坏文件」这个不变式才成立。
    """
    backup = target.with_name(target.name + ".bak")
    try:
        shutil.copy2(str(target), str(backup))
    except OSError:
        return None          # 备份不了也不能让程序起不来
    return backup


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
