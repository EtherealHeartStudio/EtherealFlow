# -*- coding: utf-8 -*-
"""验证识别客户端的**断线重连**（需求文档 风险 #2 的对策）。

风险 #2 原文是「WSL 服务不稳定 → 识别中断」，对策是「做成 systemd 服务自动重启；
客户端做重连」。服务端的 systemd 早就做了，但**客户端的重连从来没被验证过** ——
因为从没真的断过。这个测试用可控的假服务端把三种情形跑一遍：

1. 服务端**根本没起** → 客户端要报错、要持续重试、不能崩；
2. 服务端起来了 → 客户端自动连上，会话能用；
3. 会话中途服务端**被杀掉**再**拉起来** → 客户端要自动重连，**新会话能用**。

用真的 WebSocket（``websockets.sync.server``），不是打桩，所以连的是完整协议。

退出码 0 = 三条都过。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.sync.server import serve as ws_serve   # noqa: E402

from client.asr import AsrClient                        # noqa: E402


class FakeAsrServer:
    """只会说 EtherealFlow 协议的最小服务端。"""

    def __init__(self) -> None:
        self.sessions = 0
        self.audio_bytes = 0
        self.finals = 0
        self.starts: list[dict] = []      # 记录每次 start 帧的参数
        self._server = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def _handler(self, conn) -> None:  # noqa: ANN001
        try:
            for message in conn:
                if isinstance(message, (bytes, bytearray)):
                    self.audio_bytes += len(message)
                    conn.send(json.dumps({"type": "partial", "text": "累积文本"}))
                    continue
                payload = json.loads(message)
                kind = payload.get("type")
                if kind == "start":
                    self.sessions += 1
                    self.starts.append(payload)
                    conn.send(json.dumps({"type": "started"}))
                elif kind == "finish":
                    self.finals += 1
                    conn.send(json.dumps({"type": "final", "text": "假服务端的最终结果",
                                          "duration": self.audio_bytes / 32000.0}))
                elif kind == "cancel":
                    conn.send(json.dumps({"type": "cancelled"}))
        except Exception:  # noqa: BLE001  连接断了就退出这个连接的处理
            pass

    def start(self, port: int = 0) -> int:
        self._server = ws_serve(self._handler, "127.0.0.1", port)
        self.port = self._server.socket.getsockname()[1]

        def _serve() -> None:
            try:
                self._server.serve_forever()
            except OSError:
                pass        # shutdown() 关掉 socket 后这里会报错，属正常收尾

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def wait_for(pred, timeout: float = 12.0, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    rows: list[tuple[str, bool, str]] = []
    errors: list[str] = []
    finals: list[dict] = []
    partials: list[str] = []
    statuses: list[str] = []

    server = FakeAsrServer()
    port = server.start()
    server.stop()                      # 先占个端口号再放掉 → 保证"确定没人监听"

    client = AsrClient(
        url="ws://127.0.0.1:%d" % port,
        on_partial=partials.append,
        on_final=finals.append,
        on_error=errors.append,
        on_status=statuses.append,
    )
    client.start()

    # ---- 1) 服务端没起：要报错、不能崩 ----
    got_error = wait_for(lambda: any("连接失败" in e for e in errors), timeout=8)
    rows.append(("服务端没起时能报出连接失败（而不是静默卡住）", got_error,
                 (errors[0][:80] if errors else "（没有报错）")))
    rows.append(("服务端没起时客户端进程仍然存活（自动重试中）",
                 client._thread is not None and client._thread.is_alive(),  # noqa: SLF001
                 "工作线程存活=%s" % (client._thread is not None
                                     and client._thread.is_alive())))  # noqa: SLF001

    # ---- 2) 服务端起来：要自动连上且会话能用 ----
    server = FakeAsrServer()
    server.start(port)
    connected = wait_for(lambda: client.connected, timeout=15)
    rows.append(("服务端起来后客户端自动连上（无需重建客户端）", connected,
                 "connected=%s 状态=%s" % (client.connected, statuses[-3:])))

    finals.clear()
    client.begin_session(language="Chinese", context="")
    client.feed(b"\x00\x00" * 2560)
    client.end_session()
    got_final = wait_for(lambda: bool(finals), timeout=10)
    rows.append(("重连后的会话能正常拿到 final", got_final,
                 "final=%s" % (finals[-1].get("text") if finals else "（无）")))
    rows.append(("假服务端确实收到了音频与会话", server.sessions >= 1 and server.audio_bytes > 0,
                 "sessions=%d audio=%d 字节" % (server.sessions, server.audio_bytes)))

    # ---- 3) 会话中途掉线 → 自动重连 → 新会话能用 ----
    before = len(errors)
    server.stop()
    noticed = wait_for(lambda: len(errors) > before or not client.connected, timeout=12)
    rows.append(("服务端被杀掉后客户端能察觉", noticed,
                 "connected=%s 新增报错=%d" % (client.connected, len(errors) - before)))

    server2 = FakeAsrServer()
    server2.start(port)
    reconnected = wait_for(lambda: client.connected, timeout=20)
    rows.append(("服务端重新拉起来后自动重连", reconnected,
                 "connected=%s" % client.connected))

    finals.clear()
    client.begin_session(language=None, context="")
    client.feed(b"\x00\x00" * 2560)
    client.end_session()
    got_final2 = wait_for(lambda: bool(finals), timeout=12)
    rows.append(("重连后的**新**会话依然可用", got_final2,
                 "final=%s" % (finals[-1].get("text") if finals else "（无）")))

    # ---- 4) 说话说到一半断线：会话参数不能丢 ----
    # 这是上面那个测试顺带暴露出来的缺口：如果断线时这次说话还没结束，
    # 续传的音频会**没有前置 start 帧**，服务端只能按默认参数自动开会话，
    # 用户指定的语言提示与热词就全丢了。
    client.begin_session(language="Chinese", context="热词测试")
    client.feed(b"\x00\x00" * 1280)
    time.sleep(0.4)
    server2.stop()                       # 说到一半，服务端挂了
    dropped = wait_for(lambda: not client.connected, timeout=12)

    server3 = FakeAsrServer()
    server3.start(port)
    back = wait_for(lambda: client.connected, timeout=20)
    client.feed(b"\x00\x00" * 1280)      # 续传：注意**没有**再调 begin_session
    time.sleep(0.8)
    params_kept = any(s.get("language") == "Chinese" and s.get("context") == "热词测试"
                      for s in server3.starts)
    rows.append(("说话中途断线重连后，会话参数（语言/热词）被重新下发",
                 dropped and back and params_kept,
                 "重连后收到的 start 帧=%s" % (server3.starts or "（无）")))
    client.end_session()
    client.close()
    server.stop()
    server2.stop()
    server3.stop()

    print("=" * 74)
    print("验证：识别客户端断线重连（需求文档 风险 #2 的对策）")
    print("=" * 74)
    print("状态变化序列：%s" % statuses)
    if errors:
        print("期间报错（去重）：")
        for e in list(dict.fromkeys(errors))[:4]:
            print("   %s" % e[:96])
    print("-" * 74)
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
