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


#: 识别引擎 context 的长度上限。
#: R2T2 的 context 会被拼进解码提示里，太长既拖慢又可能挤掉真正的内容。
#: 200 字对一份热词表足够了（通常几十个词）。
ASR_CONTEXT_MAX_CHARS = 200


def build_asr_context(hotwords: Iterable[str] = (), context: str = "") -> str:
    """把**热词表**和**识别上下文**合成发给识别引擎的 context。

    需求文档 FR-7 写着「词典：热词列表（每行一个），**传给识别引擎作为
    context**」—— 但 v0.3.0 实际只把热词喂给了 LLM，**没给识别引擎**
    （界面和 README 都说传了，见 BF-4）。这个函数就是补上那一段。

    合成规则（有意为之，写在这里免得以后有人问为什么）：

    * 热词在前，识别上下文在后 —— 引擎对列表靠前的词更敏感；
    * 热词用「、」分隔，引擎的 context 就是一段提示文本；
    * 去重（同一个词写两遍没意义）；
    * 整体截断到 ``ASR_CONTEXT_MAX_CHARS`` 字 —— 超了会挤占解码预算；
    * 没有热词时**原样返回 context**，不改变任何现有行为。

    纯函数，不依赖 Tk 也不依赖网络，可以离线断言。
    """
    words: list[str] = []
    seen: set[str] = set()
    for raw in hotwords or ():
        for piece in str(raw).replace("，", ",").replace("、", ",").split(","):
            w = piece.strip()
            if not w or w in seen:
                continue
            seen.add(w)
            words.append(w)
    extra = str(context or "").strip()

    parts: list[str] = []
    if words:
        parts.append("、".join(words))
    if extra:
        parts.append(extra)
    out = "，".join(parts)
    if len(out) > ASR_CONTEXT_MAX_CHARS:
        out = out[:ASR_CONTEXT_MAX_CHARS].rstrip("、，")
    return out
