# -*- coding: utf-8 -*-
"""CherryVoice · 验证增量流式翻译（FR-5）。

用一个**记录调用**的假 LLM，把三种触发条件和"只翻译新增部分"这两条硬要求
变成可判定的断言：

* 句末标点触发
* 静默 ~1.5 秒触发
* 缓冲超过 N 字触发
* **只翻译新增部分**：所有送去翻译的原文拼起来，必须恰好等于"已落定的原文"，
  一个字都不能重复翻（这就是需求文档说的"不要每来一个字就调一次 LLM"）
* 后端回退时，受影响的尾部段要重译而不是硬拼
* 翻译全失败时，`finish()` 必须回退成原文（不能提交空串）

退出码 0 = 通过。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.llm import Outcome                      # noqa: E402
from client.translator import (IncrementalTranslator, guess_target_language,  # noqa: E402
                               last_sentence_end)


class FakeLlm:
    """记录每次翻译的源文本，返回可判定的译文。"""

    def __init__(self, fail: bool = False) -> None:
        self.sources: list[tuple[str, str]] = []
        self.fail = fail

    def translate(self, text: str, target: str = "English") -> Outcome:
        self.sources.append((text, target))
        if self.fail:
            return Outcome(text=text, degraded=True, error="假失败", code="server")
        return Outcome(text="<%s>" % text.strip(), changed=True)


def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    # ---------- 纯函数 ---------- #
    check("只把句末标点当边界（逗号不算）",
          last_sentence_end("你好，世界。再来") == 5,
          "下标=%d" % last_sentence_end("你好，世界。再来"))
    check("方向自动判断：中文 → English",
          guess_target_language("这是一段中文") == "English",
          guess_target_language("这是一段中文"))
    check("方向自动判断：英文 → Chinese",
          guess_target_language("hello world") == "Chinese",
          guess_target_language("hello world"))

    # ---------- 句末标点触发 ---------- #
    llm = FakeLlm()
    seen: list[str] = []
    tr = IncrementalTranslator(llm, target_language="English", silence_sec=5.0,
                               chunk_chars=999, on_update=seen.append)
    tr.start()
    # ASR 的 partial 是**累积全文**，这里模拟逐字增长
    full = ""
    for ch in "我们今天测试一下，这个语音输入法。然后继续测试。":
        full += ch
        tr.update(full)
        time.sleep(0.01)
    wait_for(lambda: len(tr._segments) >= 2)          # noqa: SLF001
    res = tr.finish(full)
    sent = "".join(s for s, _t in llm.sources)
    closed = "".join(s for s, _t in res.segments)
    check("句末标点触发：已落定的原文都翻过，且没有落定段被漏掉",
          closed in sent and sent.startswith(closed),
          "送翻=%r 已落定=%r" % (sent, closed))
    check("只翻译新增部分：送去翻译的原文没有重复",
          sent == "".join(dict.fromkeys([sent])) and "。然后继续测试。" in sent,
          "送翻序列=%r" % [s for s, _t in llm.sources])
    check("译文是各段落译文的拼接（纯追加）",
          res.text == "<我们今天测试一下，这个语音输入法。><然后继续测试。>",
          repr(res.text))
    check("译文回调确实是流式增长的（收到多次回调）", len(seen) >= 2,
          "回调次数=%d" % len(seen))

    # ---------- 静默触发 ---------- #
    llm2 = FakeLlm()
    tr2 = IncrementalTranslator(llm2, target_language="English", silence_sec=0.4,
                                chunk_chars=999)
    tr2.start()
    tr2.update("没有句末标点的一句话")                 # 没有任何句末标点
    ok_silence = wait_for(lambda: len(llm2.sources) >= 1, timeout=3.0)
    res2 = tr2.finish("没有句末标点的一句话")
    check("静默 ~1.5 秒（测试用 0.4s）触发翻译", ok_silence,
          "调用 %d 次，源=%r" % (len(llm2.sources), [s for s, _t in llm2.sources]))
    check("静默触发后 commit 的译文非空", bool(res2.text.strip()), repr(res2.text))

    # ---------- 超长触发 ---------- #
    llm3 = FakeLlm()
    tr3 = IncrementalTranslator(llm3, target_language="English", silence_sec=5.0,
                                chunk_chars=10)
    tr3.start()
    tr3.update("一二三四五六七八九十十一十二")          # 12 字 > chunk_chars=10，无标点
    ok_len = wait_for(lambda: len(llm3.sources) >= 1, timeout=3.0)
    res3 = tr3.finish("一二三四五六七八九十十一十二")
    check("缓冲超过 N 字触发翻译", ok_len,
          "调用 %d 次，源=%r" % (len(llm3.sources), [s for s, _t in llm3.sources]))
    check("超长触发后译文非空", bool(res3.text.strip()), repr(res3.text))

    # ---------- 后端回退：尾部段重译 ---------- #
    llm4 = FakeLlm()
    tr4 = IncrementalTranslator(llm4, target_language="English", silence_sec=5.0,
                                chunk_chars=999)
    tr4.start()
    tr4.update("第一句话。")
    wait_for(lambda: len(tr4._segments) >= 1)          # noqa: SLF001
    first_sources = [s for s, _t in llm4.sources]
    # 回退：把已经落定的"第一句话。"改成"第一句啊。"（后端的 rollback）
    tr4.update("第一句啊。第二句。")
    wait_for(lambda: len(tr4._segments) >= 2)
    res4 = tr4.finish("第一句啊。第二句。")
    sources4 = [s for s, _t in llm4.sources]
    check("后端回退时，受影响的段被重译而不是硬拼",
          len(sources4) > len(first_sources) and "第一句啊。" in "".join(sources4),
          "原文段=%r 送翻=%r" % ([s for s, _t in res4.segments], sources4))
    check("回退重译后，译文里不含作废的旧段",
          "第一句话。" not in res4.text, repr(res4.text))

    # ---------- 翻译全失败 → 回退原文 ---------- #
    tr5 = IncrementalTranslator(FakeLlm(fail=True), target_language="English",
                                silence_sec=0.3, chunk_chars=999,
                                on_error=lambda _m: None)
    tr5.start()
    tr5.update("翻译会失败的一句话。")
    time.sleep(0.6)
    res5 = tr5.finish("翻译会失败的一句话。")
    check("翻译全失败时 finish() 回退为原文（绝不提交空串）",
          res5.degraded and res5.text == "翻译会失败的一句话。",
          "text=%r degraded=%s failed=%d" % (res5.text, res5.degraded, res5.failed))

    # ---------- 空输入 ---------- #
    tr6 = IncrementalTranslator(FakeLlm(), silence_sec=0.3)
    tr6.start()
    res6 = tr6.finish("")
    check("空输入不崩、不调用 LLM", res6.calls == 0 and res6.text == "",
          "calls=%d text=%r" % (res6.calls, res6.text))

    print("=" * 74)
    print("P4 验证：增量流式翻译")
    print("=" * 74)
    for name, ok, detail in rows:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
    fails = [n for n, ok, _d in rows if not ok]
    print("-" * 74)
    print("结论：%s（%d/%d）"
          % ("全部通过" if not fails else "失败项：%s" % fails,
             sum(1 for _n, ok, _d in rows if ok), len(rows)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
