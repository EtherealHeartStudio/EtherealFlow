# -*- coding: utf-8 -*-
"""EtherealFlow · 修正模式（FR-4）。

「修正」不该只有一种力度。同一句话，有人只想把识别错字改掉、一个字都别动，
有人希望去掉口头禅再补上标点，还有人要的是能直接发出去的书面表达。
这三件事用一个 Prompt 是做不到的 —— 用力过猛会改写用户的话，用力不足又等于没用。

所以这里定三个**内置模式**，并把「模式」做成配置里的一等公民：

    不整理   只修明显的识别错误（同音字、专有名词），其余一字不动
    轻度整理 去口水词与自我修正、补标点，**保留措辞**
    深度整理 保留原意，整理成书面表达（理顺语序、可结构化）

用户还能自己加模式（``cfg["modes"]``）；自定义模式与内置模式在界面上同等对待。

这个模块刻意**不依赖 Tk、不依赖网络** —— 模式解析、自定义模式的增删、
结尾句号处理都是纯函数，可以直接离线断言（见 ``tools/test_llm.py``）。
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: 选中「深度整理」之类时用来做界面示例的同一句原文，方便直观对比三种力度
EXAMPLE_INPUT = "嗯我跟你说明天回议吧不对不对先讨论排期再聊产品方案"

BUILTIN_MODES: list[dict[str, str]] = [
    {
        "id": "none",
        "name": "不整理",
        "desc": "保留原本表达，仅修正明显的识别错误",
        "example": "我跟你说明天会议吧不对不对先讨论排期再聊产品方案",
        "prompt": """你是一个语音识别结果的**校对**助手。只做一件事：修正明显的识别错误。

1. 修正同音字、错别字（依据上下文判断，例如「回议」→「会议」）
2. 修正明显被识别错的专有名词与人名（可参考热词）
3. **其余一律保持原样**：不要删口水词、不要加标点、不要改数字写法、
   不要调整语序、不要替用户重写任何一句话

用户选这个模式，就是明确要求「别动我的字」。改动越少越好。

直接输出结果本身，不要任何解释、不要复述上述要求。

{hotwords}原文：
{text}""",
    },
    {
        "id": "light",
        "name": "轻度整理",
        "desc": "保留你的措辞，清理口头禅与自我修正，补上标点",
        "example": "关于明天的会议，要讨论几件事：先讨论排期，再聊产品方案。",
        "prompt": """你是一个文本修正助手。请修正下面这段语音识别结果：

1. 去除"嗯、啊、那个、就是"等口水词，以及"不对不对""我是说"这类自我修正
2. 补充正确的标点符号
3. 修正明显的同音错别字
4. 数字用阿拉伯数字
5. **保留用户原本的措辞与语气**：不要增删信息，不要重新组织结构，
   不要把一个口语化的说法改写成另一种表达

直接输出结果本身，不要任何解释、不要复述上述要求。

{hotwords}原文：
{text}""",
    },
    {
        "id": "deep",
        "name": "深度整理",
        "desc": "保留你的原意，调整措辞，整理成书面表达",
        "example": "明天会议讨论：1. 排期问题 2. 产品方案。",
        "prompt": """你是一个文本整理助手。请把下面这段语音识别结果整理成**书面表达**：

1. 去除口水词、重复与自我修正（"不对不对""我是说"之类）
2. 补充标点，理顺语序，删掉冗余的重复
3. 修正同音错别字，数字用阿拉伯数字
4. 原文若在列举事项，可整理成编号列表，让结构更清楚
5. 允许调整措辞与句式，但**必须完整保留原意与信息量**；
   **不得添加原文没有的任何内容**，不得替用户发表观点

直接输出结果本身，不要任何解释、不要复述上述要求。

{hotwords}原文：
{text}""",
    },
]

DEFAULT_MODE_ID = "light"

#: 用户填的模式名最短长度。太短的名字在卡片上没法看
_NAME_MIN = 1


def _norm_id(text: str) -> str:
    """把模式名变成可用作 id 的片段。中文保留，其余非字母数字折叠成 -。"""
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", (text or "").strip()).strip("-")
    return slug.lower() or "mode"


def custom_modes(cfg: Optional[dict]) -> list[dict[str, str]]:
    """配置里的自定义模式（做过规范化，坏数据不会让程序崩）。"""
    raw = (cfg or {}).get("modes") or []
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if len(name) < _NAME_MIN or not prompt:
            continue                      # 名字或 Prompt 为空的自定义模式直接忽略
        mid = str(item.get("id") or "").strip() or _norm_id(name)
        if any(m["id"] == mid for m in out) or any(m["id"] == mid for m in BUILTIN_MODES):
            mid = "custom-" + mid
        out.append({"id": mid, "name": name,
                    "desc": str(item.get("desc") or "").strip(),
                    "example": str(item.get("example") or "").strip(),
                    "prompt": prompt,
                    "custom": "1"})
    return out


def all_modes(cfg: Optional[dict]) -> list[dict[str, str]]:
    """内置 + 自定义，内置在前（界面上就是三张固定卡片 + 用户自己的卡片）。"""
    return [dict(m) for m in BUILTIN_MODES] + custom_modes(cfg)


def find_mode(cfg: Optional[dict], mode_id: str) -> Optional[dict[str, str]]:
    for m in all_modes(cfg):
        if m["id"] == mode_id:
            return m
    return None


def resolve_mode(cfg: Optional[dict]) -> dict[str, str]:
    """当前生效的模式。id 对不上（例如自定义模式被删了）就回退到默认模式。"""
    mode = find_mode(cfg, str((cfg or {}).get("mode") or ""))
    if mode is None:
        mode = find_mode(cfg, DEFAULT_MODE_ID) or dict(BUILTIN_MODES[0])
    return mode


def effective_prompt(cfg: Optional[dict], mode: Optional[dict] = None) -> str:
    """最终要用的修正 Prompt。

    ``cfg["prompt"]`` 非空时**完全覆盖**所选模式的 Prompt —— 这是留给高级用户的
    后门（v0.2 就有这个字段，不能因为新增了模式就把它废掉）。
    """
    override = str((cfg or {}).get("prompt") or "").strip()
    if override:
        return override
    return (mode or resolve_mode(cfg))["prompt"]


def prompt_is_overridden(cfg: Optional[dict]) -> bool:
    """界面上要据此提醒用户「你手写的 Prompt 正盖住所选模式」。"""
    return bool(str((cfg or {}).get("prompt") or "").strip())


def new_mode(cfg: Optional[dict], name: str, desc: str = "",
             prompt: str = "", example: str = "") -> dict[str, str]:
    """造一个自定义模式（id 保证不与现有模式冲突）。"""
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name:
        raise ValueError("模式名不能为空")
    if not prompt:
        raise ValueError("Prompt 不能为空")
    taken = {m["id"] for m in all_modes(cfg)}
    base = _norm_id(name)
    mid, n = base, 2
    while mid in taken:
        mid = "%s-%d" % (base, n)
        n += 1
    return {"id": mid, "name": name, "desc": (desc or "").strip(),
            "example": (example or "").strip(), "prompt": prompt, "custom": "1"}


def upsert_mode(cfg: dict, mode: dict[str, str]) -> dict[str, str]:
    """把自定义模式写进配置（同 id 覆盖）。返回写进去的那条。"""
    modes = [dict(m) for m in custom_modes(cfg)]
    for i, m in enumerate(modes):
        if m["id"] == mode["id"]:
            modes[i] = dict(mode)
            break
    else:
        modes.append(dict(mode))
    cfg["modes"] = modes
    return mode


def delete_mode(cfg: dict, mode_id: str) -> bool:
    """删掉一个自定义模式。**内置模式删不掉**（返回 False）。"""
    if any(m["id"] == mode_id for m in BUILTIN_MODES):
        return False
    modes = [dict(m) for m in custom_modes(cfg)]
    left = [m for m in modes if m["id"] != mode_id]
    if len(left) == len(modes):
        return False
    cfg["modes"] = left
    if str(cfg.get("mode") or "") == mode_id:
        cfg["mode"] = DEFAULT_MODE_ID       # 删掉的正是当前模式 → 回退
    return True


# --------------------------------------------------------------------------- #
# 结尾句号
# --------------------------------------------------------------------------- #

_TRAILING_PERIOD = "。．."


def strip_trailing_period(text: str) -> str:
    """去掉**结尾**的句号（中英文都算）。

    需求来自真实习惯：很多人说话是接着上一句说的，系统硬补一个句号反而碍事。
    只动结尾，不动句中的标点；全删空了就把原文还回去（绝不返回空串）。
    """
    original = text or ""
    out = original.rstrip()
    while out and out[-1] in _TRAILING_PERIOD:
        out = out[:-1].rstrip()
    return out if out else original


def postprocess(text: str, cfg: Optional[dict]) -> str:
    """修正结果的后处理（目前只有「去结尾句号」）。"""
    if (cfg or {}).get("strip_trailing_period"):
        return strip_trailing_period(text)
    return text


def describe_modes(cfg: Optional[dict]) -> list[str]:
    """给日志/自测用的一行式清单。"""
    active = resolve_mode(cfg)["id"]
    return ["%s%s%s" % (m["name"], "" if m.get("custom") else "",
                        "（当前）" if m["id"] == active else "")
            for m in all_modes(cfg)]


def modes_as_config(cfg: dict) -> list[dict[str, Any]]:
    """写回配置时用的纯数据形式（去掉内部用的 custom 标记）。"""
    return [{k: v for k, v in m.items() if k != "custom"}
            for m in custom_modes(cfg)]
