# -*- coding: utf-8 -*-
"""EtherealFlow · LLM 修正 / 翻译（FR-4 / FR-5 的后端部分）。

OpenAI 兼容 ``/v1/chat/completions``，只用标准库（``urllib``），零第三方依赖。

三件事是从竞品源码里学来的，这里都落实了：

**1. 失败必须分类**（CapsWriter-Offline 的 ``llm_error_handler.py``）
    认证 / 连接 / 请求格式错误 → **不降级**，提示用户去改配置
    超时 / 限流 / 服务端 5xx     → **静默降级**，直接注入识别原文
  需求文档 FR-4 也要求"LLM 不可用时直接用原文注入"，但"不可用"要看是哪种不可用 ——
  把 Key 写错和模型太慢当成同一回事，用户永远不知道自己配错了。

**2. 输出必须校验**（openwhispr 的 ``cleanupOutput.ts``）
    模型有时会把输入整段复读一遍、或把热词表吐回来、或加一堆 Markdown。
    这些**都不能注入到用户的输入框里**。校验不过就退回原文。

**3. 语言指令放最后**（openwhispr 的注释：模型对**末尾**指令最敏感）
    所以"只输出纯文本"这类硬约束追加在 prompt 末尾，而不是开头。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 异常分类
# --------------------------------------------------------------------------- #

class LlmError(Exception):
    """基类。``degrade=True`` 表示可以安全降级为原文。"""
    degrade = True
    code = "error"


class LlmAuthError(LlmError):
    """API Key / 鉴权问题 —— 用户必须去改配置，不能悄悄降级。"""
    degrade = False
    code = "auth"


class LlmConnectionError(LlmError):
    """连不上服务 —— 同样是配置/服务问题，要告诉用户。"""
    degrade = False
    code = "connection"


class LlmBadRequestError(LlmError):
    """请求格式错（如模型名不存在）—— 要告诉用户。"""
    degrade = False
    code = "bad_request"


class LlmTimeoutError(LlmError):
    degrade = True
    code = "timeout"


class LlmRateLimitError(LlmError):
    degrade = True
    code = "rate_limit"


class LlmServerError(LlmError):
    degrade = True
    code = "server"


class LlmBadOutputError(LlmError):
    """模型输出没通过校验 —— 退回原文。"""
    degrade = True
    code = "bad_output"


# --------------------------------------------------------------------------- #
# Prompt（需求文档 §7.2 + 末尾硬约束）
# --------------------------------------------------------------------------- #

CORRECT_PROMPT = """你是一个文本修正助手。请修正下面这段语音识别结果：
1. 去除"嗯、啊、那个、就是"等口水词
2. 补充正确的标点符号
3. 修正明显的同音错别字
4. 数字用阿拉伯数字
5. 保持原意，不要增删内容，不要改写语气

直接输出修正后的文本，不要任何解释。

{hotwords}原文：
{text}"""

TRANSLATE_PROMPT = """将下面的文本翻译成{target_language}。只输出译文，不要任何解释。

{hotwords}原文：
{text}"""

# openwhispr 的经验：模型对**末尾**指令最敏感，所以硬约束放最后
PLAIN_TEXT_SUFFIX = ("\n\n只输出纯文本本身：不要 Markdown、不要代码块、不要引号包裹、"
                     "不要任何解释、前缀或后缀。不要重复原文。")

# 热词必须放在"原文："**之前**。放在原文后面时，模型会把它当成正文的一部分，
# 甚至直接把热词表吐回来当结果（这是实测踩到的）。
HOTWORD_LINE = ("参考热词（仅用于纠正同音错别字，**不要把它们当作要输出的内容**）：{words}\n\n")


# --------------------------------------------------------------------------- #
# 结果与校验
# --------------------------------------------------------------------------- #

@dataclass
class Completion:
    text: str = ""
    model: str = ""
    elapsed_ms: float = 0.0
    usage: dict = field(default_factory=dict)


@dataclass
class Outcome:
    """一次修正/翻译的最终结论。"""
    text: str                     # 最终应当注入的文本
    changed: bool = False         # 是否真的用了模型结果
    degraded: bool = False        # 是否降级为原文
    notify: bool = False          # 是否应该提示用户（配置类问题）
    error: str = ""
    code: str = ""
    elapsed_ms: float = 0.0

    def describe(self) -> str:
        if self.changed:
            return "已修正（%.0f ms）" % self.elapsed_ms
        if self.degraded:
            return "降级为原文（%s，%.0f ms）" % (self.code or "error", self.elapsed_ms)
        return "未改动"


_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?|\n?\s*```\s*$")
_PREFIX = re.compile(r"^\s*(修正后(的文本)?|译文|翻译结果|结果|输出)\s*[:：]\s*")


def clean_model_text(raw: str) -> str:
    """把模型爱加的那些壳剥掉。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if _FENCE.search(text):
        text = _FENCE.sub("", text).strip()
    text = _PREFIX.sub("", text)
    # 整段被引号包住 → 去掉
    if len(text) >= 2 and text[0] in "\"“'‘" and text[-1] in "\"”'’":
        text = text[1:-1].strip()
    return text


def validate_output(original: str, output: str,
                    hotwords: Optional[list[str]] = None) -> str:
    """校验模型输出；不合格抛 ``LlmBadOutputError``。

    检查项（都来自真实踩过的坑）：
      * 空输出
      * **复读**：输出 ≈ 输入的两遍（openwhispr 的 CLEANUP_OUTPUT_INVALID）
      * **热词回声**：输出基本就是热词表（openwhispr 的 DICTIONARY_ECHO）
      * 长度失控：修正是"清理"，不该把文本变长好几倍
    """
    text = clean_model_text(output)
    if not text:
        raise LlmBadOutputError("模型返回空文本")

    src = (original or "").strip()
    norm = lambda s: re.sub(r"\s+", "", s)          # noqa: E731
    n_src, n_out = norm(src), norm(text)

    if n_src and n_out == n_src:
        # 原样返回：不算错，但也没修正 → 让调用方按"未改动"处理
        return src

    if n_src and len(n_src) >= 4:
        doubled = n_out in (n_src * 2, norm(src + src))
        if doubled or (n_out.startswith(n_src) and n_out[len(n_src):] == n_src):
            raise LlmBadOutputError("模型把原文复读了两遍")

    if hotwords:
        # 去掉分隔符再比，否则 "A、B、C" 与 "A,B,C" 匹配不上（实测踩到）
        only = lambda s: re.sub(r"[^\w]", "", s)   # noqa: E731
        n_only = only(n_out)
        joined = only("".join(hotwords) + ",".join(hotwords))
        if joined and n_only and len(n_only) <= len(joined) + 8 and n_only in joined:
            raise LlmBadOutputError("模型把热词表当成结果吐了回来")

    limit = max(len(n_src) * 3, len(n_src) + 120)
    if n_src and len(n_out) > limit:
        raise LlmBadOutputError("输出长度失控（%d 字 vs 原文 %d 字）"
                                % (len(n_out), len(n_src)))
    return text


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class LlmClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8317/v1",
                 api_key: str = "", model: str = "",
                 timeout: float = 8.0, max_tokens: int = 1024,
                 correct_prompt: str = CORRECT_PROMPT,
                 translate_prompt: str = TRANSLATE_PROMPT) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.correct_prompt = correct_prompt
        self.translate_prompt = translate_prompt
        self.last_error = ""

    # -- 底层 -------------------------------------------------------------- #

    def complete(self, user_prompt: str, system: str = "") -> Completion:
        url = self.base_url + "/chat/completions"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        body: dict[str, Any] = {"messages": messages, "temperature": 0.0,
                                "max_tokens": self.max_tokens, "stream": False}
        if self.model:
            body["model"] = self.model

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise self._classify_http(exc.code, detail) from exc
        except (socket.timeout, TimeoutError) as exc:
            # 超时有两种截然不同的原因，必须分开：
            #   "服务没起来" → 要提示用户；"模型太慢" → 静默降级
            # 某些环境下连一个没人监听的端口也是**超时**而不是拒绝连接，
            # 所以不能只看异常类型，得再探一次 TCP 可达性。
            if not self._probe_reachable():
                raise LlmConnectionError(
                    "连不上 %s（TCP 不可达，服务可能没启动）" % self.base_url) from exc
            raise LlmTimeoutError("请求超时（>%.0fs）" % self.timeout) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                if not self._probe_reachable():
                    raise LlmConnectionError(
                        "连不上 %s（TCP 不可达）" % self.base_url) from exc
                raise LlmTimeoutError("请求超时（>%.0fs）" % self.timeout) from exc
            raise LlmConnectionError("连不上 %s：%s" % (self.base_url, reason)) from exc
        except json.JSONDecodeError as exc:
            raise LlmServerError("返回的不是合法 JSON") from exc

        elapsed = (time.monotonic() - t0) * 1000.0
        try:
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            if not text:
                # 有些模型把内容放在 reasoning_content（OpenTypeless 的兜底）
                text = message.get("reasoning_content") or ""
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmServerError("返回结构不符合 OpenAI 规范") from exc

        return Completion(text=text, model=payload.get("model", self.model),
                          elapsed_ms=elapsed, usage=payload.get("usage") or {})

    def _probe_reachable(self, timeout: float = 1.0) -> bool:
        """快速探一下 host:port 的 TCP 可达性，用来区分"服务没起来"和"模型太慢"。"""
        parts = urllib.parse.urlsplit(self.base_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _classify_http(status: int, detail: str) -> LlmError:
        if status in (401, 403):
            return LlmAuthError("鉴权失败（HTTP %d）：API Key 无效或缺失。%s"
                                % (status, detail))
        if status == 429:
            return LlmRateLimitError("触发限流（HTTP 429）")
        if status == 404:
            return LlmBadRequestError("接口不存在（HTTP 404）：检查 LLM 地址。%s" % detail)
        if 400 <= status < 500:
            return LlmBadRequestError("请求被拒（HTTP %d）：%s" % (status, detail))
        return LlmServerError("服务端错误（HTTP %d）" % status)

    # -- 业务 -------------------------------------------------------------- #

    def _run(self, prompt: str, original: str,
             hotwords: Optional[list[str]] = None) -> Outcome:
        try:
            comp = self.complete(prompt)
        except LlmError as exc:
            self.last_error = str(exc)
            return Outcome(text=original, degraded=True, notify=not exc.degrade,
                           error=str(exc), code=exc.code)
        try:
            text = validate_output(original, comp.text, hotwords)
        except LlmBadOutputError as exc:
            self.last_error = str(exc)
            return Outcome(text=original, degraded=True, error=str(exc),
                           code=exc.code, elapsed_ms=comp.elapsed_ms)
        changed = text.strip() != (original or "").strip()
        return Outcome(text=text, changed=changed,
                       degraded=not changed, elapsed_ms=comp.elapsed_ms)

    def _build_prompt(self, template: str, hotwords: Optional[list[str]],
                      **kwargs: Any) -> str:
        """填模板并把热词插到「原文：」**之前**。

        自定义模板（设置界面里可改）可能没有 ``{hotwords}`` 占位符，
        所以这里做了兼容，不会因为用户改了模板就崩。
        """
        words = ""
        if hotwords:
            words = HOTWORD_LINE.format(words="、".join(hotwords[:60]))
        try:
            if "{hotwords}" in template:
                return template.format(hotwords=words, **kwargs)
            body = template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            body = template                      # 用户模板占位符写错也不该崩
            if "{text}" not in template and kwargs.get("text"):
                body = template + "\n\n" + str(kwargs["text"])
        if words:
            marker = "原文："
            idx = body.find(marker)
            if idx >= 0:
                return body[:idx] + words + body[idx:]
            return body.rstrip() + "\n\n" + words.strip()
        return body

    def correct(self, text: str, hotwords: Optional[list[str]] = None) -> Outcome:
        if not text.strip():
            return Outcome(text=text, degraded=True, error="原文为空", code="empty")
        prompt = self._build_prompt(self.correct_prompt, hotwords, text=text)
        return self._run(prompt + PLAIN_TEXT_SUFFIX, text, hotwords)

    def translate(self, text: str, target_language: str = "English") -> Outcome:
        if not text.strip():
            return Outcome(text=text, degraded=True, error="原文为空", code="empty")
        prompt = self._build_prompt(self.translate_prompt, None,
                                    target_language=target_language, text=text)
        return self._run(prompt + PLAIN_TEXT_SUFFIX, text)


def default_client(cfg: Optional[dict] = None) -> LlmClient:
    cfg = cfg or {}
    return LlmClient(base_url=cfg.get("base_url", "http://127.0.0.1:8317/v1"),
                     api_key=cfg.get("api_key", ""),
                     model=cfg.get("model", ""),
                     timeout=cfg.get("timeout", 8.0),
                     max_tokens=cfg.get("max_tokens", 1024),
                     correct_prompt=cfg.get("prompt") or CORRECT_PROMPT,
                     translate_prompt=cfg.get("translate_prompt") or TRANSLATE_PROMPT)
