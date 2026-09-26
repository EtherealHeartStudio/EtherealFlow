# -*- coding: utf-8 -*-
"""EtherealFlow · 识别服务客户端（WebSocket，后台线程 + 自动重连）。

对应需求文档 §7.1 协议。设计要点：

* 用 ``websockets.sync.client``，**同步**接口更好嵌进 tkinter 这种线程模型；
* 一条长连接常驻，断线自动重连（需求文档 风险 #2 的对策）；
* 所有帧（start / 音频 / finish / cancel）走**同一个队列**，
  因此"连接还没建立就按下热键"也不会乱序，重连后仍然按原顺序补齐；
* 音频队列有上限：连不上时不让内存无限涨，超出就丢**最旧**的音频并告警。

回调都在**工作线程**里触发，UI 侧必须自己转线程（``Overlay.post`` 已经是线程安全的）。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable, Optional

from websockets.sync.client import connect as ws_connect

MAX_QUEUE_BLOCKS = 400          # 约 64 秒音频，超出就丢最旧的
DRAIN_BATCH = 8                 # 每轮最多连发 8 帧(≈1.28 s 音频)就回去收一次


class AsrClient:
    def __init__(self, url: str = "ws://127.0.0.1:18300",
                 on_partial: Optional[Callable[[str], None]] = None,
                 on_final: Optional[Callable[[dict], None]] = None,
                 on_segment_final: Optional[Callable[[dict], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None) -> None:
        self.url = url
        self.on_partial = on_partial or (lambda _t: None)
        self.on_final = on_final or (lambda _m: None)
        # 长语音"分段重建会话"时，每一段的 final 走这个回调（要**追加**而不是结束）
        self.on_segment_final = on_segment_final or (lambda _m: None)
        self.on_error = on_error or (lambda _m: None)
        self.on_status = on_status or (lambda _s: None)

        self._q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error = ""
        self.session_open = False
        self._rotating = False
        # 记住本次会话的 start 参数：断线重连后要**用同样的参数**重开会话，
        # 否则续传的音频会被服务端按默认参数自动开会话（语言提示、热词全丢）。
        self._session_args: dict = {"language": None, "context": ""}

    # -- 对外 -------------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="asr")
        self._thread.start()

    def begin_session(self, language: Optional[str] = None, context: str = "") -> None:
        self.session_open = True
        self._session_args = {"language": language, "context": context}
        self._q.put(("start", {"type": "start", **self._session_args}))

    def feed(self, pcm: bytes) -> None:
        if self._q.qsize() > MAX_QUEUE_BLOCKS:
            dropped = 0
            while self._q.qsize() > MAX_QUEUE_BLOCKS // 2:
                try:
                    self._q.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
            self.on_error("识别服务未就绪，丢弃了 %d 个音频块（约 %.1f 秒）"
                          % (dropped, dropped * 0.16))
        self._q.put(("audio", pcm))

    def end_session(self) -> None:
        self._q.put(("finish", {"type": "finish"}))

    def cancel_session(self) -> None:
        self.session_open = False
        self._rotating = False
        self._q.put(("cancel", {"type": "cancel"}))

    def rotate(self) -> None:
        """**分段重建会话**：结束当前段、拿到 final，然后立刻开新的一段。

        为什么需要：流式解码每块都要重新编码累积音频，成本随长度增长
        （实测 6.7 s 音频 0.58× 实时 → 33.7 s 0.88× → 53.9 s **1.15×**，
        也就是超过约 45 秒就跟不上实时了）。所以在到点之前主动切一段，
        让每一段都待在"比实时快"的区间里，把长说话拆成若干短会话。
        """
        if not self.session_open or self._rotating:
            return
        self._rotating = True
        self._q.put(("finish", {"type": "finish"}))

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- 工作线程 ---------------------------------------------------------- #

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                with ws_connect(self.url, open_timeout=5, close_timeout=2) as ws:
                    self.connected = True
                    self.last_error = ""
                    self.on_status("connected")
                    backoff = 0.5
                    # 断线时如果这次说话还没结束，得用**原来的参数**补一个 start，
                    # 否则续传的音频会被服务端按默认参数自动开会话。
                    if self.session_open:
                        ws.send(json.dumps({"type": "start", **self._session_args},
                                           ensure_ascii=False))
                    self._pump(ws)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                msg = "%s" % exc
                if msg != self.last_error:
                    self.last_error = msg
                    self.on_error("识别服务连接失败：%s" % msg)
                    self.on_status("disconnected")
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
        self.connected = False

    def _pump(self, ws) -> None:  # noqa: ANN001
        while not self._stop.is_set():
            # 1) 发：**每轮最多发这么多帧就回去收一次**。
            #    如果一次性把积压全灌进 socket，``ws.send`` 会在对方处理不过来时阻塞，
            #    期间收不到服务端的 keepalive ping，就会被当成死连接断开
            #    （实测：长语音背压时确实会这样）。分批 + 穿插接收可以避免。
            sent = 0
            while sent < DRAIN_BATCH:
                try:
                    kind, payload = self._q.get_nowait()
                except queue.Empty:
                    break
                if kind == "audio":
                    ws.send(payload)
                else:
                    ws.send(json.dumps(payload, ensure_ascii=False))
                    if kind == "cancel":
                        self.session_open = False
                sent += 1
            # 2) 收
            try:
                message = ws.recv(timeout=0.02)
            except TimeoutError:
                continue
            if isinstance(message, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(message)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "partial":
                self.on_partial(msg.get("text", ""))
            elif mtype == "final":
                self.session_open = False
                if self._rotating:
                    self._rotating = False
                    self.on_segment_final(msg)
                else:
                    self.on_final(msg)
            elif mtype == "error":
                self.on_error("服务端错误：%s" % msg.get("message", ""))
            elif mtype == "ready":
                self.on_status("ready")
