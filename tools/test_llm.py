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

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.llm import (LlmClient, LlmBadOutputError, clean_model_text,  # noqa: E402
                        validate_output)

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
    ]
    for mode, want_changed, want_deg, want_notify, want_code, label in cases:
        MODE["value"] = mode
        out = client.correct(ORIGINAL, hotwords=["EtherealFlow", "Confucius4", "R2T2"])
        ok = (out.changed == want_changed and out.degraded == want_deg
              and out.notify == want_notify
              and (want_code == "" or out.code == want_code)
              and out.text.strip() != "" or want_changed)
        # 降级时注入的必须是**原文**，绝不能是空串（否则用户白说一句）
        if want_deg and want_code != "" and mode != "same":
            ok = ok and out.text == ORIGINAL
        check("端到端：%s" % label, bool(ok),
              "changed=%s degraded=%s notify=%s code=%s text=%r"
              % (out.changed, out.degraded, out.notify, out.code, out.text[:40]))

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
    okc, msg = nokey.test_connection()
    check("测试连接：成功且指出找到了模型", okc and "已找到" in msg, msg)
    wrong = LlmClient(base_url=base, api_key="", model="拼错的模型名", timeout=5.0)
    okc2, msg2 = wrong.test_connection()
    check("测试连接：模型名写错要明确指出来", okc2 and "没找到" in msg2, msg2)

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
