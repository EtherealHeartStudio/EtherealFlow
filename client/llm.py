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

import http.client
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


class LlmConfigError(LlmError):
    """**地址本身写错了**（格式非法、端口不是数字…）—— 必须告诉用户去改。

    为什么单独一类：它和"连不上"是两回事。

    * 连不上 → 服务可能只是没启动，用户重试一下就好；
    * 地址写错 → 用户改了才有用，报"连接失败"会让人一直查网络、查防火墙。

    ``http://host:abc/v1`` 这种畸形地址在 ``urllib`` 内部抛的是
    ``http.client.InvalidURL`` —— 它**既不是** ``URLError`` **也不是** ``OSError``，
    所以以前会一路逃出 ``correct()``，把发起修正的线程打死
    （实测：界面永远停在"修正中"，用户的话白说了）。
    """
    degrade = False
    code = "config"


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

    ⚠️ 本函数**绝不抛异常**：``urlsplit`` 遇到畸形 IPv6 会抛 ``ValueError``，
    而它是在 ``LlmClient.__init__`` 里被调用的 —— 那里抛出去会一路炸到
    ``default_client()``，程序根本起不来。所以解析不了就原样返回，
    交给 :func:`check_base_url` 去报错。
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
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return text          # 解析不了 → 原样留着，check_base_url 会给出人话提示
    # 只有路径为空或就是根时才补 /v1 —— 用户自己写了 /api 之类就别自作主张
    if parts.path in ("", "/"):
        text = text.rstrip("/") + "/v1"
    return text


def check_base_url(base_url: str) -> Optional[str]:
    """发请求**之前**检查地址格式。

    返回 ``None`` = 没问题；返回一段**给用户看的中文说明** = 地址有问题。

    为什么必须有这一步（BF-7）：``http://host:abc/v1`` 这种畸形地址在
    ``http.client`` 里抛的是 ``InvalidURL`` —— 它**既不是** ``URLError``
    **也不是** ``OSError**，以前会一路逃出 ``correct()`` 打死发起修正的线程
    （实测：界面永远停在「修正中」，用户的话白说了）。

    而且光靠"捕获"不够：把它归成「连接失败」是**误导** —— 用户会去查网络、
    查防火墙，而真正要改的是地址里的那个端口。���以这里给出的是
    「哪个地址不对 + 怎么改」。
    """
    text = (base_url or "").strip()
    if not text:
        return ("LLM 地址是空的。请在「设置 → LLM」里填上接口地址，"
                "例如 http://127.0.0.1:8317/v1")
    if any(ch.isspace() or ord(ch) < 0x21 for ch in text):
        return ("LLM 地址「%s」里有空格或控制字符。"
                "地址里不能有空格，正确格式形如 http://127.0.0.1:8317/v1" % text)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        return ("LLM 地址「%s」无法解析（%s）。请检查有没有多余的括号或字符，"
                "正确格式形如 http://127.0.0.1:8317/v1" % (text, exc))
    if parts.scheme not in ("http", "https"):
        return ("LLM 地址「%s」的协议不对。只支持 http:// 或 https://，"
                "正确格式形如 http://127.0.0.1:8317/v1" % text)
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        # 端口不是数字 / 超出 0-65535 —— ``urlsplit`` 是**惰性**报这个错的，
        # 不主动取 ``.port`` 就永远发现不了。
        return ("LLM 地址「%s」的端口不合法（%s）。端口必须是 1-65535 的数字，"
                "正确格式形如 http://127.0.0.1:8317/v1" % (text, exc))
    if not host:
        return ("LLM 地址「%s」里没有主机名。"
                "正确格式形如 http://127.0.0.1:8317/v1" % text)
    if port is not None and not (1 <= port <= 65535):
        return ("LLM 地址「%s」的端口 %d 超出范围。端口必须是 1-65535 的数字，"
                "正确格式形如 http://127.0.0.1:8317/v1" % (text, port))
    return None


@dataclass
class Completion:
    text: str = ""
    model: str = ""
    elapsed_ms: float = 0.0
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""
    #: 失败时的说明。**空串 = 成功**。
    #: ``complete()`` 不会抛异常（见那里的说明），失败全靠这三个字段汇报。
    error: str = ""
    code: str = ""                # 失败分类（auth / connection / config / timeout …）
    notify: bool = False          # 是否要提示用户（配置类问题 = True）


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

    @staticmethod
    def _error_completion(err: LlmError, elapsed_ms: float = 0.0) -> Completion:
        """把一次失败包装成 ``Completion``。

        ``complete()`` **不抛异常**（BF-7），失败全靠返回值里的
        ``error`` / ``code`` / ``notify`` 汇报。理由见 :class:`LlmConfigError`：
        以前一个畸形地址抛的 ``http.client.InvalidURL`` 会一路逃出去，
        把发起修正的线程打死（界面永远停在"修正中"，用户的话白说了）。
        """
        return Completion(error=str(err), code=err.code,
                          notify=not err.degrade, elapsed_ms=elapsed_ms)

    def complete(self, user_prompt: str, system: str = "") -> Completion:
        """发一次补全请求。**绝不抛异常**。

        失败时返回一个 ``error`` 非空的 ``Completion``，``code`` 标明失败分类
        （auth / connection / config / timeout / …），``notify`` 标明要不要提示用户。

        这样调用方（``correct()`` / ``translate()`` / ``test_connection()``）
        不管遇到什么怪情况都拿得到一个**结构化**的结果，不会被一个
        没预料到的异常掀翻。
        """
        # 地址先查格式。畸形地址（如 http://host:abc/v1）在 http.client 里
        # 抛的是 InvalidURL —— 归类成「配置错误」并给出能看懂的提示，
        # 而不是含糊的「连接失败」（用户改一下端口就行了，不该去查网络）。
        problem = check_base_url(self.base_url)
        if problem:
            return self._error_completion(LlmConfigError(problem))

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
            return self._error_completion(self._classify_http(exc.code, detail))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                if not self._probe_reachable():
                    return self._error_completion(
                        LlmConnectionError("连不上 %s（TCP 不可达）" % self.base_url))
                return self._error_completion(
                    LlmTimeoutError("请求超时（>%.0fs）" % self.timeout))
            return self._error_completion(
                LlmConnectionError("连不上 %s：%s" % (self.base_url, reason)))
        except (socket.timeout, TimeoutError):
            # 超时有两种截然不同的原因，必须分开：
            #   "服务没起来" → 要提示用户；"模型太慢" → 静默降级
            # 某些环境下连一个没人监听的端口也是**超时**而不是拒绝连接，
            # 所以不能只看异常类型，得再探一次 TCP 可达性。
            if not self._probe_reachable():
                return self._error_completion(LlmConnectionError(
                    "连不上 %s（TCP 不可达，服务可能没启动）" % self.base_url))
            return self._error_completion(
                LlmTimeoutError("请求超时（>%.0fs）" % self.timeout))
        except http.client.InvalidURL as exc:
            # 地址里有 http.client 不认的东西（控制字符、畸形 IPv6…）
            return self._error_completion(
                LlmConfigError("LLM 地址无法使用：%s。地址里不能有空格或特殊字符，"
                               "正确格式形如 http://127.0.0.1:8317/v1" % exc))
        except http.client.HTTPException as exc:
            # 响应层面的协议错误（连接被掐断、状态行不合法…）
            return self._error_completion(
                LlmConnectionError("与 %s 的连接异常：%s" % (self.base_url, exc)))
        except json.JSONDecodeError:
            return self._error_completion(LlmServerError("返回的不是合法 JSON"))
        except ValueError as exc:
            # urllib 对地址/参数的非预期用法（比如端口不是数字）
            return self._error_completion(
                LlmConfigError("请求地址或参数不合法：%s" % exc))
        except OSError as exc:
            return self._error_completion(
                LlmConnectionError("连不上 %s：%s" % (self.base_url, exc)))
        except Exception as exc:  # noqa: BLE001
            # 最后一道兜底：**任何**异常都不许逃出去（BF-7）。
            return self._error_completion(
                LlmServerError("调用 LLM 时出现未预期的错误：%r" % (exc,)))

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
        except LlmTruncatedError as exc:
            return self._error_completion(exc, elapsed)
        except (AttributeError, IndexError, TypeError):
            return self._error_completion(
                LlmServerError("返回结构不符合 OpenAI 规范"), elapsed)

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

        和 ``complete()`` 不同，本方法**仍然抛异常** —— 但只抛 ``LlmError``
        的子类（畸形地址归类成 ``LlmConfigError``），不抛任何没预料到的类型。
        """
        problem = check_base_url(self.base_url)
        if problem:
            raise LlmConfigError(problem)

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
        except http.client.InvalidURL as exc:
            raise LlmConfigError("LLM 地址无法使用：%s。地址里不能有空格或特殊字符，"
                                 "正确格式形如 http://127.0.0.1:8317/v1" % exc) from exc
        except http.client.HTTPException as exc:
            raise LlmConnectionError("与 %s 的连接异常：%s" % (self.base_url, exc)) from exc
        except json.JSONDecodeError as exc:
            raise LlmServerError("返回的不是合法 JSON（该地址可能不是 OpenAI 兼容接口）"
                                 ) from exc
        except ValueError as exc:
            raise LlmConfigError("请求地址或参数不合法：%s" % exc) from exc
        except OSError as exc:
            raise LlmConnectionError("连不上 %s：%s" % (self.base_url, exc)) from exc

        data = payload.get("data")
        if not isinstance(data, list):
            data = payload.get("models") or []      # 少数实现用 models
        ids: list[str] = []
        for item in data:
            mid = item.get("id") or item.get("name") if isinstance(item, dict) else str(item)
            if mid:
                ids.append(str(mid))
        return sorted(ids)

    def test_connection(self, probe_call: bool = True) -> tuple[bool, str]:
        """给设置界面的「测试连接」用：返回 ``(是否通, 给人看的说明)``。

        这里把 ``LlmError`` 的分类**原样保留**成文字，因为分类本身就是给用户看的
        诊断结论：Key 错了和模型太慢是两件完全不同的事，不能都显示"连接失败"。

        ``probe_call=True`` 时会**真的发一次最小请求**。
        ⚠️ 为什么不能只查模型列表：列表里**有**这个模型 ≠ 这个模型**能用**。
        实测踩过：代理端把 ``gpt-5.6-sol`` 列在 ``/v1/models`` 里，
        但实际调用返回 ``usage_limit_reached``（配额用尽）——
        只查列表的话，设置界面显示「✓ 连通」，用户一用就失败。
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

        if probe_call and self.model:
            # 真发一次最小请求，确认这个模型**真的能用**
            saved_max = self.max_tokens
            try:
                self.max_tokens = 8          # 探测别浪费配额
                comp = self.complete("hi")
            except Exception as exc:  # noqa: BLE001
                return False, "列表里有「%s」，但实际调用失败：%r" % (self.model, exc)
            finally:
                self.max_tokens = saved_max
            if comp.error:
                return False, "列表里有「%s」，但实际调用失败：%s（%s）" % (
                    self.model, comp.error, comp.code)

        return True, "连通，共 %d 个模型%s" % (
            len(models), "，已找到「%s」" % self.model if self.model else "")

    def _probe_reachable(self, timeout: float = 1.0) -> bool:
        """快速探一下 host:port 的 TCP 可达性，用来区分"服务没起来"和"模型太慢"。"""
        try:
            parts = urllib.parse.urlsplit(self.base_url)
            host = parts.hostname or "127.0.0.1"
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            # 地址本身就畸形（端口不是数字）→ 谈不上可达
            return False
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
        """跑一次修正/翻译，**必定**返回 ``Outcome``（绝不抛异常，BF-7）。

        失败时 ``text`` 一律是**原文** —— 绝不能返回空串：
        空串会让用户的话白说（app.py 里 ``done`` 状态虽然会设，
        但注入的是空的，等于什么都没输入）。
        """
        try:
            comp = self.complete(prompt)
        except Exception as exc:  # noqa: BLE001  —— complete() 理论上不抛，
            # 这里仍然兜住：任何异常都不许把调用方的线程带走。
            self.last_error = repr(exc)
            return Outcome(text=original, degraded=True, notify=True,
                           error="调用 LLM 时出现未预期的错误：%r" % (exc,),
                           code="internal")

        if comp.error:
            # 失败分类在这里落地：
            #   * notify=True  → 配置类问题（地址错、Key 错），**必须提示用户**
            #   * notify=False → 超时/限流/5xx 之类，静默降级，别打扰用户
            self.last_error = comp.error
            return Outcome(text=original, degraded=True, notify=comp.notify,
                           error=comp.error, code=comp.code,
                           elapsed_ms=comp.elapsed_ms)

        try:
            text = validate_output(original, comp.text, hotwords)
        except LlmBadOutputError as exc:
            self.last_error = str(exc)
            return Outcome(text=original, degraded=True, error=str(exc),
                           code=exc.code, elapsed_ms=comp.elapsed_ms)
        except Exception as exc:  # noqa: BLE001
            self.last_error = repr(exc)
            return Outcome(text=original, degraded=True, error=repr(exc),
                           code="internal", elapsed_ms=comp.elapsed_ms)
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
        """修正一段识别文本。**必定**返回 ``Outcome``，绝不抛异常（BF-7）。

        失败时 ``text`` 是**原文**（不是空串），并用 ``notify`` / ``code``
        区分「配置错误（要提示用户）」和「静默降级（别打扰用户）」。
        """
        original = text if isinstance(text, str) else str(text or "")
        try:
            if not original.strip():
                return Outcome(text=original, degraded=True, error="原文为空",
                               code="empty")
            prompt = self._build_prompt(self.correct_prompt, hotwords, text=original)
            outcome = self._run(prompt + PLAIN_TEXT_SUFFIX, original, hotwords)
        except Exception as exc:  # noqa: BLE001  —— 最后的兜底
            self.last_error = repr(exc)
            return Outcome(text=original, degraded=True, notify=True,
                           error="修正时出现未预期的错误：%r" % (exc,),
                           code="internal")
        # 去结尾句号只在**模型真的出了结果**时做。失败降级时必须**原样**
        # 返回原文 —— 绝不能顺手把原文的句号也改掉，那会让用户看到的东西
        # 和自己说的不一样。
        if self.strip_trailing_period and not outcome.error and outcome.text.strip():
            stripped = strip_trailing_period(outcome.text)
            if stripped != outcome.text:
                outcome.text = stripped
                outcome.changed = stripped.strip() != (original or "").strip()
                outcome.degraded = not outcome.changed
                outcome.no_change = not outcome.changed
        return outcome

    def translate(self, text: str, target_language: str = "English") -> Outcome:
        """翻译一段文本。**必定**返回 ``Outcome``，绝不抛异常（BF-7）。"""
        original = text if isinstance(text, str) else str(text or "")
        try:
            if not original.strip():
                return Outcome(text=original, degraded=True, error="原文为空",
                               code="empty")
            prompt = self._build_prompt(self.translate_prompt, None,
                                        target_language=target_language, text=original)
            return self._run(prompt + PLAIN_TEXT_SUFFIX, original)
        except Exception as exc:  # noqa: BLE001  —— 最后的兜底
            self.last_error = repr(exc)
            return Outcome(text=original, degraded=True, notify=True,
                           error="翻译时出现未预期的错误：%r" % (exc,),
                           code="internal")


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
