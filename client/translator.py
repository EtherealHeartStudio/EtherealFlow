# -*- coding: utf-8 -*-
"""EtherealFlow · 增量流式翻译（FR-5）。

需求文档的硬要求：**"不要每来一个字就调一次 LLM"**，按
标点/句末、静默 ~1.5 秒、或缓冲超过 N 字 才触发一次，且只翻译新增部分。

竞品调研的结论是：**这块没有现成实现可抄** —— CapsWriter 只在终稿后整段处理，
opentypeless 把翻译写成 system prompt 里一句指令，openwhispr 是"整段 cleanup →
整段 translate"。原文/译文并行滚动必须自研。

设计：**分段 + append-only**

* 原文按**句子边界**切成"已落定段"（``_segments``）和当前"开口段"（``_open``）；
* 已落定段**永不重译** —— 只翻译新增的那部分，符合"只翻译新增部分"的要求；
* 开口段不显示译文，等它被落定（遇到句末标点、静默、或超长）才翻，
  这样译文区是**纯追加**的，不会边翻边抖；
* 万一后端回退了已经落定的文字（R2T2 号称 append-only，但 UI 必须容错），
  就把受影响的尾部段丢掉重译，而不是硬拼。

**翻译方向是会话级的（BF-10）**：中英混说时，一句话不该被劈成两种语言。
所以方向只在**第一次翻译时**判定一次，之后整个会话锁死不动
（``_resolve_target()`` / ``session_target``）。

**翻译失败绝不静默吞句（BF-11）**：某段失败会**重试一次**；仍失败就**回退成原文**
并入译文（和成功的译文拼在一起），绝不整段丢掉 —— 丢掉等于用户的话凭空消失，
而用户和日志都看不出来。

线程模型：``update()`` 只把累积原文塞进队列立刻返回（**绝不阻塞识别线程**），
真正的 LLM 调用在独立工作线程里做。
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .textutil import smart_join

# 句末标点：中英文都算。逗号/顿号**不算** —— 按逗号切会把语义切碎，译文很跳。
SENTENCE_END = "。！？!?；;…"
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_LATIN = re.compile(r"[A-Za-z]")


def guess_target_language(text: str) -> str:
    """自动判断方向：中文多就译成英文，否则译成中文。"""
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN.findall(text))
    if cjk and cjk >= latin:
        return "English"
    return "Chinese"


def resolve_target_language(configured: str, source: str) -> str:
    """把「设置里的目标语言」解析成本会话**实际使用**的方向。

    * 配置是显式语言（``English`` / ``Chinese`` / ``Japanese`` …）→ 直接用用户的值；
    * 配置是 ``auto``（或空）→ 按 ``source`` 判一次。

    注意：``auto`` 的判定**每个会话只做一次**（见
    :meth:`IncrementalTranslator._resolve_target`）—— 逐段重判会把一句话
    劈成两种语言（BF-10）。
    """
    cfg = (configured or "").strip()
    if cfg and cfg.lower() != "auto":
        return cfg
    return guess_target_language(source)


def last_sentence_end(text: str) -> int:
    """返回最后一个句末标点的下标（含）；没有则 -1。"""
    idx = -1
    for i, ch in enumerate(text):
        if ch in SENTENCE_END:
            idx = i
    return idx


@dataclass
class TranslationResult:
    text: str = ""                 # 最终应当**提交**的文本（译文优先）
    source: str = ""               # 对应原文
    translated: bool = False       # 是否真的拿到了译文（至少一段是模型翻的）
    degraded: bool = False         # 有段落翻译失败 → 已回退原文（不静默丢句）
    calls: int = 0                 # LLM 调用次数（含重试）
    failed: int = 0                # 翻译失败并回退原文的**段数**
    error: str = ""
    target: str = ""               # 本会话锁定的翻译方向（BF-10）
    segments: list[tuple[str, str]] = field(default_factory=list)

    def describe(self) -> str:
        text = ("%s | 段数=%d LLM调用=%d 失败=%d%s"
                % ("已翻译" if self.translated else "降级为原文",
                   len(self.segments), self.calls, self.failed,
                   " | " + self.error if self.error else ""))
        if self.failed:
            # 需求 BF-11.3：日志里必须写明有几段没翻出来、回退成了原文。
            # 不写的话，用户看到的是"少了一句话"，完全不知道发生了什么。
            text += " | %d 段翻译失败，已回退原文" % self.failed
        if self.target:
            text += " | 方向=%s" % self.target
        return text


_SENTINEL = object()


class IncrementalTranslator:
    def __init__(self, llm, target_language: str = "auto",
                 silence_sec: float = 1.5, chunk_chars: int = 40,
                 min_chars: int = 2,
                 on_update: Optional[Callable[[str], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self.llm = llm
        cfg = (target_language or "").strip()
        self.target_language = cfg or "auto"
        self.silence_sec = float(silence_sec)
        self.chunk_chars = int(chunk_chars)
        self.min_chars = int(min_chars)
        self.on_update = on_update or (lambda _t: None)
        self.on_error = on_error or (lambda _t: None)

        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self._segments: list[tuple[str, str]] = []    # (原文, 译文) 已落定
        self._open = ""                               # 尚未落定的原文
        self._full = ""
        self.translation = ""
        self.calls = 0
        self.failed = 0
        self.last_error = ""
        self._lock = threading.Lock()

        # --- BF-10：翻译方向是**会话级**的 --- #
        # 整个会话只判一次方向，判完锁死，之后每一段都用同一个方向。
        # 显式指定（设置里选了「英文」/「中文」…）时不需要文本就能定，
        # 构造时直接锁定；"auto" 时必须等到**第一次翻译**（那时才有原文）
        # 才判一次，之后永不重判。
        self._locked_target: Optional[str] = None
        if self.target_language.lower() != "auto":
            self._locked_target = self.target_language
        self.translated_segments = 0                 # 真正拿到模型译文的段数

    # -- 对外 -------------------------------------------------------------- #

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True, name="translate")
        self._worker.start()

    def update(self, full_text: str) -> None:
        """喂**累积原文**（ASR partial 就是累积全文）。立即返回。"""
        if full_text is None:
            return
        with self._lock:
            if full_text == self._full:
                return
        self._q.put(full_text)

    def finish(self, full_text: str = "") -> TranslationResult:
        """音频结束：把开口段也落定，返回最终要提交的文本。"""
        if full_text:
            self._q.put(full_text)
        self._q.put(_SENTINEL)
        if self._worker is not None:
            self._worker.join(timeout=30)
            self._worker = None
        # worker 已经 join 完，下面读到的都是最终状态。
        with self._lock:
            src = self._full
            trans = self.translation
            segs = list(self._segments)
            target = self._locked_target or ""
            failed = self.failed
            translated_segments = self.translated_segments
        # BF-11.1：只要有段落失败过，degraded 必须是 True。
        # 旧写法是 `not trans.strip() or self.failed > 0 and not trans.strip()`，
        # 运算符优先级让 `failed > 0` 整个失效 —— 有段失败也照样报"没降级"，
        # 于是注入的文本悄悄少了一句话，用户和日志都看不出来。
        degraded = bool(failed) or not trans.strip()
        res = TranslationResult(
            text=trans, source=src,
            translated=translated_segments > 0,
            degraded=degraded,
            calls=self.calls, failed=failed, error=self.last_error,
            segments=segs, target=target)
        if not translated_segments:
            # 一段译文都没拿到 → 提交原文（绝不提交空串）
            res.text = src
            res.degraded = True
        return res

    def stop(self) -> None:
        self._stop.set()
        self._q.put(_SENTINEL)
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None

    @property
    def open_text(self) -> str:
        with self._lock:
            return self._open

    # -- 工作线程 ---------------------------------------------------------- #

    def _run(self) -> None:
        deadline: Optional[float] = None
        while not self._stop.is_set():
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
            try:
                item = self._q.get(timeout=timeout if timeout is not None
                                   else self.silence_sec)
            except queue.Empty:
                item = None

            if item is _SENTINEL:
                self._flush_open()            # 收尾：开口段也落定
                break
            if item is not None:
                self._ingest(str(item))
                if self._close_sentences():
                    pass
                elif len(self._open) >= self.chunk_chars:
                    self._close_open()
                deadline = time.monotonic() + self.silence_sec
                continue
            # 静默到点：把开口段落定（用户停下来了，这段大概率说完了）
            if self._open.strip():
                self._close_open()
            deadline = None

    def _ingest(self, full: str) -> None:
        """用新的累积原文刷新状态，并丢掉被后端改写掉的尾部段。"""
        with self._lock:
            self._full = full
            while self._segments:
                closed = "".join(s for s, _t in self._segments)
                if full.startswith(closed):
                    break
                self._segments.pop()          # 回退：这一段不再可信，重译
            closed = "".join(s for s, _t in self._segments)
            self._open = full[len(closed):] if full.startswith(closed) else full

    def _close_sentences(self) -> bool:
        """把开口段里已经说完的句子落定；返回是否落定了东西。"""
        with self._lock:
            text, open_ = self._open, self._open
        idx = last_sentence_end(open_)
        if idx < 0 or idx + 1 < self.min_chars:
            return False
        closed, rest = open_[:idx + 1], open_[idx + 1:]
        self._translate_and_append(closed)
        with self._lock:
            self._open = rest
        return True

    def _close_open(self) -> None:
        with self._lock:
            text, self._open = self._open, ""
        if text.strip():
            self._translate_and_append(text)

    def _flush_open(self) -> None:
        self._close_open()

    def _translate_and_append(self, source: str) -> None:
        """翻译一段并追加到译文区。

        失败处理（BF-11）：**先重试一次**；仍失败就把这一段**回退成原文**
        并进译文 —— 绝不静默丢掉整句话。丢掉 = 用户的话凭空消失，
        而用户和日志都看不出来（旧实现正是这么干的：段失败就不追加，
        而 ``degraded`` 又因为运算符优先级失效，永远报"没降级"）。
        """
        target = self._resolve_target(source)
        outcome, err = self._call_llm(source, target)
        if outcome is None:
            outcome, err = self._call_llm(source, target)      # 重试一次
        if outcome is None:
            # 重试后仍失败 → 该段回退为原文并入译文
            self.failed += 1
            self.last_error = err
            self._append_segment(source, self._fallback_text(source, err),
                                 translated=False)
            self.on_error("翻译失败（已重试一次），本段回退原文：%s"
                          % (err or "未知错误"))
            return
        self._append_segment(source, outcome.text, translated=True)

    def _fallback_text(self, source: str, err: str) -> str:
        """翻译失败后，这一段用什么文字顶上。

        默认**回退原文**（BF-11.2）：整段丢掉的话，用户说的那句话就凭空
        消失了 —— 而用户和日志都看不出来。回退成原文至少不丢信息。

        单独拆成一个方法，是为了能在测试里**复现旧行为**（返回空串 = 丢弃）
        来做阳性对照：断言必须能抓住这种坏实现，否则它什么都没断言。
        """
        return source

    def _append_segment(self, source: str, text: str, translated: bool) -> None:
        """把一段（原文, 译文）追加进译文区，并通知界面。"""
        with self._lock:
            self._segments.append((source, text))
            if translated:
                self.translated_segments += 1
            # 必须用 smart_join：英文译文直接硬拼会变成 "…afternoon.Discuss…"
            self.translation = smart_join(t for _s, t in self._segments)
            out = self.translation
        self.on_update(out)

    # -- 翻译方向（BF-10） -------------------------------------------------- #

    def _resolve_target(self, source: str) -> str:
        """本会话的翻译方向；**第一次需要翻译时判定一次，之后锁死**。

        BF-10：逐段各判一次方向会把一句话劈成两种语言（前半段译成英文、
        后半段译成中文）。所以整个会话只判一次：

        * 设置里显式指定（``English`` / ``Chinese`` …）→ 直接用用户的值
          （构造时就锁好 —— 不需要文本）；
        * ``auto`` → 在**第一次翻译发生时**判一次（那一刻才有文本可判，
          构造时还没有任何文本），之后所有段落都用这个方向。

        判定样本取**此刻累积到的原文**（``self._full``，至少包含当前这一段），
        而不是只看当前段 —— 当前段可能只有半句话，样本太少会判歪。
        """
        with self._lock:
            if self._locked_target is not None:
                return self._locked_target
            sample = self._full or source
        target = resolve_target_language(self.target_language, sample)
        with self._lock:
            if self._locked_target is None:
                self._locked_target = target
            return self._locked_target

    @property
    def session_target(self) -> Optional[str]:
        """本会话已锁定的翻译方向。

        * 设置里显式指定 → 构造后立刻就是这个值；
        * ``auto`` → 第一次翻译前是 ``None``，翻译一次后固定下来不再变。
        """
        with self._lock:
            return self._locked_target

    # -- 一次 LLM 调用 ------------------------------------------------------ #

    def _call_llm(self, source: str, target: str):
        """调一次 LLM。成功返回 ``(Outcome, "")``，失败返回 ``(None, 错误说明)``。

        这里**不**报 ``on_error``：第一次失败可能马上被重试救回来，
        报错了等于在日志里吓人。真正要报的是"重试后仍失败"，由调用方决定。
        """
        self.calls += 1
        try:
            outcome = self.llm.translate(source, target)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        if outcome.degraded or not outcome.text.strip():
            return None, (outcome.error or "未取得译文（%s）"
                          % (outcome.code or "unknown"))
        return outcome, ""


def default_translator(llm, cfg: Optional[dict] = None, **callbacks):
    cfg = cfg or {}
    return IncrementalTranslator(
        llm,
        target_language=cfg.get("target_language", "auto"),
        silence_sec=cfg.get("silence_sec", 1.5),
        chunk_chars=cfg.get("chunk_chars", 40),
        min_chars=cfg.get("min_chars", 2),
        **callbacks)
