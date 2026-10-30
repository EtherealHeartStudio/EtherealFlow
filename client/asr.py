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
# 分段期间暂存区的上限。暂存的是「下一段」的音频，
# 服务端 flush 一段要几秒 —— 几秒的音频是几块，200 块(32 秒)足够宽裕。
MAX_HOLD_BLOCKS = 200


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
        # 松手时恰好在分段途中 → 记下「想结束」，等这次分段的 final 回来再收尾。
        # 不这么做的话：松手会发第二个 finish，而服务端那会儿正在 finish 收尾，
        # 第二个 finish 落在刚重开、还没收到音频的会话上 → 最后一段直接没了。
        self._end_pending = False
        # 分段期间「扣住」的音频 —— 这段属于**下一个**会话。
        # 见 feed() 的说明：直接发给正在收尾的旧会话会被服务端丢弃。
        self._audio_hold: list[bytes] = []
        self._hold_dropped = 0
        # 记住本次会话的 start 参数：断线重连后要**用同样的参数**重开会话，
        # 否则续传的音频会被服务端按默认参数自动开会话（语言提示、热词全丢）。
        self._session_args: dict = {"language": None, "context": ""}

    # -- 对外 -------------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="asr")
        self._thread.start()

    def begin_session(self, language: Optional[str] = None, context: str = "",
                      keep_end_pending: bool = False) -> None:
        """开一个会话（发 ``start``）。

        ``keep_end_pending=True`` 用于**分段后重开**：这时用户可能已经松手了
        （松手恰好发生在分段途中），必须保留「想结束」这个标记，
        否则新会话的 final 会被当成普通的一段，最后一段的内容就丢了。
        """
        self.session_open = True
        self._rotating = False
        if not keep_end_pending:
            self._end_pending = False
        self._session_args = {"language": language, "context": context}
        self._q.put(("start", {"type": "start", **self._session_args}))
        # 新会话起来了 → 把分段期间扣住的音频放出去。
        # 顺序很重要：start 必须**先于**这些音频发出去，
        # 否则服务端会按默认参数自动开会话（丢掉 language/context）。
        self._release_hold()
        # ⚠️ 用户可能**恰好在分段途中松了手**（end_session 发现正在轮转，
        # 就只记下「想结束」而没发 finish）。新会话现在刚起来、音频刚放出去，
        # 必须**补一个 finish**，否则新会话永远等不到 final ——
        # 最后一段的内容就这么没了（实测整段消失）。
        # 顺序：start → 音频 → finish。
        if self._end_pending:
            self._end_pending = False
            self._q.put(("finish", {"type": "finish"}))

    def feed(self, pcm: bytes) -> None:
        """由采集线程调用：只入队，绝不阻塞、绝不抛异常。

        ⚠️ **分段（rotate）期间要把音频「扣住」，不能发给旧会话。**
        实测踩过：对真实 WSL 服务跑一遍，7.7 秒音频分三段喂，
        对照组（一次性喂完）34 字，轮转组只有 23 字 —— **丢了 32%**。

        原因链（与服务端配合出的问题）：
        1. 轮转时我们发 ``finish``，服务端开始 flush（要几秒）；
        2. 这几秒里用户还在说话，``feed()`` 照常把音频推进队列；
        3. 服务端 flush 完把 session 置空，**随后收到��频会按默认参数
           自动开一个新会话**（用户的 language/context 全丢）；
        4. 我们重开会话时发的 ``start`` 又把这个自动会话**丢弃** ——
           那段音频的文本永远进不了 final。

        正确做法：分段期间攒进 ``_audio_hold``，等新会话 ``start`` 发出去之后再放行。
        """
        if not pcm:
            return
        if not self.session_open:
            return
        if self._rotating:
            self._hold_audio(bytes(pcm))
            return
        self._enqueue_audio(bytes(pcm))

    def _hold_audio(self, block: bytes) -> None:
        """把一块音频放进暂存区（分段期间用）。"""
        self._audio_hold.append(block)
        if len(self._audio_hold) > MAX_HOLD_BLOCKS:
            self._audio_hold.pop(0)
            self._hold_dropped += 1
            if self._hold_dropped == 1:
                # 只提醒一次，别刷屏
                self.on_error("分段期间音频积压过多，丢弃了 %d 个块（约 %.1f 秒）"
                              % (self._hold_dropped, self._hold_dropped * 0.16))

    def _release_hold(self) -> None:
        """新会话起来后，把扣住的音频按原顺序放回发送队列。"""
        if not self._audio_hold:
            return
        held, self._audio_hold = self._audio_hold, []
        for block in held:
            self._enqueue_audio(block)

    def _enqueue_audio(self, block: bytes) -> None:
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
        self._q.put(("audio", block))

    def end_session(self) -> None:
        """松手：请求最终结果。

        ⚠️ **分段在途中时不能再发第二个 finish。**
        实测踩过：服务端处理一次 finish 要几秒（等 R2T2 最终解码），
        这期间再发一个 finish 会落到**刚重开、还没收到音频**的会话上，
        最后一段的文本直接没了。
        正确做法是记下「想结束」，等这次分段的 final 回来时把它当成真正的结束。
        """
        if not self.session_open:
            return
        if self._rotating:
            self._end_pending = True
            return
        self.session_open = False
        self._rotating = False
        self._q.put(("finish", {"type": "finish"}))

    def cancel_session(self) -> None:
        self.session_open = False
        self._rotating = False
        self._end_pending = False
        self._audio_hold.clear()
        self._q.put(("cancel", {"type": "cancel"}))

    def rotate(self) -> None:
        """**分段重建会话**：结束当前段、拿到 final，然后立刻开新的一段。

        为什么需要：流式解码每块都要重新编码累积音频，成本随长度增长
        （实测 6.7 s 音频 0.58× 实时 → 33.7 s 0.88× → 53.9 s **1.15×**，
        也就是超过约 45 秒就跟不上实时了）。所以在到点之前主动切一段，
        让每一段都待在"比实时快"的区间里，把长说话拆成若干短会话。

        ⚠️ **一次只能有一个分段在途。** 重复调用会被忽略 ——
        服务端处理一次 finish 要几秒，这期间再发一次 finish 会把刚重开的
        会话又结束掉（实测「触发 3 次只完成 1 次」，中间内容直接丢了）。
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
                        # ⚠️ 必须先把队列里**残留的 start 帧**丢掉，否则会发出两个 start。
                        # 两个 start 的后果：服务端收到第二个会「重开会话并丢弃旧会话」
                        # （还会先 flush 出一个空 final）—— 刚送到的音频全白费，
                        # 实测服务端 chunks=0、识别结果为空。
                        #
                        # 什么时候会残留？连接建立要十几毫秒，这期间用户按下热键，
                        # ``begin_session`` 就往队列里塞了一个 start；连接一建立，
                        # 这里又补发一个 → 两个。
                        self._drop_pending_starts()
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

    def _drop_pending_starts(self) -> None:
        """把发送队列里**待发的 start 帧**丢掉（保留其它帧的相对顺序）。

        为什么需要：连接建立时如果 ``session_open`` 为真，我们会**补发**一个
        start（为了让重连后的会话沿用原来的 language/context）。
        但队列里可能还留着上一个连接排进去的 start —— 两者叠在一起就发出两个
        start，服务端收到第二个会「重开会话并丢弃旧会话」，刚送到的音频全废。

        ``queue.Queue`` 没有「过滤」操作，所以取出来再放回去。
        """
        kept: list[tuple[str, object]] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[0] != "start":
                kept.append(item)
        for item in kept:
            self._q.put(item)

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
            elif mtype == "cancelled":
                # 服务端确认「本次会话已丢弃」。
                # 不处理的话，session_open 会一直停在 True，
                # 下次按热键会被 app 的「已在录音中」挡掉。
                self.session_open = False
                self._rotating = False
                self._end_pending = False
                self._audio_hold.clear()
            elif mtype == "final":
                if self._rotating:
                    # 分段收尾：这一段的结果要**追加**到前缀，不是结束。
                    self._rotating = False
                    self.on_segment_final(msg)
                    # ⚠️ 这里**不能**因为 _end_pending 就把这次 final 当成结束 ——
                    # 新会话刚起来，它的音频还在路上。
                    # 正确的结束点是**新会话**的 final（那时才是最后一段）。
                    # _end_pending 的作用是：新会话的 final 到来时按「结束」处理，
                    # 而不是按「又一段」处理。
                elif self._end_pending:
                    # 新会话的 final：这是整个说话的最后一段。
                    self._end_pending = False
                    self.session_open = False
                    self.on_final(msg)
                else:
                    self.session_open = False
                    self.on_final(msg)
            elif mtype == "error":
                self.on_error("服务端错误：%s" % msg.get("message", ""))
            elif mtype == "ready":
                self.on_status("ready")
