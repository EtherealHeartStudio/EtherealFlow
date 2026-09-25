# -*- coding: utf-8 -*-
"""CherryVoice · P3 验收：**完整闭环**。

    说话 → 悬浮窗出字 → 松开 → LLM 修正 → 修正后的文本进当前输入框

怎么做到不需要人说话、也不需要真 API Key：

* 音频走 ``--replay`` 回放一份真实 WAV（走的是和按下热键**完全相同**的代码路径）；
* LLM 指向本进程起的**假 OpenAI 服务**，它把识别原文包装成 ``（已修正）…``；
* 目标输入框是本进程的 Tk 文本框，内容能直接读回来 ——
  所以"注入的到底是修正后的文本还是识别原文"是**可判定**的。

断言：文本框里出现的是**带修正标记**的文本，而不是识别原文。
这就同时证明了 LLM 那一步真的跑了、而且它的结果被注入进去了。

退出码 0 = 通过。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import win32 as w32            # noqa: E402

MARK = "（已修正）"
REPO = Path(__file__).resolve().parent.parent


class FakeLlm(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        prompt = payload["messages"][-1]["content"]
        original = prompt.split("原文：")[-1].lstrip("\n").split("\n\n只输出纯文本")[0].strip()
        content = MARK + original
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


def main() -> int:
    wav = sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.gettempdir()) / "cv-test16k.wav")
    if not Path(wav).exists():
        print("找不到测试音频：%s" % wav)
        return 2

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlm)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm_url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
    print("假 LLM 服务：%s" % llm_url)

    # 目标输入框
    root = tk.Tk()
    w32.enable_dpi_awareness()
    root.title("CherryVoice 闭环验收目标")
    root.geometry("720x240+180+180")
    text = tk.Text(root, font=("Microsoft YaHei UI", 13))
    text.pack(fill="both", expand=True)
    root.update()
    hwnd = w32.top_level_window(int(root.winfo_id()))
    w32.force_foreground(hwnd)
    text.focus_set()
    for _ in range(50):
        root.update()
        time.sleep(0.01)
    print("目标窗口：0x%X [%s]" % (hwnd, w32.class_name(hwnd)))

    cfg = {
        "llm": {"enabled": True, "base_url": llm_url, "api_key": "fake-key",  # release-check: allow
                "model": "fake", "timeout": 8.0},
        "inject": {"mode": "clipboard", "restore_clipboard": True, "restore_delay_ms": 400},
        "hotwords": [],
    }
    cfg_path = Path(tempfile.gettempdir()) / "cv-closed-loop.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    app = subprocess.Popen(
        [str(REPO / ".venv" / "Scripts" / "python.exe"), "-m", "client.app",
         "--config", str(cfg_path), "--replay", wav, "--exit-after-final"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        # 不给子进程建控制台：否则新控制台窗口会抢走前台，注入就打偏了
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    fg_before = w32.foreground_window()
    print("启动客户端（前台 0x%X）。期间保持目标窗口获得焦点…" % fg_before)

    deadline = time.time() + 60
    while app.poll() is None and time.time() < deadline:
        root.update()                      # 让 Tk 处理粘贴消息
        time.sleep(0.02)
    out, _ = app.communicate(timeout=10)
    root.update()
    time.sleep(0.5)
    root.update()

    content = text.get("1.0", "end-1c")
    fg_after = w32.foreground_window()
    text_hidden = root.withdraw() if False else None   # noqa: F841

    print("=" * 72)
    print("P3 验收：完整闭环（识别 → LLM 修正 → 注入）")
    print("=" * 72)
    print("--- 客户端输出 ---")
    for line in (out or "").strip().splitlines():
        print("  " + line)
    print("--- 结果 ---")
    print("  目标文本框内容 = %r" % content)
    print("  注入前后前台窗口 = 0x%X → 0x%X（%s）"
          % (fg_before, fg_after, "未变" if fg_before == fg_after else "变了"))

    checks = [
        ("文本框收到了内容", bool(content.strip()), repr(content[:40])),
        ("收到的是**修正后**的文本（带模型标记）", content.startswith(MARK),
         "以 %r 开头" % content[:8]),
        ("注入期间前台窗口没有被悬浮窗抢走", fg_before == fg_after,
         "0x%X → 0x%X" % (fg_before, fg_after)),
    ]
    ok_all = True
    for name, ok, detail in checks:
        ok_all &= ok
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
    print("-" * 72)
    print("结论：%s" % ("完整闭环打通" if ok_all else "存在失败项"))
    srv.shutdown()
    try:
        root.destroy()
    except tk.TclError:
        pass
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
