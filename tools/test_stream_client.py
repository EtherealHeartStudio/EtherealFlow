#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EtherealFlow · P1 验收客户端：把一份 WAV 推给流式识别服务，打印逐行 partial。

零第三方依赖（只用标准库），因此 **Windows 和 WSL 都能直接跑**，
同时也是验证 "Windows → WSL localhost 转发" 的工具。

用法::

    # 按实时速度推（模拟真实说话）
    python tools/test_stream_client.py --wav resources/test16k.wav

    # 全速推（测吞吐上限）
    python tools/test_stream_client.py --wav resources/test16k.wav --fast

    # 指定其它地址 / 语言 / 热词
    python tools/test_stream_client.py --wav a.wav --url ws://127.0.0.1:18300 \
        --language Chinese --context "EtherealFlow,Confucius4"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import wave

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


# --------------------------------------------------------------------------- #
# 极简 WebSocket 客户端（够用就好：文本/二进制/分片/ping-pong/close）
# --------------------------------------------------------------------------- #

class WSClient:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        if not url.startswith("ws://"):
            raise ValueError("只支持 ws:// 地址：%s" % url)
        rest = url[len("ws://"):]
        slash = rest.find("/")
        hostport, path = (rest, "/") if slash < 0 else (rest[:slash], rest[slash:])
        host, _, port = hostport.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 80)
        self.path = path or "/"
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = bytearray()

    # -- 连接 -------------------------------------------------------------- #

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (self.path, self.host, self.port, key)
        self.sock.sendall(req.encode())

        head = bytearray()
        while b"\r\n\r\n" not in head:
            piece = self.sock.recv(4096)
            if not piece:
                raise ConnectionError("握手时连接被关闭")
            head += piece
        raw = bytes(head)
        header, _, tail = raw.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise ConnectionError("WebSocket 握手失败：%s" % status)
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if ("Sec-WebSocket-Accept: " + expect).lower() not in header.decode("latin-1").lower():
            raise ConnectionError("Sec-WebSocket-Accept 校验失败")
        self._buf = bytearray(tail)

    # -- 收发 -------------------------------------------------------------- #

    def send(self, payload: bytes, opcode: int = OP_TEXT) -> None:
        assert self.sock is not None
        n = len(payload)
        frame = bytearray([0x80 | opcode])
        if n < 126:
            frame.append(0x80 | n)
        elif n < (1 << 16):
            frame.append(0x80 | 126)
            frame += struct.pack("!H", n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack("!Q", n)
        mask = os.urandom(4)
        frame += mask
        frame += bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def send_json(self, obj: dict) -> None:
        self.send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), OP_TEXT)

    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        while len(self._buf) < n:
            piece = self.sock.recv(max(4096, n - len(self._buf)))
            if not piece:
                raise ConnectionError("连接被对端关闭")
            self._buf += piece
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv(self) -> tuple[int, bytes]:
        """返回 (opcode, payload)；跳过 ping/pong，close 抛 ConnectionError。"""
        data = bytearray()
        opcode = None
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if masked:
                mask = self._read_exact(4)
                payload = bytes(c ^ mask[i & 3] for i, c in enumerate(self._read_exact(length)))
            else:
                payload = self._read_exact(length)

            if op == OP_PING:
                self.send(payload, OP_PONG)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise ConnectionError("服务端关闭连接（code=%s）"
                                      % (struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else "?"))
            if op == OP_CONT:
                data += payload
            else:
                opcode, data = op, bytearray(payload)
            if fin:
                return int(opcode or OP_TEXT), bytes(data)

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.send(b"", OP_CLOSE)
                self.sock.close()
        except OSError:
            pass
        self.sock = None


# --------------------------------------------------------------------------- #
# WAV -> 16 kHz / 单声道 / int16（纯标准库，必要时线性重采样）
# --------------------------------------------------------------------------- #

def load_wav_as_pcm16k(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        channels, width, rate, frames = (
            wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes())
        raw = wf.readframes(frames)
    if width != 2:
        raise SystemExit("只支持 16-bit PCM WAV，当前 sampwidth=%d" % width)
    samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    if channels > 1:  # 混成单声道
        samples = [sum(samples[i:i + channels]) // channels
                   for i in range(0, len(samples) - channels + 1, channels)]
    if rate != 16000:  # 线性重采样
        duration = len(samples) / float(rate)
        n_out = int(round(duration * 16000))
        out = []
        for i in range(n_out):
            pos = i * (len(samples) - 1) / float(max(n_out - 1, 1))
            lo = int(pos)
            hi = min(lo + 1, len(samples) - 1)
            frac = pos - lo
            out.append(int(samples[lo] * (1 - frac) + samples[hi] * frac))
        samples = out
    return struct.pack("<%dh" % len(samples), *samples)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="EtherealFlow 流式识别服务验收客户端")
    parser.add_argument("--wav", required=True, help="输入 WAV（任意采样率/声道，会转成 16k 单声道）")
    parser.add_argument("--url", default="ws://127.0.0.1:18300")
    parser.add_argument("--language", default=None, help="自动/Chinese/English；留空=自动")
    parser.add_argument("--context", default="", help="热词，逗号分隔")
    parser.add_argument("--chunk-ms", type=int, default=160)
    parser.add_argument("--fast", action="store_true", help="全速推送，不按实时节奏")
    args = parser.parse_args()

    pcm = load_wav_as_pcm16k(args.wav)
    duration = len(pcm) / 2.0 / 16000.0
    chunk = max(2, int(args.chunk_ms * 16))  # 每毫秒 16 采样 × 2 字节
    print("音频 %.2f s | 块 %d ms | %s" % (duration, args.chunk_ms, args.url), flush=True)

    client = WSClient(args.url)
    t_conn = time.monotonic()
    client.connect()
    print("已连接（%.0f ms）" % ((time.monotonic() - t_conn) * 1000), flush=True)

    op, payload = client.recv()
    print("服务端: %s" % payload.decode("utf-8", "replace"), flush=True)

    client.send_json({"type": "start", "language": args.language, "context": args.context})

    t0 = time.monotonic()
    first_text_at = None
    partials = 0
    sent = 0
    try:
        for off in range(0, len(pcm), chunk):
            client.send(pcm[off:off + chunk], OP_BIN)
            sent += min(chunk, len(pcm) - off)
            while True:  # 非阻塞地取走已经到的消息
                try:
                    client.sock.settimeout(0.001)  # type: ignore[union-attr]
                    op, payload = client.recv()
                except (socket.timeout, TimeoutError):
                    break
                finally:
                    client.sock.settimeout(client.timeout)  # type: ignore[union-attr]
                msg = json.loads(payload.decode("utf-8", "replace"))
                if msg.get("type") == "partial":
                    partials += 1
                    if first_text_at is None:
                        first_text_at = time.monotonic() - t0
                    print("[%6.2fs] partial: %s" % (time.monotonic() - t0, msg["text"]), flush=True)
                else:
                    print("[%6.2fs] %s" % (time.monotonic() - t0, msg), flush=True)

            if not args.fast:
                target = t0 + (off + chunk) / 2.0 / 16000.0
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        print("推送完成：%.2f s 音频，%d 字节" % (sent / 2.0 / 16000.0, sent), flush=True)
        client.send_json({"type": "finish"})
        while True:
            op, payload = client.recv()
            msg = json.loads(payload.decode("utf-8", "replace"))
            if msg.get("type") == "final":
                print("=" * 60, flush=True)
                print("FINAL: %s" % msg.get("text", ""), flush=True)
                if first_text_at is not None:
                    print("首字延迟 %.2f s | partial %d 条 | 端到端 %.2f s"
                          % (first_text_at, partials, time.monotonic() - t0), flush=True)
                print("统计: %s" % {k: v for k, v in msg.items() if k not in ("type", "text")},
                      flush=True)
                return 0
            print("[%6.2fs] %s" % (time.monotonic() - t0, msg), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
