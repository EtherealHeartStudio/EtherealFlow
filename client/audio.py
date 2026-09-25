# -*- coding: utf-8 -*-
"""CherryVoice · 麦克风采集：16 kHz / 单声道 / int16，按固定块长回调。

需求文档 FR-2：16000 Hz、单声道、16-bit PCM、每 160 ms 一块。

**踩过的坑**：Windows 上很多设备只接受 44.1k/48k，直接要 16000 会打不开流。
所以这里先试 16 kHz，失败就退回到设备默认采样率并用线性插值重采样到 16 kHz，
并把实际走的是哪条路记下来，方便排查。
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

TARGET_RATE = 16000


def list_input_devices() -> list[tuple[int, str]]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append((i, d["name"]))
    return out


def default_input_device() -> Optional[int]:
    try:
        idx = sd.default.device[0]
        return int(idx) if idx is not None and int(idx) >= 0 else None
    except Exception:  # noqa: BLE001
        return None


def _resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or x.size == 0:
        return x
    n_out = int(round(x.size * dst_rate / float(src_rate)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    pos = np.linspace(0.0, x.size - 1, num=n_out, dtype=np.float64)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = (pos - lo).astype(np.float32)
    return (x[lo] * (1.0 - frac) + x[hi] * frac).astype(np.float32)


def read_wav_pcm16k(path: str) -> bytes:
    """读一份 WAV，返回 16 kHz / 单声道 / int16 的裸 PCM（给回放和自测用）。"""
    import struct
    import wave

    with wave.open(path, "rb") as wf:
        channels, width, rate, frames = (wf.getnchannels(), wf.getsampwidth(),
                                         wf.getframerate(), wf.getnframes())
        raw = wf.readframes(frames)
    if width != 2:
        raise ValueError("只支持 16-bit PCM WAV，当前 sampwidth=%d" % width)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        samples = _resample(samples, rate, TARGET_RATE)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class MicCapture:
    """按 ``chunk_ms`` 吐出 int16 PCM 字节块。

    ``on_block(bytes)`` 在**独立线程**里被调用，不要在里面做阻塞的重活。
    """

    def __init__(self, device: Optional[int | str] = None, chunk_ms: int = 160,
                 gain: float = 1.0, on_block: Optional[Callable[[bytes], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self.device = device
        self.chunk_ms = int(chunk_ms)
        self.gain = float(gain)
        self.on_block = on_block
        self.on_error = on_error or (lambda _m: None)

        self.chunk_samples = int(round(TARGET_RATE * self.chunk_ms / 1000.0))
        self.capture_rate = TARGET_RATE
        self.resampling = False

        self._stream: Optional[sd.InputStream] = None
        self._raw: "queue.Queue[bytes]" = queue.Queue()
        self._buf = np.zeros(0, dtype=np.float32)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.running = False
        self.peak = 0.0                 # 最近一块的峰值，供音量波形用

    # -- 生命周期 ---------------------------------------------------------- #

    def _open(self, rate: int) -> sd.InputStream:
        return sd.InputStream(device=self.device, channels=1, samplerate=rate,
                              dtype="float32", blocksize=0,
                              callback=self._callback)

    def start(self) -> None:
        try:
            self._stream = self._open(TARGET_RATE)
            self.capture_rate, self.resampling = TARGET_RATE, False
        except Exception as exc:  # noqa: BLE001  PortAudio 不支持 16k
            self.on_error("16 kHz 打不开（%s），改用设备默认采样率并重采样" % exc)
            info = sd.query_devices(self.device, "input")
            rate = int(info["default_samplerate"])
            self._stream = self._open(rate)
            self.capture_rate, self.resampling = rate, True

        self._buf = np.zeros(0, dtype=np.float32)
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        self._stream.start()
        self.running = True

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    # -- 内部 -------------------------------------------------------------- #

    def _callback(self, indata, _frames, _time, status) -> None:  # noqa: ANN001
        if status:
            self.on_error("音频回调状态：%s" % status)
        self._raw.put(bytes(indata.tobytes()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._raw.get(timeout=0.2)
            except queue.Empty:
                continue
            block = np.frombuffer(raw, dtype=np.float32)
            if self.resampling:
                block = _resample(block, self.capture_rate, TARGET_RATE)
            if self.gain != 1.0:
                block = block * self.gain
            self._buf = np.concatenate([self._buf, block]) if self._buf.size else block
            while self._buf.size >= self.chunk_samples:
                piece = self._buf[:self.chunk_samples]
                self._buf = self._buf[self.chunk_samples:]
                self._emit(piece)

    def _emit(self, piece: np.ndarray) -> None:
        clipped = np.clip(piece, -1.0, 1.0)
        self.peak = float(np.max(np.abs(clipped))) if clipped.size else 0.0
        pcm = (clipped * 32767.0).astype(np.int16)
        try:
            self.on_block(pcm.tobytes())
        except Exception as exc:  # noqa: BLE001
            self.on_error("音频回调异常：%r" % exc)

    def flush(self) -> Optional[bytes]:
        """把不足一块的尾巴也吐出去（松开热键时调用）。"""
        if self._buf.size == 0:
            return None
        piece, self._buf = self._buf, np.zeros(0, dtype=np.float32)
        self._emit(piece)
        return None
