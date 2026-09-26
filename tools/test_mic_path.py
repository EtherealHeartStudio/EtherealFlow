# -*- coding: utf-8 -*-
"""EtherealFlow · 验证**真实音频采集链路**（MicCapture → 识别服务）。

两种模式，自动选择：

* **回环模式**（首选）：本机装了 VB-Audio 虚拟声卡时，
  把测试音频播放到 ``CABLE Input``，再从 ``CABLE Output`` 采集回来 ——
  这样麦克风采集走的是**真正的 Windows 音频栈**，而且不需要人对着麦克风说话。
* **管道模式**（退化）：找不到虚拟声卡时，只打开默认麦克风录 2 秒，
  验证"设备能开、块能按 160 ms 稳定到达、电平读数正常"。
  这种模式**不验证识别内容**，会在结论里如实标注。

退出码 0 = 通过。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.asr import AsrClient                      # noqa: E402
from client.audio import MicCapture, _resample, read_wav_pcm16k  # noqa: E402


def find_device(name_part: str, kind: str) -> tuple[int, dict] | tuple[None, None]:
    for i, d in enumerate(sd.query_devices()):
        ok = (d["max_input_channels"] > 0 if kind == "input"
              else d["max_output_channels"] > 0)
        if ok and name_part.lower() in d["name"].lower():
            return i, d
    return None, None


def play_pcm(pcm: bytes, device: int, stop: threading.Event) -> None:
    """把 int16 PCM 按实时速度播到指定输出设备（必要时重采样）。"""
    info = sd.query_devices(device)
    rate = int(info["default_samplerate"])
    data = np.frombuffer(pcm, dtype=np.int16)
    if rate != 16000:
        f = _resample(data.astype(np.float32), 16000, rate)
        data = np.clip(f, -32768, 32767).astype(np.int16)
    block = int(rate * 0.02)                      # 20 ms
    with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                            device=device) as stream:
        for off in range(0, data.size, block):
            if stop.is_set():
                break
            stream.write(data[off:off + block].tobytes())
    time.sleep(0.3)                               # 让尾巴播完


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True)
    parser.add_argument("--url", default="ws://127.0.0.1:18300")
    parser.add_argument("--seconds", type=float, default=2.0,
                        help="管道模式下录多久")
    args = parser.parse_args()

    pcm = read_wav_pcm16k(args.wav)
    duration = len(pcm) / 2.0 / 16000.0
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    print("测试音频 %.2f 秒" % duration)
    play_idx, play_info = find_device("CABLE Input", "output")
    rec_idx, rec_info = find_device("CABLE Output", "input")

    captured: list[bytes] = []
    peaks: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    final: dict = {}
    got_final = threading.Event()

    def on_final(msg: dict) -> None:
        final.update(msg)
        got_final.set()

    # -- 识别会话（与 client/app.py 完全相同的路径） ---------------------- #
    asr = AsrClient(url=args.url, on_partial=lambda t: print("  partial: %s" % t),
                    on_final=on_final, on_error=errors.append)

    def on_block(b: bytes) -> None:
        """既本地统计，也**真的喂给识别服务** —— 否则等于没测识别。"""
        with lock:
            captured.append(b)
            arr = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0
            peaks.append(float(np.max(np.abs(arr))) if arr.size else 0.0)
        asr.feed(b)

    loopback = play_idx is not None and rec_idx is not None
    mode = "回环模式" if loopback else "管道模式"
    print("模式：%s" % mode)
    if loopback:
        print("  播放 → [%d] %s" % (play_idx, play_info["name"]))
        print("  采集 ← [%d] %s" % (rec_idx, rec_info["name"]))
        check("找到虚拟声卡回环对", True, "CABLE Input/Output")
        device, seconds = rec_idx, duration + 1.0
    else:
        check("找到虚拟声卡回环对", False,
              "未找到 VB-Audio 回环对，退化为管道模式（只验管道，不验内容）")
        device, seconds = None, args.seconds

    asr.start()
    time.sleep(0.6)
    # start 帧必须**先**于任何音频进队，否则服务端会先按默认参数自动开会话，
    # 随后收到 start 又把它丢掉重开（与 client/app.py 的顺序保持一致）。
    asr.begin_session(language=None, context="")

    mic = MicCapture(device=device, chunk_ms=160, on_block=on_block,
                     on_error=errors.append)
    try:
        mic.start()
    except Exception as exc:  # noqa: BLE001
        check("麦克风采集设备能打开", False, repr(exc))
        asr.close()
        return report(results)

    check("麦克风采集设备能打开", True,
          "device=%s rate=%d%s" % (device, mic.capture_rate,
                                   " 重采样" if mic.resampling else ""))

    stop = threading.Event()
    if loopback:
        threading.Thread(target=play_pcm, args=(pcm, play_idx, stop), daemon=True).start()

    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        time.sleep(0.1)
    mic.stop()
    stop.set()
    asr.end_session()

    elapsed = time.monotonic() - t0
    with lock:
        n = len(captured)
        total = sum(len(b) for b in captured) / 2.0 / 16000.0
        peak = max(peaks) if peaks else 0.0

    check("采集到的音频块数与时长符合预期",
          total >= seconds * 0.7,
          "%d 块 / %.2f 秒（墙壁 %.2f 秒），块长固定 160 ms"
          % (n, total, elapsed))
    check("采集到的音频不是全静音（电平正常）", peak > 0.005,
          "峰值 %.4f" % peak)

    if loopback:
        got = got_final.wait(timeout=30)
        check("识别服务返回了 final", got, "耗时 %.1f s" % (time.monotonic() - t0))
        text = final.get("text", "")
        check("回环识别结果非空", bool(text), repr(text))
    else:
        print("（管道模式不校验识别内容）")

    if errors:
        print("--- 采集/连接过程中的告警 ---")
        for e in errors[:6]:
            print("  %s" % e)
    asr.close()
    return report(results)


def report(results: list[tuple[str, bool, str]]) -> int:
    print("=" * 68)
    for name, ok, detail in results:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
    allok = all(ok for _n, ok, _d in results)
    print("-" * 68)
    print("结论：%s" % ("全部通过" if allok else "存在失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
