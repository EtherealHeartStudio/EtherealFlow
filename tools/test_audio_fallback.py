# -*- coding: utf-8 -*-
"""验证音频采集的**降级路径**（此前从未被跑到过）。

`MicCapture` 有两级：优先按 16 kHz 打开；打不开就退到设备默认采样率、
由软件重采样到 16 kHz。本机所有设备都能直接给 16 kHz，所以**降级分支一直是死代码** ——
但用户的机器上很可能走到（Windows 上很多设备只报 44.1k/48k）。

这里用确定性方式把它跑起来：

* `_resample` 的频率与长度是否正确（正弦波的过零点数）；
* `read_wav_pcm16k` 对 8 kHz / 立体声 WAV 的处理；
* `MicCapture` 在 48 kHz + 软件重采样下，**是否仍然按 160 ms 吐出定长块**，
  以及 gain、`flush()` 尾巴是否正确。

退出码 0 = 全部通过。
"""

from __future__ import annotations

import math
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.audio import MicCapture, _resample, read_wav_pcm16k  # noqa: E402

TARGET = 16000


def tone(freq: float, seconds: float, rate: int, amp: float = 0.5) -> np.ndarray:
    n = int(rate * seconds)
    t = np.arange(n, dtype=np.float64) / rate
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def dominant_freq(x: np.ndarray, rate: int) -> float:
    """用过零点数估频率 —— 不引入 numpy.fft 之外的依赖，够用。"""
    sign = np.signbit(x)
    crossings = int(np.count_nonzero(sign[1:] != sign[:-1]))
    return crossings * rate / (2.0 * x.size)


def write_wav(path: Path, samples: np.ndarray, rate: int, channels: int = 1) -> None:
    data = np.clip(samples, -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)
    if channels > 1:
        pcm = np.repeat(pcm, channels)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


def collect_blocks(rate: int, seconds: float, chunk_ms: int = 160,
                   gain: float = 1.0) -> list[bytes]:
    """不开真设备，直接驱动 MicCapture 的降级路径。"""
    mic = MicCapture(device=None, chunk_ms=chunk_ms, gain=gain)
    mic.capture_rate = rate
    mic.resampling = rate != TARGET
    blocks: list[bytes] = []
    mic.on_block = blocks.append
    mic._stop.clear()                                   # noqa: SLF001
    worker = threading.Thread(target=mic._run, daemon=True)   # noqa: SLF001
    worker.start()

    # 故意用**不规则**的分片喂进去，模拟音频回调的真实块长
    src = tone(440.0, seconds, rate)
    pos, sizes, i = 0, [1024, 333, 4096, 777, 2048], 0
    while pos < src.size:
        step = sizes[i % len(sizes)]
        i += 1
        mic._raw.put(src[pos:pos + step].tobytes())     # noqa: SLF001
        pos += step
        time.sleep(0.002)
    time.sleep(0.4)
    mic._stop.set()                                     # noqa: SLF001
    worker.join(timeout=3)
    mic.flush()
    return blocks


def main() -> int:
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    # ---- 1) 重采样本身 ----
    for src_rate in (48000, 44100, 8000):
        x = tone(440.0, 1.0, src_rate)
        y = _resample(x, src_rate, TARGET)
        expect = int(round(x.size * TARGET / src_rate))
        freq = dominant_freq(y, TARGET)
        ok = abs(y.size - expect) <= 2 and abs(freq - 440.0) < 25
        check("重采样 %d → 16000：长度与频率都对" % src_rate, ok,
              "长度 %d（期望 %d）频率 %.1f Hz" % (y.size, expect, freq))

    # ---- 2) read_wav_pcm16k：8 kHz 单声道 / 44.1 kHz 立体声 ----
    tmp = Path(tempfile.gettempdir())
    p8 = tmp / "cv-8k.wav"
    write_wav(p8, tone(440.0, 1.0, 8000), 8000)
    pcm = read_wav_pcm16k(str(p8))
    n = len(pcm) // 2
    check("read_wav_pcm16k：8 kHz 单声道升到 16 kHz", abs(n - TARGET) <= 2,
          "%d 采样（期望约 %d）" % (n, TARGET))

    p44 = tmp / "cv-44k-stereo.wav"
    write_wav(p44, tone(440.0, 1.0, 44100), 44100, channels=2)
    pcm2 = read_wav_pcm16k(str(p44))
    n2 = len(pcm2) // 2
    check("read_wav_pcm16k：44.1 kHz 立体声混单 + 重采样", abs(n2 - TARGET) <= 2,
          "%d 采样（期望约 %d）" % (n2, TARGET))

    # ---- 3) 降级路径下的定长分块 ----
    for rate in (48000, 44100):
        blocks = collect_blocks(rate, 1.0)
        sizes = {len(b) // 2 for b in blocks[:-1]} if len(blocks) > 1 else set()
        total = sum(len(b) // 2 for b in blocks)
        # 除了最后一块（尾巴），每块都必须是 2560 采样
        all_fixed = sizes == {2560}
        check("%d Hz 降级路径仍按 160 ms 吐定长块" % rate,
              all_fixed and len(blocks) >= 5,
              "块数=%d 除尾块外长度集合=%s 总采样=%d"
              % (len(blocks), sizes or "（无）", total))

    # ---- 4) gain 生效 ----
    quiet = collect_blocks(48000, 0.5, gain=0.5)
    loud = collect_blocks(48000, 0.5, gain=1.0)
    def peak(bs: list[bytes]) -> float:
        if not bs:
            return 0.0
        arr = np.concatenate([np.frombuffer(b, dtype=np.int16) for b in bs])
        return float(np.max(np.abs(arr))) / 32768.0
    pq, pl = peak(quiet), peak(loud)
    check("gain 参数生效（0.5 倍的峰值约为 1 倍的一半）",
          pl > 0.1 and 0.35 < (pq / pl if pl else 0) < 0.65,
          "gain0.5 峰值=%.3f gain1.0 峰值=%.3f" % (pq, pl))

    # ---- 5) flush 把不足一块的尾巴也吐出去 ----
    mic = MicCapture(device=None, chunk_ms=160)
    mic.capture_rate = TARGET
    mic.resampling = False
    got: list[int] = []
    mic.on_block = lambda b: got.append(len(b) // 2)
    mic._buf = tone(440.0, 0.05, TARGET)     # 只有 800 采样 < 2560        # noqa: SLF001
    mic.flush()
    check("flush() 把不足一块的尾巴也发出去", got == [800], "发出块长=%s" % got)

    print("=" * 74)
    print("验证：音频采集降级路径（设备不支持 16 kHz 时）")
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
