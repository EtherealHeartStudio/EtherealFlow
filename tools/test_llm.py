# -*- coding: utf-8 -*-
"""EtherealFlow · 验证 LLM 修正/翻译的**失败分类**与**输出校验**。

不需要真的 API Key：起一个本地假 OpenAI 服务，用开关切换各种故障，
逐个断言"该降级的降级、该提示的提示"。

被验证的核心命题（需求文档 FR-4 + 竞品经验）：

* 认证/连接/模型名错误 → **不静默降级**，要提示用户去改配置
* 超时/限流/5xx      → **静默降级**为识别原文，用户不该被打扰
* 模型复读、吐热词表、返回空 → 输出校验拦下，退回原文
* 模型加了 Markdown 壳 → 剥掉壳后正常采用

退出码 0 = 全部通过。
"""

from __future__ import annotations

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.llm import (LlmClient, LlmBadOutputError, clean_model_text,  # noqa: E402
                        check_base_url, validate_output)

ORIGINAL = "嗯那个，我们今天就是测试一下这个语音输入法"
CORRECTED = "我们今天测试一下这个语音输入法。"

MODE = {"value": "ok"}
CALLS = {"n": 0}
# 服务端**实际看到**的请求头。用来验证「本地无 Key 时不发 Authorization」，
# 这是本地模型路径上最容易出错、又最难靠读代码确认的一点。
SEEN: dict[str, str | None] = {"auth": None, "get_path": "", "get_auth": None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # 静音
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass          # 客户端主动超时断开是预期行为，不要刷栈

    def do_GET(self):
        """``GET /v1/models`` —— 本地模型名用户记不住，设置界面靠它拉列表。"""
        SEEN["get_path"] = self.path
        SEEN["get_auth"] = self.headers.get("Authorization")
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [
                {"id": "fake-model", "object": "model"},
                {"id": "qwen2.5:7b", "object": "model"},
            ]})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        CALLS["n"] += 1
        SEEN["auth"] = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        prompt = payload["messages"][-1]["content"]
        mode = MODE["value"]

        if mode == "401":
            self._send(401, {"error": {"message": "Missing API key"}})
            return
        if mode == "429":
            self._send(429, {"error": {"message": "rate limited"}})
            return
        if mode == "500":
            self._send(500, {"error": {"message": "boom"}})
            return
        if mode == "slow":
            time.sleep(3.0)
            self._send(200, self._ok("慢死了"))
            return
        if mode == "truncated":
            # 推理模型的真实形态：思考把 max_tokens 花光，content 为空、
            # 只有 reasoning_content，finish_reason=length
            self._send(200, {
                "id": "chatcmpl-test", "object": "chat.completion",
                "created": int(time.time()), "model": "fake-model",
                "choices": [{"index": 0, "finish_reason": "length",
                             "message": {"role": "assistant", "content": "",
                                         "reasoning_content": "我们需要回答用户。" * 40}}],
                "usage": {"total_tokens": 42, "completion_tokens_details":
                          {"reasoning_tokens": 1024}}})
            return

        # 从 prompt 里把原文抠出来（热词在「原文：」之前，硬约束在其后）
        tail = prompt.split("原文：")[-1].lstrip("\n")
        original = tail.split("\n\n只输出纯文本")[0].strip()
        content = {
            "ok": CORRECTED,
            "echo": original + original,
            "empty": "",
            "hotword_echo": "EtherealFlow、Confucius4、R2T2",
            "markdown": "```text\n" + CORRECTED + "\n```",
            "prefixed": "修正后的文本：" + CORRECTED,
            "quoted": "“" + CORRECTED + "”",
            "same": original,
            "runaway": CORRECTED * 20,
        }.get(mode, CORRECTED)
        self._send(200, self._ok(content))

    @staticmethod
    def _ok(content: str) -> dict:
        return {"id": "chatcmpl-test", "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 42}}


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/v1" % srv.server_address[1]


def evaluate_case(out, want_changed: bool, want_deg: bool, want_notify: bool,
                  want_code: str) -> tuple[bool, str]:
    """把一次端到端调用的结果和期望**逐项**对比，返回 ``(是否通过, 说明)``。

    ⚠️ 这里必须逐项判、不能写成一条大 ``and`` 再 ``or want_changed`` ——
    Python 里 ``and`` 优先级高于 ``or``，那么写出来的等价于
    ``(... and ...) or want_changed``：凡是 ``want_changed=True`` 的行**恒为真**，
    成功路径永远不可能失败 —— 整套断言等于没写（BF-8）。

    期望的最终文本：改了就是 ``CORRECTED``，没改/降级就是 ``ORIGINAL``。
    """
    problems: list[str] = []
    if out.changed != want_changed:
        problems.append("changed 应为 %s，实际 %s" % (want_changed, out.changed))
    if out.degraded != want_deg:
        problems.append("degraded 应为 %s，实际 %s" % (want_deg, out.degraded))
    if out.notify != want_notify:
        problems.append("notify 应为 %s，实际 %s" % (want_notify, out.notify))
    if out.code != want_code:
        problems.append("code 应为 %r，实际 %r" % (want_code, out.code))
    want_text = CORRECTED if want_changed else ORIGINAL
    if out.text != want_text:
        problems.append("text 应为 %r，实际 %r" % (want_text, out.text))
    if not out.text.strip():
        problems.append("text 是空串（绝不能返回空串）")
    if not problems:
        return True, "changed=%s degraded=%s notify=%s code=%s text=%r" % (
            out.changed, out.degraded, out.notify, out.code, out.text[:40])
    return False, "；".join(problems)


def main() -> int:
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    # ---------- 纯函数：输出校验 ---------- #
    def expect_bad(label: str, original: str, output: str, hotwords=None) -> None:
        try:
            got = validate_output(original, output, hotwords)
            check("校验拦下：%s" % label, False, "竟然通过了，返回 %r" % got)
        except LlmBadOutputError as exc:
            check("校验拦下：%s" % label, True, str(exc))

    expect_bad("复读两遍", ORIGINAL, ORIGINAL + ORIGINAL)
    expect_bad("空输出", ORIGINAL, "   ")
    expect_bad("把热词表当结果", ORIGINAL, "EtherealFlow、Confucius4、R2T2",
               ["EtherealFlow", "Confucius4", "R2T2"])
    expect_bad("长度失控", ORIGINAL, "测试" * 200)

    check("剥掉 Markdown 代码块",
          clean_model_text("```text\n你好\n```") == "你好",
          repr(clean_model_text("```text\n你好\n```")))
    check("剥掉『修正后的文本：』前缀",
          clean_model_text("修正后的文本：你好") == "你好",
          repr(clean_model_text("修正后的文本：你好")))
    check("剥掉整段中文引号",
          clean_model_text("“你好”") == "你好",
          repr(clean_model_text("“你好”")))
    check("原样返回视作未改动（不报错）",
          validate_output(ORIGINAL, ORIGINAL) == ORIGINAL, "OK")

    # ---------- 端到端：各种故障 ---------- #
    # ---------- 修正模式（三种内置 + 自定义） ---------- #
    from client.config import DEFAULT_CONFIG
    from client.correction_modes import (BUILTIN_MODES, DEFAULT_MODE_ID,
                                         all_modes, delete_mode,
                                         effective_prompt, find_mode, new_mode,
                                         postprocess, prompt_is_overridden,
                                         resolve_mode, strip_trailing_period,
                                         upsert_mode)

    cfg0 = json.loads(json.dumps(DEFAULT_CONFIG["llm"]))
    ids = [m["id"] for m in all_modes(cfg0)]
    check("内置三个修正模式齐全且顺序固定",
          ids == ["none", "light", "deep"], "%s" % ids)
    check("每个内置模式都有名称/说明/示例/Prompt",
          all(m.get(k) for m in BUILTIN_MODES for k in
              ("name", "desc", "example", "prompt")),
          "%s" % [m["name"] for m in BUILTIN_MODES])
    check("默认模式是轻度整理", resolve_mode(cfg0)["id"] == DEFAULT_MODE_ID,
          resolve_mode(cfg0)["name"])

    # 力度必须是**递增**的：三个模式的 Prompt 不能是同一份，否则等于没得选
    prompts = {m["id"]: m["prompt"] for m in BUILTIN_MODES}
    check("三个模式的 Prompt 互不相同",
          len(set(prompts.values())) == 3, "去重后 %d 份" % len(set(prompts.values())))
    check("「不整理」明确要求不要加标点/不要删口水词",
          "不要加标点" in prompts["none"] and "不要删口水词" in prompts["none"],
          "见 none 模式 Prompt")
    check("「轻度整理」要求保留措辞",
          "保留用户原本的措辞" in prompts["light"], "见 light 模式 Prompt")
    check("「深度整理」禁止添加原文没有的内容",
          "不得添加原文没有" in prompts["deep"], "见 deep 模式 Prompt")

    # id 对不上（自定义模式被删、手改配置写错）必须回退，不能崩
    bad = dict(cfg0, mode="不存在的模式")
    check("模式 id 无效时回退到默认模式",
          resolve_mode(bad)["id"] == DEFAULT_MODE_ID, resolve_mode(bad)["name"])

    # 自定义模式
    mine = new_mode(cfg0, "邮件体", "整理成邮件语气",
                    "把下面的话整理成邮件语气。\n\n{hotwords}原文：\n{text}",
                    "您好，关于明天会议…")
    check("新建自定义模式有唯一 id", mine["id"] and mine["id"] not in ids, mine["id"])
    cfg1 = dict(cfg0)
    upsert_mode(cfg1, mine)
    check("自定义模式能出现在模式列表里",
          [m["id"] for m in all_modes(cfg1)] == ids + [mine["id"]],
          "%s" % [m["name"] for m in all_modes(cfg1)])
    cfg1["mode"] = mine["id"]
    check("能把自定义模式设为当前模式",
          resolve_mode(cfg1)["name"] == "邮件体", resolve_mode(cfg1)["name"])
    check("自定义模式的 Prompt 生效",
          effective_prompt(cfg1) == mine["prompt"], effective_prompt(cfg1)[:30])

    dup = new_mode(cfg1, "邮件体", prompt="x")
    check("重名会生成不同的 id（不会覆盖）", dup["id"] != mine["id"], dup["id"])
    check("内置模式删不掉", delete_mode(cfg1, "light") is False, "light")
    check("删掉当前自定义模式后自动回退默认",
          delete_mode(cfg1, mine["id"]) and cfg1["mode"] == DEFAULT_MODE_ID,
          "mode=%s" % cfg1["mode"])
    check("坏数据不会让程序崩（自定义模式里混入垃圾）",
          [m["id"] for m in all_modes({"modes": [None, 5, {}, {"name": "只有名字"}]})]
          == ids,
          "垃圾项被忽略")

    # llm.prompt 是高级后门：非空则完全覆盖所选模式
    cfg2 = dict(cfg0, prompt="我自己写的 Prompt {text}")
    check("手写 Prompt 覆盖模式 Prompt",
          effective_prompt(cfg2) == "我自己写的 Prompt {text}", effective_prompt(cfg2))
    check("覆盖状态可被界面查询到", prompt_is_overridden(cfg2) is True, "")

    # 结尾句号
    check("去掉结尾中文句号", strip_trailing_period("你好。") == "你好",
          repr(strip_trailing_period("你好。")))
    check("去掉结尾英文句号（含多余空格）",
          strip_trailing_period("hello.  ") == "hello",
          repr(strip_trailing_period("hello.  ")))
    check("只去句号，不动问号/感叹号",
          strip_trailing_period("你好？") == "你好？"
          and strip_trailing_period("你好！") == "你好！",
          repr(strip_trailing_period("你好？")))
    check("句中句号不动", strip_trailing_period("你好。再见") == "你好。再见",
          repr(strip_trailing_period("你好。再见")))
    check("全是句号时不返回空串", strip_trailing_period("。。。") != "",
          repr(strip_trailing_period("。。。"))[:20])
    check("开关关闭时不做后处理",
          postprocess("你好。", {"strip_trailing_period": False}) == "你好。"
          and postprocess("你好。", {"strip_trailing_period": True}) == "你好",
          "postprocess")

    # 客户端真的按模式取 Prompt
    from client.llm import default_client
    cli_none = default_client(dict(cfg0, mode="none"))
    cli_deep = default_client(dict(cfg0, mode="deep"))
    check("客户端按所选模式取 Prompt",
          cli_none.correct_prompt == prompts["none"]
          and cli_deep.correct_prompt == prompts["deep"],
          "%s / %s" % (cli_none.mode_name, cli_deep.mode_name))
    check("客户端带回模式名（日志里要显示）",
          cli_none.mode_name == "不整理", cli_none.mode_name)

    srv, base = start_server()
    client = LlmClient(base_url=base, api_key="k", model="fake-model", timeout=1.5)

    cases = [
        # (模式, 期望 changed, 期望 degraded, 期望 notify, 期望 code, 说明)
        ("ok", True, False, False, "", "正常修正"),
        ("markdown", True, False, False, "", "带 Markdown 壳也能采用"),
        ("prefixed", True, False, False, "", "带前缀也能采用"),
        ("quoted", True, False, False, "", "带引号也能采用"),
        ("echo", False, True, False, "bad_output", "复读 → 退回原文"),
        ("empty", False, True, False, "bad_output", "空输出 → 退回原文"),
        ("hotword_echo", False, True, False, "bad_output", "热词回声 → 退回原文"),
        ("same", False, True, False, "", "原样返回 → 视作未改动"),
        ("runaway", False, True, False, "bad_output", "长度失控 → 退回原文"),
        ("401", False, True, True, "auth", "鉴权失败 → 降级但**必须提示**用户"),
        ("429", False, True, False, "rate_limit", "限流 → 静默降级"),
        ("500", False, True, False, "server", "5xx → 静默降级"),
        ("slow", False, True, False, "timeout", "超时 → 静默降级"),
        ("truncated", False, True, True, "truncated",
         "推理模型思考吃光 max_tokens → 单独分类并**提示用户**"
         "（不能报成『输出长度失控』，也不能静默）"),
    ]
    for mode, want_changed, want_deg, want_notify, want_code, label in cases:
        MODE["value"] = mode
        out = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
        # 断言统一走 evaluate_case —— 那里逐项比对，不会出现
        # "and 优先级高于 or 导致 want_changed=True 的行恒真"的坑（BF-8）。
        ok, detail = evaluate_case(out, want_changed, want_deg,
                                   want_notify, want_code)
        check("端到端：%s" % label, ok, detail)

    # 连不上：先占一个端口拿到"确定没人监听"的号，再放掉
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    dead = LlmClient(base_url="http://127.0.0.1:%d/v1" % free_port,
                     api_key="k", timeout=1.0)
    out = dead.correct(ORIGINAL)
    check("端到端：连不上服务 → 降级但必须提示用户",
          out.degraded and out.notify and out.code == "connection" and out.text == ORIGINAL,
          "degraded=%s notify=%s code=%s text=%r"
          % (out.degraded, out.notify, out.code, out.text[:30]))

    # 空原文不该发请求
    before = CALLS["n"]
    out = client.correct("   ")
    check("空原文不发请求", CALLS["n"] == before and out.code == "empty",
          "调用次数 %d → %d，code=%s" % (before, CALLS["n"], out.code))

    # 翻译走的是另一套 prompt
    MODE["value"] = "ok"
    out = client.translate(ORIGINAL, "English")
    check("翻译：正常返回并被采用", out.changed and out.text == CORRECTED,
          "text=%r" % out.text)

    # ---------- BF-8 阳性对照：断言必须**真的能失败** ---------- #
    # 这一段是**测试自己的自检**，不是测产品。
    #
    # 背景：原来的断言写成
    #     ok = (A and B and C and D and E) or want_changed
    # Python 里 ``and`` 优先级高于 ``or``，它等价于 ``(... and ...) or want_changed``
    # —— 于是凡是 ``want_changed=True`` 的行**恒为真**，成功路径永远不可能失败。
    # 这样的断言等于没写：实现坏掉了它也不会报。
    #
    # 对照做法：故意让实现返回**坏输出**，然后断言对应的用例**必须失败**。
    # 这条对照要**能重复跑**，所以用 mock 替换掉某个函数，而不是手工改文件。
    MODE["value"] = "markdown"
    good = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
    ok_good, detail_good = evaluate_case(good, True, False, False, "")
    check("阳性对照前提：正常服务下『带 Markdown 壳也能采用』判通过",
          ok_good, detail_good)

    # 现在把校验函数换成一个**坏的**：它不剥 Markdown 壳，原样返回。
    # 于是实现的正确性被破坏 —— 对应用例**必须**被判失败。
    import unittest.mock as _mock
    with _mock.patch("client.llm.validate_output",
                     lambda original, output, hotwords=None: output):
        MODE["value"] = "markdown"
        broken = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
    ok_broken, detail_broken = evaluate_case(broken, True, False, False, "")
    check("阳性对照：实现被改坏时，对应用例**必须判失败**"
          "（否则说明断言恒真，BF-8 没修好）",
          not ok_broken,
          "改坏后 evaluate_case 判定=%s（应为 False）。细节：%s"
          % (ok_broken, detail_broken))

    # 反向对照：把实现改坏到"输出内容完全不对"时，也必须判失败。
    with _mock.patch("client.llm.validate_output",
                     lambda original, output, hotwords=None: "（坏的实现）"):
        MODE["value"] = "ok"
        broken2 = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
    ok_broken2, detail_broken2 = evaluate_case(broken2, True, False, False, "")
    check("阳性对照：实现返回坏内容时，对应用例**必须判失败**",
          not ok_broken2,
          "改坏后 evaluate_case 判定=%s（应为 False）。细节：%s"
          % (ok_broken2, detail_broken2))

    # 证明"老写法"确实是恒真的：把 BF-8 里那个原始表达式原样搬过来，
    # 喂它上面那个**已经坏了**的结果 —— 它照样判"通过"。
    # 这正是缺陷报告里说的「4/15 行成功路径不可能失败」。
    want_changed = True
    want_deg = False
    want_notify = False
    want_code = ""
    old_style = (broken.changed == want_changed and broken.degraded == want_deg
                 and broken.notify == want_notify
                 and (want_code == "" or broken.code == want_code)
                 and broken.text.strip() != "" or want_changed)
    check("阳性对照：老写法（and/or 优先级错误）对坏结果仍判通过 —— "
          "这正是 BF-8 的病根",
          old_style is True,
          "老写法判定=%s（这里必须是 True，才说明病根复现成功）" % old_style)

    # 另一条对照：让**假服务**返回坏输出（长度失控），却按"正常修正"去断言，
    # 对应用例必须判失败。这条不需要 mock，直接改服务端的输出开关。
    MODE["value"] = "runaway"
    runaway = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
    ok_runaway, detail_runaway = evaluate_case(runaway, True, False, False, "")
    check("阳性对照：服务端返回坏输出时，按『正常修正』断言**必须判失败**",
          not ok_runaway,
          "判定=%s（应为 False）。细节：%s" % (ok_runaway, detail_runaway))
    MODE["value"] = "ok"

    # ---------- BF-7：畸形地址不许杀掉修正线程 ---------- #
    # `http://host:abc/v1` 这种地址在 http.client 里抛的是 InvalidURL ——
    # 它**既不是** URLError **也不是** OSError。以前它会一路逃出 correct()，
    # 把 client/app.py 里发起修正的线程打死：界面永远停在"修正中"，
    # 用户的话白说了（不注入任何东西，done 状态永不设置）。
    bad_urls = [
        ("http://host:abc/v1", "端口不是数字"),
        ("http://host:99999/v1", "端口超出范围"),
        ("http://host:not a port/v1", "地址里有空格"),
        ("", "地址是空的"),
        ("ftp://host/v1", "协议不支持"),
    ]
    for raw, why in bad_urls:
        try:
            out = LlmClient(base_url=raw, api_key="k", model="m",
                            timeout=1.0).correct(ORIGINAL)
        except Exception as exc:  # noqa: BLE001
            check("畸形地址（%s）不许抛异常" % why, False,
                  "抛出了 %s: %s" % (type(exc).__name__, exc))
            continue
        check("畸形地址（%s）→ 归类为配置错误并提示用户" % why,
              out.degraded and out.notify and out.code == "config"
              and out.text == ORIGINAL,
              "degraded=%s notify=%s code=%s text=%r error=%r"
              % (out.degraded, out.notify, out.code, out.text[:30], out.error[:80]))

    # 提示必须**说清楚哪个地址不对、怎么改**，而不是"连接失败"
    out = LlmClient(base_url="http://host:abc/v1", api_key="k").correct(ORIGINAL)
    check("配置错误提示里要指出是哪个地址、端口怎么写",
          "host:abc" in out.error and "端口" in out.error,
          "error=%r" % out.error[:120])

    # 翻译路径同样不许抛异常
    try:
        out = LlmClient(base_url="http://host:abc/v1", api_key="k").translate(ORIGINAL, "English")
        check("翻译遇到畸形地址 → 降级为原文并提示用户",
              out.degraded and out.notify and out.code == "config"
              and out.text == ORIGINAL,
              "degraded=%s notify=%s code=%s text=%r"
              % (out.degraded, out.notify, out.code, out.text[:30]))
    except Exception as exc:  # noqa: BLE001
        check("翻译遇到畸形地址 → 降级为原文并提示用户", False,
              "抛出了 %s: %s" % (type(exc).__name__, exc))

    # 地址检查函数本身
    check("地址检查：正常地址通过",
          check_base_url("http://127.0.0.1:8317/v1") is None,
          repr(check_base_url("http://127.0.0.1:8317/v1")))
    check("地址检查：端口不是数字 → 给出人话提示",
          "端口" in (check_base_url("http://host:abc/v1") or ""),
          repr(check_base_url("http://host:abc/v1")))
    check("地址检查：地址为空 → 给出人话提示",
          "空" in (check_base_url("") or ""),
          repr(check_base_url("")))
    check("地址检查：协议不对 → 给出人话提示",
          "http" in (check_base_url("ftp://host/v1") or ""),
          repr(check_base_url("ftp://host/v1")))

    # complete() 本身也不能抛异常 —— 它是底层，改动它最容易漏
    try:
        comp = LlmClient(base_url="http://host:abc/v1", api_key="k").complete("hi")
        check("complete() 遇畸形地址 → 返回带错误的 Completion，不抛异常",
              bool(comp.error) and comp.code == "config" and comp.notify,
              "error=%r code=%r notify=%r" % (comp.error[:60], comp.code, comp.notify))
    except Exception as exc:  # noqa: BLE001
        check("complete() 遇畸形地址 → 返回带错误的 Completion，不抛异常", False,
              "抛出了 %s: %s" % (type(exc).__name__, exc))

    # ---------- 本地模型路径 ---------- #
    # 本地部署（Ollama / LM Studio / llama.cpp / vLLM）通常**不校验** API Key。
    # 不设 Key 时绝不能发 Authorization 头 —— 有些实现收到 "Bearer " 会直接 401，
    # 表现成"本地模型连不上"，排查起来毫无头绪。
    SEEN["auth"] = "未重置"
    nokey = LlmClient(base_url=base, api_key="", model="fake-model", timeout=5.0)
    out = nokey.correct(ORIGINAL)
    check("本地无 Key：不带 Authorization 头且能出结果",
          out.changed and SEEN["auth"] is None,
          "服务端看到的 Authorization=%r" % SEEN["auth"])

    # 用户手填地址一定会填出各种写法，每一种都得能用
    port = srv.server_address[1]
    for label, raw, want in [
        ("裸地址（无协议、无 /v1）", "127.0.0.1:%d" % port, "http://127.0.0.1:%d/v1" % port),
        ("多余尾斜杠", "http://127.0.0.1:%d/v1/" % port, "http://127.0.0.1:%d/v1" % port),
        ("直接粘完整端点", "http://127.0.0.1:%d/v1/chat/completions" % port,
         "http://127.0.0.1:%d/v1" % port),
        ("非标准路径不擅自补 /v1", "http://127.0.0.1:%d/api" % port,
         "http://127.0.0.1:%d/api" % port),
    ]:
        c = LlmClient(base_url=raw, api_key="k", model="fake-model", timeout=5.0)
        served = c.correct(ORIGINAL).changed if want.endswith("/v1") else True
        check("地址归一化：%s" % label, c.base_url == want and served,
              "%r → %r" % (raw, c.base_url))

    # 模型列表 + 测试连接（设置界面的两个按钮靠它们）
    models = nokey.list_models()
    check("列出服务端模型", models == ["fake-model", "qwen2.5:7b"], repr(models))
    check("列模型走的是正确的路径", SEEN["get_path"].endswith("/v1/models"),
          SEEN["get_path"])
    # ⚠️ probe_call=False：这两条测的是**文案**，不是连通性探测本身。
    # 开启探测会额外发一次聊天请求，而这里的假服务端只实现了 models 接口。
    # 「探测真的发了请求」由下面单独的用例覆盖。
    okc, msg = nokey.test_connection(probe_call=False)
    check("测试连接：成功且指出找到了模型", okc and "已找到" in msg, msg)
    wrong = LlmClient(base_url=base, api_key="", model="拼错的模型名", timeout=5.0)
    okc2, msg2 = wrong.test_connection(probe_call=False)
    check("测试连接：模型名写错要明确指出来", okc2 and "没找到" in msg2, msg2)

    # ⚠️ 「列表里有这个模型」≠「这个模型能用」。
    # 实测踩过：代理端把模型列在 /v1/models 里，但实际调用返回
    # usage_limit_reached（配额用尽）—— 只查列表的话，设置界面显示
    # 「✓ 连通」，用户一用就失败。所以 test_connection 必须真发一次请求。
    MODE["value"] = "500"
    try:
        okc3, msg3 = nokey.test_connection(probe_call=True)
        check("测试连接：模型在列表里但实际调用失败 → 要报失败",
              (not okc3) and "实际调用失败" in msg3, msg3)
        okc4, msg4 = nokey.test_connection(probe_call=False)
        check("测试连接：只看列表时仍算连通（models 接口正常）",
              okc4 and "已找到" in msg4, msg4)
    finally:
        MODE["value"] = "ok"

    srv.shutdown()
    print("=" * 74)
    print("P3 验证：LLM 修正 / 翻译（本地假服务，不需要 API Key）")
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
