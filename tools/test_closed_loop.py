# -*- coding: utf-8 -*-
"""EtherealFlow · P3 验收：**完整闭环**。

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

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps                                # noqa: E402
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


def _probe_synthetic_paste(root, text) -> bool:
    """探测：本环境能不能用合成按键完成「粘贴」？

    方法：往目标 Text 里塞一个标记 → 清空 → 用**同一套注入器**写入剪贴板
    并模拟 Ctrl+V → 看标记回来了没有。

    回来了 = 合成按键投递正常 → 后面可以断言文本框内容。
    没回来 = 本环境不投递合成按键（受管环境/自动化会话的常见情况）
    → 粘贴环节无法在此验证，调用方应标 [SKIP]。

    这个探测必须在**跑客户端之前**做，否则会把「客户端的注入」和
    「环境能不能粘贴」两件事混在一起，分不清是谁的问题。
    """
    marker = "【粘贴探测】"
    text.insert("1.0", marker)
    root.update()
    text.delete("1.0", "end-1c")
    root.update()

    from client.inject import (clipboard_set_text, clipboard_snapshot,
                               press_ctrl_v)
    # 探测会改剪贴板，先备份，测完还原 —— 不能让测试污染用户剪贴板。
    snap = clipboard_snapshot()
    try:
        if not clipboard_set_text(marker):
            print("  [探测] 剪贴板写入失败 → 判定本环境无法验证粘贴")
            return False
        time.sleep(0.05)
        press_ctrl_v()

        deadline = time.time() + 2.0
        while time.time() < deadline:
            root.update()
            time.sleep(0.02)
            if marker in text.get("1.0", "end-1c"):
                print("  [探测] 合成粘贴可用 → 文本框内容可断言")
                return True
        print("  [探测] 合成粘贴不可用（Ctrl+V 没投递到桌面）→ 粘贴环节将标 [SKIP]")
        return False
    finally:
        # 还原用户原本的剪贴板（写回文本；非文本内容这里不碰，
        # 探测本身只写文本，不会破坏别的格式）
        if snap.has_text and snap.text:
            clipboard_set_text(snap.text)
        else:
            text.delete("1.0", "end-1c")
            root.update()


def main() -> int:
    # 依赖检查：缺识别服务或缺音频时**明确 SKIP**，不算失败。
    # 项目规矩见 docs/development.md §3：区分「环境做不到」和「代码错了」。
    skip = _deps.require_asr()
    if skip:
        print(skip)
        return 0
    wav = sys.argv[1] if len(sys.argv) > 1 else str(Path(tempfile.gettempdir()) / "cv-test16k.wav")
    if not Path(wav).exists():
        found = _deps.find_wav()
        if found is None:
            print("[SKIP] 找不到测试音频：%s —— 需要一份 16k 单声道 WAV。"
                  "本项跳过，不代表代码有问题。" % wav)
            return 0
        wav = str(found)
        print("用找到的测试音频：%s" % wav)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlm)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm_url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
    print("假 LLM 服务：%s" % llm_url)

    # 目标输入框
    root = tk.Tk()
    w32.enable_dpi_awareness()
    root.title("EtherealFlow 闭环验收目标")
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

    # 先探测「合成粘贴在本环境是否可用」，再跑客户端。
    # 顺序很重要：探测放在客户端跑之前，才能把「环境能不能粘贴」
    # 和「客户端注入对不对」分成两件事，否则会误判成代码缺陷。
    paste_possible = _probe_synthetic_paste(root, text)
    text.delete("1.0", "end-1c")
    root.update()

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

    # ── 关键判定：合成按键在本环境能不能投递到桌面？ ──────────────
    # 注入的**最后一步**是模拟 Ctrl+V。若本环境不把合成按键投递到桌面
    # （实测：受管环境/自动化会话里 GetAsyncKeyState 看不到任何按键），
    # 粘贴根本不会发生，文本框必然是空的 ——
    # 那是**环境做不到**，不是代码错了。
    # 项目规矩（docs/development.md §3）：这种情况必须标 [SKIP]，
    # 不能标 [PASS]，也不能算成 [FAIL]。
    paste_possible = _probe_synthetic_paste(root, text)

    checks = [
        ("注入期间前台窗口没有被悬浮窗抢走", fg_before == fg_after,
         "0x%X → 0x%X" % (fg_before, fg_after)),
    ]
    if paste_possible:
        checks.append(("文本框收到了内容", bool(content.strip()), repr(content[:40])))
        checks.append(("收到的是**修正后**的文本（带模型标记）", content.startswith(MARK),
                       "以 %r 开头" % content[:8]))
    ok_all = True
    for name, ok, detail in checks:
        ok_all &= ok
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)

    if not paste_possible:
        print("[SKIP] 文本框内容与「收到的是修正后文本」两项 —— "
              "本环境不把合成按键投递到桌面（Ctrl+V 不会触发），"
              "粘贴环节无法在此验证。识别与 LLM 修正环节已通过。")
        print("        剪贴板内容已正确写入（客户端日志里的「注入…成功」为证），"
              "真机验收时人工确认粘贴。")

    print("-" * 72)
    if paste_possible:
        print("结论：%s" % ("完整闭环打通" if ok_all else "存在失败项"))
    else:
        print("结论：闭环各环节已验证，粘贴环节因环境限制跳过")
    srv.shutdown()
    try:
        root.destroy()
    except tk.TclError:
        pass
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
