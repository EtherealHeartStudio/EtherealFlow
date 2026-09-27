# -*- coding: utf-8 -*-
"""EtherealFlow · 文本拼接。

看起来不值一提，但**真机联调实测踩到了**：把分段译好的英文用 ``"".join()``
硬拼，会拼出 ``The meeting is at three o'clock this afternoon.Discuss the new
version.`` —— 句子之间少了空格。中文拼接不需要空格，所以这个 bug 在纯中文
测试里永远暴露不出来，一接英文就现形。

同一个隐患也在 ASR 分段处：说太久会按 30 秒切段，段与段之间如果直接相加，
英文会粘成一个词。

判据用「边界两侧是不是 ASCII 字符」，而不是语言标签 —— 因为翻译方向可能是
``auto``，每一段各自判断语种，用标签反而会判错。
"""

from __future__ import annotations

from typing import Iterable

# 右侧以这些标点开头时，前面不需要空格（"…(续)"、"…, and" 之类）
_NO_SPACE_BEFORE = ")]}）】》」』>,.;:!?，。；：！？、…·"
# 左侧以这些字符结尾时，后面不需要空格
_NO_SPACE_AFTER = "([{（【《「『<"


def needs_space(left: str, right: str) -> bool:
    """两段文本相接处是否需要补一个空格。"""
    if not left or not right:
        return False
    a, b = left[-1], right[0]
    if a.isspace() or b.isspace():
        return False
    if a in _NO_SPACE_AFTER or b in _NO_SPACE_BEFORE:
        return False
    # 左边以 ASCII 结尾、右边以 ASCII 字母数字开头 → 是英文/代码的句子边界
    return a.isascii() and b.isascii() and b.isalnum()


def smart_join(parts: Iterable[str]) -> str:
    """拼接文本片段，按需在片段之间补空格。

    * 中文 + 中文  → 直接接（``会议在三点。`` + ``讨论新版本。``）
    * 英文 + 英文  → 补空格（``…afternoon.`` + ``Discuss…``）
    * 中文 + 英文 / 英文 + 中文 → 不补（中文本来就不用空格分隔）
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if out and needs_space(out, part):
            out += " "
        out += part
    return out
