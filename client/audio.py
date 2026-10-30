# -*- coding: utf-8 -*-
"""EtherealFlow · 麦克风采集：16 kHz / 单声道 / int16，按固定块长回调。

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


def _name_tokens(name: str) -> set[str]:
    """把设备名拆成「有意义的小写词」。

    ``"Microphone (Realtek Audio)"`` → ``{"microphone", "realtek", "audio"}``

    为什么要拆词而不是直接比子串 —— 同一个物理设备在不同机器/不同时刻
    上报的**名字会重排**：``"Realtek Microphone"`` 与
    ``"Microphone (Realtek Audio)"`` 互相都不是子串，但词集合高度重合。
    """
    import re
    raw = re.split(r"[^0-9a-zA-Z一-鿿]+", (name or "").lower())
    # 去掉没有信息量的词：它们在每个设备名里都出现
    stop = {"audio", "device", "input", "output", "mic", "microphone",
            "the", "of", "and", "on", "usb", "设备", "麦克风", "输入", "输出"}
    return {t for t in raw if t and t not in stop}


def _names_match(a: str, b: str) -> bool:
    """两个设备名是不是指同一个设备。

    三级判定，从严到宽：
    1. 完全相等；
    2. 一方是另一方的子串（``"Realtek Mic"`` vs ``"Realtek Microphone"``）；
    3. 词集合有交集且一方包含另一方（**处理词序变化**：
       ``"Realtek Microphone"`` vs ``"Microphone (Realtek Audio)"``）。

    第 3 条要求**包含**而不是只要有交集 —— 否则 ``"USB Mic"`` 会和
    ``"USB Speaker"`` 误判成同一个设备。
    """
    a_s = (a or "").strip().lower()
    b_s = (b or "").strip().lower()
    if not a_s or not b_s:
        return False
    if a_s == b_s:
        return True
    if a_s in b_s or b_s in a_s:
        return True
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    return ta.issubset(tb) or tb.issubset(ta)


#: 对外名字（settings_ui 也用同一套判据 ——
#: 界面认得出设备、运行时却认不出的话，用户选的是一个、用的另一个）。
matches_device_name = _names_match


def resolve_input_device(index: Optional[int] = None,
                         name: str = "") -> Optional[int]:
    """按「名称优先、序号兜底」解析出实际要用的输入设备序号。

    **为什么不能只存序号** —— 设备序号会漂移：
    插拔一次 USB 声卡、换个蓝牙耳机、系统的默认设备变了，
    同一个物理麦克风的序号就可能变。EtherealLive 的 P9 计划里明确记了这个坑
    （「同一支麦在重启后换了序号」）。

    所以配置里存**两个字段**：``audio.device``（序号）和
    ``audio.device_name``（名称）。启动时：
    1. 名称能对上 → 用当前的序号（跟着名字走，序号变了也不怕）；
    2. 名称对不上（设备拔了/换了）→ 退回存下来的序号；
    3. 都没有 → 用系统默认设备。

    名称匹配不要求完全相等 —— 不同机器上报的设备名常带前后缀差异或
    词序变化（``Microphone (Realtek Audio)`` vs ``Realtek Microphone``），
    详见 ``_names_match``。
    """
    devices = list_input_devices()
    if not devices:
        # 一台输入设备都没有（CI runner、无声卡的虚拟机）。
        # 这时**不能**去校验序号 —— 没有表可对。存下来的序号直接用，
        # 让 sounddevice 自己去报错（那才是真实的失败）。
        return index

    wanted = (name or "").strip()
    if wanted:
        # 第一轮：完全相等（最严格，最不会认错）
        for idx, dev_name in devices:
            if dev_name == wanted:
                return idx
        # 第二轮：模糊匹配（子串 / 词集合包含）
        for idx, dev_name in devices:
            if _names_match(wanted, dev_name):
                return idx

    # 名称对不上 → 退回存下来的序号。
    #
    # ⚠️ 有效性判断必须用**同一张表**（``devices``，也就是
    # ``list_input_devices()``），不能拿 ``len(sd.query_devices())`` 去比。
    # ``sd.query_devices()`` 包含只有输出端口的设备（扬声器等），
    # 序号空间比输入设备表**大**；反过来在 CI runner 上它又可能**更小**
    # （无声卡 → 只剩一两个虚拟设备）。
    # 两种情况下都会判错：
    #   · 本机：序号 20 可能落在「扬声器」那一段 → 越界 → 明明有这设备却退回默认
    #   · CI：序号 1 > 总数 → 越界 → 明明列表里有 1 号设备却退回默认
    # （实测：CI 上这条把两条用例打红，本机因设备多而看不出来。）
    if index is not None and any(idx == index for idx, _ in devices):
        return index
    return default_input_device()


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
        # ⚠️ **必须 flush**：采集是按整块（160 ms）切分的，
        # 用户松手时缓冲区里往往还剩**不足一块**的尾巴 ——
        # 不吐出去就等于每次都丢掉最后那点音（实测最多 160 ms）。
        # flush 要在 worker 退出**之后**做：那时不会再有新的数据进来，
        # 也不存在与 worker 争抢 _buf 的问题。
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - 尾巴丢了也不能影响收尾
            pass

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
