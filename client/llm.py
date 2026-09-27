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

from .config import DEFAULT_LLM_MAX_TOKENS, DEFAULT_LLM_TIMEOUT
from .correction_modes import (BUILTIN_MODES, DEFAULT_MODE_ID,  # noqa: F401
                               effective_prompt, resolve_mode,
                               strip_trailing_period)

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


class LlmTruncatedError(LlmError):
    """模型的思考过程吃光了 max_tokens，根本没给出结果。

    这是接真模型时踩到的：``deepseek-flash`` 这类**推理模型**会先把 ``max_tokens``
    花在内部思考上，预算不够时 ``content`` 是空的、只有 ``reasoning_content``，
    ``finish_reason`` 是 ``length``。

    如果这时把思考过程当结果交出去，下游只会报「输出长度失控（2460 字 vs 原文 25 字）」
    —— 用户完全想不到要去调大 ``max_tokens`` 或换个模型。所以这里单独分类，
    把话说明白。

    ``degrade=False``：这是**用户必须处理**的配置问题（模型选错了，或预算给少了），
    跟"模型太慢"不是一回事。若按静默降级处理，修正会一声不响地永远失效。
    """
    degrade = False
    code = "truncated"


# --------------------------------------------------------------------------- #
# Prompt（需求文档 §7.2 + 末尾硬约束）
# --------------------------------------------------------------------------- #

# 修正 Prompt 现在由**模式**决定（见 client/correction_modes.py）。
# 这里保留 CORRECT_PROMPT 只是为了让「直接 new 一个 LlmClient」也有个合理默认值，
# 它**就是**「轻度整理」模式的 Prompt —— 写两份会漂移，所以做成别名。
CORRECT_PROMPT = next(m["prompt"] for m in BUILTIN_MODES if m["id"] == DEFAULT_MODE_ID)

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

# --------------------------------------------------------------------------- #
# 常用预设
# --------------------------------------------------------------------------- #

# 用户不该为了「接一个模型」去翻各家的文档找地址。这里按「本地优先」排列，
# 因为本项目主打语音不出机器，本地模型才是首选。
#
# ``api_key: ""`` 是有意的**清空**（本机服务不校验 Key，留着反而可能被拒）；
# 云端预设**不带** api_key 键 —— 表示「保留用户已填的 Key」，只换地址和模型名，
# 免得用户选一下预设就把辛苦贴进去的 Key 弄丢了。
PRESETS: list[tuple[str, dict[str, str]]] = [
    ("本地 · Ollama", {"base_url": "http://localhost:11434/v1",
                       "model": "qwen2.5:7b", "api_key": ""}),
    ("本地 · LM Studio", {"base_url": "http://localhost:1234/v1",
                          "model": "", "api_key": ""}),
    ("本地 · llama.cpp server", {"base_url": "http://localhost:8080/v1",
                                 "model": "", "api_key": ""}),
    ("本地 · vLLM", {"base_url": "http://localhost:8000/v1",
                     "model": "", "api_key": ""}),
    ("本地 · CLIProxy 等代理（8317）", {"base_url": "http://127.0.0.1:8317/v1",
                                        "model": "", "api_key": ""}),
    ("云端 · OpenAI", {"base_url": "https://api.openai.com/v1",
                       "model": "gpt-4o-mini"}),
    ("云端 · DeepSeek", {"base_url": "https://api.deepseek.com/v1",
                         "model": "deepseek-chat"}),
    ("云端 · 月之暗面 Kimi", {"base_url": "https://api.moonshot.cn/v1",
                              "model": "moonshot-v1-8k"}),
    ("云端 · 智谱 GLM", {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                         "model": "glm-4-flash"}),
    ("云端 · 阿里通义（兼容模式）",
     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "model": "qwen-plus"}),
    ("云端 · 硅基流动 SiliconFlow", {"base_url": "https://api.siliconflow.cn/v1",
                                     "model": "Qwen/Qwen2.5-7B-Instruct"}),
]


def apply_preset(cfg: dict, preset: dict[str, str]) -> dict:
    """把预设套到配置上。**不带 api_key 的预设不会清掉用户已填的 Key。**"""
    for key in ("base_url", "model", "api_key"):
        if key in preset:
            cfg[key] = preset[key]
    return cfg


def normalize_base_url(url: str) -> str:
    """把用户填的地址收拾成「能直接拼 /chat/completions」的形式。

    这个函数是**真机联调逼出来的**：让用户手填一个地址，他一定会填出各种写法，
    而每一种都得能用，否则「配好了却连不上」。要能吃下的写法：

        http://localhost:11434                     → …/v1   （Ollama 的根地址）
        http://localhost:1234/v1/                  → …/v1   （多余尾斜杠）
        http://localhost:8080/v1/chat/completions  → …/v1   （直接粘了完整端点）
        127.0.0.1:8317/v1                          → http://127.0.0.1:8317/v1（补协议）
        http://host:8000/api                       → 原样    （非标准路径不要乱补）
    """
    text = (url or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
            break
    if "://" not in text:
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    # 只有路径为空或就是根时才补 /v1 —— 用户自己写了 /api 之类就别自作主张
    if parts.path in ("", "/"):
        text = text.rstrip("/") + "/v1"
    return text


@dataclass
class Completion:
    text: str = ""
    model: str = ""
    elapsed_ms: float = 0.0
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""


@dataclass
class Outcome:
    """一次修正/翻译的最终结论。"""
    text: str                     # 最终应当注入的文本
    changed: bool = False         # 是否真的用了模型结果
    degraded: bool = False        # 是否降级为原文
    notify: bool = False          # 是否应该提示用户（配置类问题）
    no_change: bool = False       # 模型成功了，但判定这句不需要改
    error: str = ""
    code: str = ""
    elapsed_ms: float = 0.0
    mode: str = ""                # 用的哪个修正模式（仅修正有）

    def describe(self) -> str:
        if self.changed:
            return "已修正（%.0f ms）" % self.elapsed_ms
        if self.no_change:
            # 和"降级"是两件事：模型的答案是"这句不用改"，不是"模型没用上"。
            # 不区分的话，选「不整理」时用户会看到满屏"降级"，以为配置坏了。
            return "无需改动（%.0f ms）" % self.elapsed_ms
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
                 timeout: float = DEFAULT_LLM_TIMEOUT, max_tokens: int = DEFAULT_LLM_MAX_TOKENS,
                 correct_prompt: str = CORRECT_PROMPT,
                 translate_prompt: str = TRANSLATE_PROMPT,
                 strip_trailing_period: bool = False,
                 mode_name: str = "") -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.correct_prompt = correct_prompt
        self.translate_prompt = translate_prompt
        self.strip_trailing_period = bool(strip_trailing_period)
        self.mode_name = mode_name or ""
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
            finish = str(choice.get("finish_reason") or "")
            text = message.get("content") or ""
            if not text:
                # 有些模型把内容放在 reasoning_content。但**预算被思考吃光**时
                # 那里装的是自言自语，绝不能当成结果 —— 否则注入到用户输入框里的
                # 会是一段"我们需要回答用户…"。这种情况单独报错，把原因说清楚。
                reasoning = message.get("reasoning_content") or ""
                if reasoning and finish == "length":
                    raise LlmTruncatedError(
                        "模型的思考过程用完了 max_tokens（%d），没能给出结果。"
                        "把 max_tokens 调大，或换一个非推理模型。"
                        % self.max_tokens)
                text = reasoning
        except (AttributeError, IndexError, TypeError) as exc:
            raise LlmServerError("返回结构不符合 OpenAI 规范") from exc

        return Completion(text=text, model=payload.get("model", self.model),
                          elapsed_ms=elapsed, usage=payload.get("usage") or {},
                          finish_reason=finish)

    def list_models(self) -> list[str]:
        """列出服务端可用模型（OpenAI 兼容 ``GET /v1/models``）。

        作用是双向的：

        1. **省得用户记模型名**。本地部署的名字（``qwen2.5:7b``、
           ``llama-3.2-3b-instruct``）没人记得住，设置界面直接拉下来给用户选。
        2. 它同时是**最轻量的连通性检查** —— 能列出模型就说明地址和 Key 都对，
           比发一次真实的补全请求便宜得多，适合做成"测试连接"按钮。
        """
        url = self.base_url + "/models"
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, headers=headers, method="GET")
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
            if not self._probe_reachable():
                raise LlmConnectionError(
                    "连不上 %s（TCP 不可达，服务可能没启动）" % self.base_url) from exc
            raise LlmTimeoutError("列出模型超时（>%.0fs）" % self.timeout) from exc
        except urllib.error.URLError as exc:
            raise LlmConnectionError("连不上 %s：%s"
                                     % (self.base_url, getattr(exc, "reason", exc))) from exc
        except json.JSONDecodeError as exc:
            raise LlmServerError("返回的不是合法 JSON（该地址可能不是 OpenAI 兼容接口）"
                                 ) from exc

        data = payload.get("data")
        if not isinstance(data, list):
            data = payload.get("models") or []      # 少数实现用 models
        ids: list[str] = []
        for item in data:
            mid = item.get("id") or item.get("name") if isinstance(item, dict) else str(item)
            if mid:
                ids.append(str(mid))
        return sorted(ids)

    def test_connection(self) -> tuple[bool, str]:
        """给设置界面的「测试连接」用：返回 ``(是否通, 给人看的说明)``。

        这里把 ``LlmError`` 的分类**原样保留**成文字，因为分类本身就是给用户看的
        诊断结论：Key 错了和模型太慢是两件完全不同的事，不能都显示"连接失败"。
        """
        try:
            models = self.list_models()
        except LlmError as exc:
            return False, "%s（%s）" % (exc, exc.code)
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)
        if not models:
            return True, "连通，但服务端没有报告任何模型（可手动填模型名）"
        if self.model and self.model not in models:
            return True, ("连通，共 %d 个模型；但没找到「%s」，请确认名字拼写"
                          % (len(models), self.model))
        return True, "连通，共 %d 个模型%s" % (
            len(models), "，已找到「%s」" % self.model if self.model else "")

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
        # degraded 与 no_change 都置位是**有意**的：
        #   * translator 靠 degraded 判断"这段没译出来"（原样返回等于没翻），语义不能变；
        #   * 修正这边靠 no_change 区分"模型说不用改"和"模型没用上"，日志才不误导。
        return Outcome(text=text, changed=changed,
                       degraded=not changed, no_change=not changed,
                       elapsed_ms=comp.elapsed_ms, mode=self.mode_name)

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
        outcome = self._run(prompt + PLAIN_TEXT_SUFFIX, text, hotwords)
        if self.strip_trailing_period and outcome.text.strip():
            stripped = strip_trailing_period(outcome.text)
            if stripped != outcome.text:
                outcome.text = stripped
                outcome.changed = stripped.strip() != (text or "").strip()
                outcome.degraded = not outcome.changed
                outcome.no_change = not outcome.changed
        return outcome

    def translate(self, text: str, target_language: str = "English") -> Outcome:
        if not text.strip():
            return Outcome(text=text, degraded=True, error="原文为空", code="empty")
        prompt = self._build_prompt(self.translate_prompt, None,
                                    target_language=target_language, text=text)
        return self._run(prompt + PLAIN_TEXT_SUFFIX, text)


def default_client(cfg: Optional[dict] = None) -> LlmClient:
    """按配置组装客户端。修正 Prompt 由**当前模式**决定（内置或用户自定义）。"""
    cfg = cfg or {}
    mode = resolve_mode(cfg)
    return LlmClient(base_url=cfg.get("base_url", "http://127.0.0.1:8317/v1"),
                     api_key=cfg.get("api_key", ""),
                     model=cfg.get("model", ""),
                     timeout=cfg.get("timeout", DEFAULT_LLM_TIMEOUT),
                     max_tokens=cfg.get("max_tokens", DEFAULT_LLM_MAX_TOKENS),
                     correct_prompt=effective_prompt(cfg, mode),
                     translate_prompt=cfg.get("translate_prompt") or TRANSLATE_PROMPT,
                     strip_trailing_period=bool(cfg.get("strip_trailing_period")),
                     mode_name=mode["name"])
