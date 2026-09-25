# -*- coding: utf-8 -*-
"""CherryVoice · P4 集成验证：翻译开关真的接到了主流程上。

验证三件事（用本地假 LLM，不需要 API Key）：

1. 开启 ``--translate`` 后，**提交的是译文而不是原文**；
2. 悬浮窗的译文区是**流式增长**的（中途收到过译文回调）；
3. 翻译失败时回退提交原文（FR-4 的降级原则在 FR-5 同样适用）。

做法：起假 LLM（把源文本包成 ``<EN>…`` 当"译文"），跑一遍 ``--replay``，
再从客户端日志和落地文本断言。注入方式设为 ``none``，避免往用户窗口灌字。

退出码 0 = 通过。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MARK = "<EN>"


class FakeLlm(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fail = False

    def log_message(self, *_a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        prompt = payload["messages"][-1]["content"]
        src = prompt.split("原文：")[-1].lstrip("\n").split("\n\n只输出纯文本")[0].strip()
        if FakeLlm.fail:
            try:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass
            return
        # 假服务要能区分两种请求，否则测不出"关掉翻译时走的是修正而不是翻译"
        if "翻译" in prompt:
            content = MARK + src
        else:
            content = src          # 修正请求：原样返回 → 视作未改动 → 提交识别原文
        body = json.dumps({
            "id": "x", "object": "chat.completion", "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        }, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def run_app(wav: str, llm_url: str, translate: bool) -> str:
    cfg = {"llm": {"enabled": True, "base_url": llm_url, "api_key": "k",
                   "model": "fake", "timeout": 6.0},
           "translate": {"enabled": translate, "target_language": "English",
                         "silence_sec": 1.0, "chunk_chars": 30},
           "inject": {"mode": "none"}}
    cfg_path = Path(tempfile.gettempdir()) / "cv-translate-flow.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    args = [str(REPO / ".venv" / "Scripts" / "python.exe"), "-m", "client.app",
            "--config", str(cfg_path), "--replay", wav, "--exit-after-final"]
    if translate:
        args.append("--translate")
    proc = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    wav = sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.gettempdir()) / "cv-test16k.wav")
    if not Path(wav).exists():
        print("找不到测试音频：%s" % wav)
        return 2

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlm)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v1" % srv.server_address[1]

    rows: list[tuple[str, bool, str]] = []

    # ---- 1) 开启翻译：应当提交译文 ----
    FakeLlm.fail = False
    out = run_app(wav, url, translate=True)
    submitted = [l for l in out.splitlines() if "[未注入]" in l]
    payload = submitted[-1].split("：", 1)[-1] if submitted else ""
    rows.append(("开启翻译后提交的是译文（带 <EN> 标记）",
                 payload.startswith(MARK), "提交内容=%r" % payload[:60]))
    rows.append(("译文来自流式增量翻译，而不是整段一次翻完",
                 "翻译：已翻译" in out and "段数=" in out,
                 next((l for l in out.splitlines() if "翻译：" in l), "(无日志)")))
    rows.append(("原文仍然照常显示（原文/译文并行）",
                 "FINAL:" in out, "OK" if "FINAL:" in out else "缺 FINAL 日志"))

    # ---- 2) 关闭翻译：应当提交原文 ----
    out2 = run_app(wav, url, translate=False)
    sub2 = [l for l in out2.splitlines() if "[未注入]" in l]
    payload2 = sub2[-1].split("：", 1)[-1] if sub2 else ""
    rows.append(("关闭翻译后提交识别原文（不带 <EN>）",
                 bool(payload2) and not payload2.startswith(MARK),
                 "提交内容=%r" % payload2[:60]))

    # ---- 3) 翻译失败：回退原文 ----
    FakeLlm.fail = True
    out3 = run_app(wav, url, translate=True)
    sub3 = [l for l in out3.splitlines() if "[未注入]" in l]
    payload3 = sub3[-1].split("：", 1)[-1] if sub3 else ""
    rows.append(("翻译失败时回退提交原文（绝不提交空串）",
                 bool(payload3.strip()) and not payload3.startswith(MARK),
                 "提交内容=%r" % payload3[:60]))

    srv.shutdown()
    print("=" * 76)
    print("P4 集成验证：流式翻译接到主流程")
    print("=" * 76)
    for name, ok, detail in rows:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
    fails = [n for n, ok, _d in rows if not ok]
    print("-" * 76)
    print("结论：%s" % ("全部通过" if not fails else "失败项：%s" % fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
