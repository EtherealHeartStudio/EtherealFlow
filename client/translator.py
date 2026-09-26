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
    translated: bool = False       # 是否真的拿到了译文
    degraded: bool = False         # 翻译全失败 → 回退原文
    calls: int = 0
    failed: int = 0
    error: str = ""
    segments: list[tuple[str, str]] = field(default_factory=list)

    def describe(self) -> str:
        return ("%s | 段数=%d LLM调用=%d 失败=%d%s"
                % ("已翻译" if self.translated else "降级为原文",
                   len(self.segments), self.calls, self.failed,
                   " | " + self.error if self.error else ""))


_SENTINEL = object()


class IncrementalTranslator:
    def __init__(self, llm, target_language: str = "auto",
                 silence_sec: float = 1.5, chunk_chars: int = 40,
                 min_chars: int = 2,
                 on_update: Optional[Callable[[str], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self.llm = llm
        self.target_language = target_language or "auto"
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
        src = self._full
        with self._lock:
            trans, segs = self.translation, list(self._segments)
        res = TranslationResult(
            text=trans, source=src, translated=bool(trans.strip()),
            degraded=not trans.strip() or self.failed > 0 and not trans.strip(),
            calls=self.calls, failed=self.failed, error=self.last_error, segments=segs)
        if not trans.strip():
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
        target = self.target_language
        if target == "auto":
            target = guess_target_language(source)
        self.calls += 1
        try:
            outcome = self.llm.translate(source, target)
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            self.last_error = str(exc)
            self.on_error("翻译失败：%s" % exc)
            return
        if outcome.degraded or not outcome.text.strip():
            self.failed += 1
            self.last_error = outcome.error or "未取得译文"
            self.on_error("翻译未成功（%s）：%s" % (outcome.code, outcome.error))
            return
        with self._lock:
            self._segments.append((source, outcome.text))
            self.translation = "".join(t for _s, t in self._segments)
            text = self.translation
        self.on_update(text)


def default_translator(llm, cfg: Optional[dict] = None, **callbacks):
    cfg = cfg or {}
    return IncrementalTranslator(
        llm,
        target_language=cfg.get("target_language", "auto"),
        silence_sec=cfg.get("silence_sec", 1.5),
        chunk_chars=cfg.get("chunk_chars", 40),
        min_chars=cfg.get("min_chars", 2),
        **callbacks)
