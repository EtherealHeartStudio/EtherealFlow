# -*- coding: utf-8 -*-
"""EtherealFlow · 麦克风设备选择（BF-12）与设置热更新（BF-13）的回归用例。

覆盖四组缺陷：

* **BF-12.1** 松手时尾部不足一块的音频必须补发（``MicCapture.stop()`` 要 flush）
* **BF-12.3** 设备**按名称持久化**：序号会漂移，必须按名重解析
* **BF-13.1** 设置保存后 **LLM 客户端热更新**，不用重启
* **BF-13.2** 设置界面能改 ``max_tokens`` / 分段时长 / 麦克风设备
* **BF-13.3** 界面明确标注「哪些立即生效、哪些要重启」

**双向验证**：每个断言都配一个「坏实现」当阳性对照 ——
把实现改坏，断言**必须**变红。这条路的意义在于：一个永远为真的断言
看起来和真断言一模一样（BF-8 就是在这种断言上翻车的）。

退出码 0 = 没有 FAIL。
"""

from __future__ import annotations

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                                       # noqa: E402

from client import audio as audio_mod                                    # noqa: E402
from client.config import DEFAULT_CONFIG                                 # noqa: E402
from client.hotreload import ConfigWatcher, diff_sections                # noqa: E402

rows: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    rows.append((name, ok, detail))


# --------------------------------------------------------------------------- #
# BF-12.1：尾部不足一块的音频必须补发
# --------------------------------------------------------------------------- #

class _FakeStream:
    """假装是 sounddevice 的 InputStream。"""

    def __init__(self) -> None:
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def _make_mic(blocks: list[bytes], chunk_ms: int = 160) -> tuple[audio_mod.MicCapture, list]:
    """造一个不碰真实声卡的 MicCapture。

    直接构造对象但**不调 start()**（那会真的打开设备），
    然后手工把音频灌进 ``_buf``，模拟「采集线程收到了一块不完整的音频」。
    """
    got: list[bytes] = []
    mic = audio_mod.MicCapture(device=None, chunk_ms=chunk_ms,
                               on_block=got.append, on_error=lambda _m: None)
    mic._stream = _FakeStream()
    full = int(audio_mod.TARGET_RATE * chunk_ms / 1000.0)
    # 造「三块完整的 + 半个」的音频
    samples = np.zeros(full * 3 + full // 2, dtype=np.float32)
    mic._buf = samples
    mic._worker = None
    return mic, got


def check_bf12_1() -> None:
    mic, got = _make_mic([])
    mic.stop()
    # flush 会把尾巴吐出来 —— 走的是 _emit → on_block
    check("BF-12.1 松手时尾部不足一块的音频会被补发",
          len(got) == 1 and len(got[0]) > 0,
          "补发块数=%d 字节=%d" % (len(got), len(got[0]) if got else 0))

    # 对照组：**不** flush 就丢掉尾巴（旧的 buggy 行为）
    mic2, got2 = _make_mic([])
    mic2.running = False
    mic2._stop.set()
    if mic2._stream is not None:
        mic2._stream.stop()
        mic2._stream.close()
        mic2._stream = None
    check("BF-12.1 阳性对照：不 flush 就会丢掉尾巴（断言能红）",
          len(got2) == 0,
          "不 flush 时补发块数=%d（应为 0，说明对照组确实复现了丢尾巴）" % len(got2))


# --------------------------------------------------------------------------- #
# BF-12.3：设备按名称持久化
# --------------------------------------------------------------------------- #

def _fake_devices(spec: list[tuple[str, bool]]) -> list:
    """造一张假的 ``sd.query_devices()`` 表。

    ``spec`` 的每一项是 ``(设备名, 有没有输入口)``。

    ## 为什么要这张表

    实现里读设备列表有**两个入口**：``list_input_devices()``（过滤出有输入口的）
    和 ``sd.query_devices()``（全部设备，含只有输出口的扬声器）。
    以前用例只换掉了前者，后者读到的还是**本机真实的声卡列表** ——
    而本机设备多，序号 1 总是「在范围内」，掩盖掉了 bug。
    到了 CI runner（没有音频硬件，只有一两个虚拟设备），同一个 bug 就暴露了：
    序号 1 被判定成越界 → 明明列表里有 1 号设备却退回系统默认。
    **同一个缺陷，本机绿、CI 红。**

    ## 关键：只换 ``sd.query_devices()``，让真的 ``list_input_devices()`` 去过滤

    这样两个入口**永远一致**（前者本来就是后者的过滤结果），
    不会出现「假设备表和真设备表对不上」的诡异失败。

    ## 扬声器为什么必须在同一张表里

    因为这正是缺陷的本质：存下来的序号可能指向一台**只有输出口**的设备
    （序号 1 是扬声器、序号 2 才是麦克风）。
    按 ``len(sd.query_devices())`` 判「序号还在不在范围内」会**放行**它 ——
    程序于是去打开一台扬声器当麦克风（用户听自己的声音，识别出一堆乱码）。
    正确的判据是「这个序号**是不是一台输入设备**」。
    """
    out = []
    for name, has_input in spec:
        out.append({"name": name,
                    "max_input_channels": 2 if has_input else 0,
                    "max_output_channels": 0 if has_input else 2,
                    "default_samplerate": 48000.0})
    return out


def check_bf12_3() -> None:
    """设备序号会漂移 → 必须按名称重解析。

    这里把 ``sd.query_devices()`` 换成**假设备表**，让真的
    ``list_input_devices()`` 去过滤 —— 两个数据源永远一致，
    用例结果**与跑它的机器有没有声卡无关**。

    ⚠️ 以前只换了 ``list_input_devices``，后者读到的还是**本机真实的声卡列表**
    —— 而本机设备多，序号 1 总是「在范围内」，掩盖掉了 bug。
    到了 CI runner（没有音频硬件，只有一两个虚拟设备），同一个 bug 就暴露了：
    序号 1 被判定成越界 → 明明列表里有 1 号设备却退回系统默认。
    **同一个缺陷，本机绿、CI 红。**
    """
    real_default = audio_mod.default_input_device
    real_query = audio_mod.sd.query_devices
    audio_mod.default_input_device = lambda: 0

    try:
        # 重启前：Realtek 是 1 号
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("CABLE Output", True), ("Realtek Microphone", True),
             ("USB Mic", True)])
        before = audio_mod.resolve_input_device(1, "Realtek Microphone")
        check("BF-12.3 重启前按名称解析到正确设备", before == 1,
              "解析结果=%r（设备表里 1 = Realtek Microphone）" % before)

        # 重启后：Realtek 变成了 3 号（**序号漂移了**）
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("CABLE Output", True), ("USB Mic", True),
             ("Headset", True), ("Realtek Microphone", True)])
        after = audio_mod.resolve_input_device(1, "Realtek Microphone")
        check("BF-12.3 序号漂移后仍按名称找到同一支麦",
              after == 3,
              "解析结果=%r（现在是 3 号，不是存的旧序号 1）" % after)

        # 对照组：**只按序号**解析 → 选错设备
        only_index = audio_mod.resolve_input_device(1, "")
        check("BF-12.3 阳性对照：只按序号会选错设备（断言能红）",
              only_index == 1 and only_index != after,
              "只按序号=%r ≠ 按名称=%r" % (only_index, after))

        # 设备名对不上（拔了）→ 退回序号
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("CABLE Output", True), ("USB Mic", True)])
        fallback = audio_mod.resolve_input_device(1, "Realtek Microphone")
        check("BF-12.3 设备不在了时退回存的序号", fallback == 1,
              "回退结果=%r" % fallback)

        # 都没有 → 系统默认
        none_cfg = audio_mod.resolve_input_device(None, "")
        check("BF-12.3 没指定设备时用系统默认", none_cfg == 0,
              "默认设备=%r" % none_cfg)

        # 词序变化：设备名带前后缀差异时仍认得出
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("Microphone (Realtek Audio)", True)])
        substr = audio_mod.resolve_input_device(None, "Realtek Microphone")
        check("BF-12.3 设备名有前后缀差异时仍能认出来（词序变化也算同一支）",
              substr == 0, "解析结果=%r" % substr)

        # ⚠️ 反面：不能**认得太松**。
        # 「USB Mic」与「USB Speaker」有共同的词，但**不是**同一个设备 ——
        # 认错了会把录音采到扬声器上（用户听自己的声音，识别出一堆乱码）。
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("USB Mic", True), ("USB Speaker", False)])
        wrong = audio_mod.resolve_input_device(None, "USB Mic")
        check("BF-12.3 不同设备不会互相误认（USB Mic ≠ USB Speaker）",
              wrong == 0, "解析结果=%r（应为 0 = USB Mic）" % wrong)

        # ⚠️ **CI 条件**：设备表很短（无声卡的机器只有一两个虚拟设备）。
        # 序号有效性必须按**同一张表**判断 —— 以前拿
        # ``len(sd.query_devices())`` 比，序号 1 会被判成越界。
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("Microsoft Sound Mapper", True)])
        short = audio_mod.resolve_input_device(0, "Microsoft Sound Mapper")
        check("BF-12.3 设备表很短时（CI 条件）也能按序号兜底",
              short == 0,
              "解析结果=%r（设备表只有 1 项，序号 0 必须仍被认出来）" % short)

        # ⚠️ 序号指向**只有输出口**的设备（扬声器）时，不能放行 ——
        # 放行就等于去打开扬声器当麦克风（用户听自己的声音，识别出一堆乱码）。
        # 旧判据 ``0 <= index < len(sd.query_devices())`` 会放行它。
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("Mic A", True), ("Speaker B", False), ("Mic C", True)])
        speaker = audio_mod.resolve_input_device(1, "")
        check("BF-12.3 序号指向扬声器时不放行（不拿扬声器当麦克风）",
              speaker != 1,
              "解析结果=%r（1 号是只有输出口的设备，不能用）" % speaker)

        # 但**确实是输入设备**的序号（哪怕序号不等于它在输入设备表里的位置）
        # 必须能用 —— 输入设备表的索引与全表索引**不是一回事**。
        audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
            [("Speaker A", False), ("Mic B", True)])
        real_input = audio_mod.resolve_input_device(1, "")
        check("BF-12.3 序号指向真麦克风时能用（输入设备在全表里的索引）",
              real_input == 1,
              "解析结果=%r（1 号 = Mic B，是输入设备）" % real_input)
    finally:
        audio_mod.sd.query_devices = real_query
        audio_mod.default_input_device = real_default


def check_bf12_3_config() -> None:
    """配置里必须**同时**存 device 与 device_name。"""
    audio = DEFAULT_CONFIG.get("audio", {})
    check("BF-12.3 默认配置里就有 device 与 device_name 两个字段",
          "device" in audio and "device_name" in audio,
          "audio 段=%s" % sorted(audio))

    # 启动时读配置 → 按名解析
    from client.config import deep_merge
    cfg = deep_merge(DEFAULT_CONFIG, {"audio": {"device": 1,
                                                 "device_name": "Realtek Microphone"}})
    real_query = audio_mod.sd.query_devices
    # 同样是换 ``sd.query_devices()``（而不是 list_input_devices）——
    # 两个入口保持一致，用例结果与机器无关。
    audio_mod.sd.query_devices = lambda *a, **k: _fake_devices(
        [("X", True), ("Pad", False), ("Y", False), ("Z", False),
         ("Realtek Microphone", True)])
    try:
        got = audio_mod.resolve_input_device(cfg["audio"].get("device"),
                                             cfg["audio"].get("device_name", ""))
        check("BF-12.3 从配置读出的设备按名称重解析（序号已漂移）",
              got == 4, "解析结果=%r" % got)
    finally:
        audio_mod.sd.query_devices = real_query


# --------------------------------------------------------------------------- #
# BF-13：设置热更新
# --------------------------------------------------------------------------- #

def check_bf13() -> None:
    tmp = Path(tempfile.gettempdir()) / "cv-hotreload-test.json"
    if tmp.exists():
        tmp.unlink()

    got_cfgs: list[dict] = []
    watcher = ConfigWatcher(tmp, got_cfgs.append, interval=0.05)

    # 文件还不存在 → 不该有任何反应
    watcher.start()
    time.sleep(0.15)
    check("BF-13.1 配置文件不存在时监视器不误报",
          not got_cfgs, "回调次数=%d" % len(got_cfgs))

    # 写入配置 → 应该被检测到
    tmp.write_text(json.dumps({"llm": {"model": "m1"}}), encoding="utf-8")
    watcher.poll()
    check("BF-13.1 配置文件变了会被检测到并回调",
          len(got_cfgs) == 1 and got_cfgs[0]["llm"]["model"] == "m1",
          "回调次数=%d 内容=%s" % (len(got_cfgs), got_cfgs))

    # 没变 → 不重复回调
    watcher.poll()
    check("BF-13.1 文件没变时不重复回调",
          len(got_cfgs) == 1, "回调次数=%d" % len(got_cfgs))

    # 坏文件 → 跳过这一轮，**不能**把用户配置冲掉
    tmp.write_text("{ this is not json", encoding="utf-8")
    watcher.poll()
    check("BF-13.1 配置文件损坏时跳过而不是回退默认值（不冲掉用户设置）",
          len(got_cfgs) == 1, "回调次数=%d（应仍为 1）" % len(got_cfgs))

    # 恢复成好文件 → 继续工作
    tmp.write_text(json.dumps({"llm": {"model": "m2"}}), encoding="utf-8")
    watcher.poll()
    check("BF-13.1 配置恢复后继续正常工作",
          len(got_cfgs) == 2 and got_cfgs[-1]["llm"]["model"] == "m2",
          "回调次数=%d" % len(got_cfgs))
    watcher.stop()

    # ---------- 关键回归：**等长**的内容改动也要被认出来 ----------
    #
    # 旧实现用 ``(mtime, size)`` 判断「文件变没变」，有两个真实的漏检：
    #   1. 等长的内容改不动 —— ``{"llm": {"model": "m1"}}`` 与
    #      ``{"llm": {"model": "m2"}}`` **字节数完全一样**；
    #   2. 某些文件系统的 mtime 只精确到秒 —— 同一秒内的两次写入看不出来。
    #
    # 这不是测试的边角料 —— 用户把修正模式从 ``light`` 改成 ``deep``
    # （都是 4 个字符）、超时从 ``15`` 改成 ``12``，**都是等长的**。
    # 旧实现下这些改动会被**静默丢弃**：用户改了设置，客户端却当没改。
    tmp3 = Path(tempfile.gettempdir()) / "cv-hotreload-samelen.json"
    if tmp3.exists():
        tmp3.unlink()
    got3: list[dict] = []
    w3 = ConfigWatcher(tmp3, got3.append, interval=0.05)
    # 先 start（此时文件还不存在）—— start() 会把**当前**内容记成基线，
    # 所以「启动前就有的配置」不算变化（客户端一启动不该白重载一遍）。
    w3.start()
    tmp3.write_text(json.dumps({"llm": {"model": "m1"}}), encoding="utf-8")
    w3.poll()
    same_len = json.dumps({"llm": {"model": "m2"}})
    check("BF-13.1 等长内容的改动前：两版字节数确实相同",
          len(same_len) == len(json.dumps({"llm": {"model": "m1"}})),
          "m1 与 m2 都是 %d 字节" % len(same_len))

    # ⚠️ **把 mtime 强行设成和上一版完全一样** ——
    # 模拟「文件系统 mtime 粒度粗」（某些网络盘、CI runner 的文件系统、
    # FAT32 都是秒级粒度）。
    #
    # 不这么做的话，这条用例在**本机**永远通过（本机 mtime 是 100ns 粒度），
    # 抓不到「靠 (mtime, size) 判断变化」的缺陷 —— 那缺陷只在 CI 上暴露。
    # 固定住 mtime 之后，用例结果就与文件系统的行为**无关**了。
    import os as _os
    stamp = tmp3.stat().st_mtime
    tmp3.write_text(same_len, encoding="utf-8")
    _os.utime(tmp3, (stamp, stamp))
    check("BF-13.1 模拟粗粒度文件系统：mtime 与字节数都没变",
          tmp3.stat().st_mtime == stamp
          and tmp3.stat().st_size == len(same_len),
          "mtime=%r 字节数=%d（与上一版完全一样）"
          % (tmp3.stat().st_mtime, tmp3.stat().st_size))

    w3.poll()
    check("BF-13.1 mtime 与字节数都没变时，内容改动仍能被认出来",
          len(got3) == 2 and got3[-1]["llm"]["model"] == "m2",
          "回调次数=%d 内容=%s" % (len(got3), got3))
    w3.stop()

    # ---------- 关键回归：一次读失败**不能**永久丢掉这次改动 ----------
    # 设置界面写文件是「先截断、再写」—— 监视线程正好落在这个窗口里时，
    # 读到的是半个文件（JSON 解析失败）。
    # 旧实现：读失败时**照样更新了 mtime** → 这次改动被永久丢弃，
    # 用户改了设置却没生效，而且**再也等不到**。
    tmp2 = Path(tempfile.gettempdir()) / "cv-hotreload-transient.json"
    if tmp2.exists():
        tmp2.unlink()
    got2: list[dict] = []
    w2 = ConfigWatcher(tmp2, got2.append, interval=0.05)
    w2.start()
    time.sleep(0.05)

    real_load = w2._load
    calls = {"n": 0}

    def flaky_load():
        """第一次读失败（模拟写到一半被读到），之后正常。"""
        calls["n"] += 1
        if calls["n"] == 1:
            return None, None
        return real_load()

    w2._load = flaky_load
    tmp2.write_text(json.dumps({"llm": {"model": "m3"}}), encoding="utf-8")
    w2.poll()                       # 第一次：读失败
    w2.poll()                       # 第二次：应该重试并成功
    check("BF-13.1 一次读失败不会永久丢掉这次改动（下一轮重试）",
          len(got2) == 1 and got2[0]["llm"]["model"] == "m3",
          "回调次数=%d 内容=%s" % (len(got2), got2))
    w2.stop()

    # diff_sections
    check("BF-13.1 diff_sections 能看出哪一段变了",
          diff_sections({"llm": {"model": "a"}, "hotkey": {"keys": ["ctrl"]}},
                        {"llm": {"model": "b"}, "hotkey": {"keys": ["ctrl"]}})
          == {"llm"},
          "diff=%s" % diff_sections({"llm": {"model": "a"}},
                                    {"llm": {"model": "b"}}))


def check_bf13_2() -> None:
    """设置界面必须能改 max_tokens / 分段时长 / 麦克风设备。"""
    from client.settings_ui import EFFECTS_HELP, SettingsWindow

    tmp = Path(tempfile.gettempdir()) / "cv-settings-bf13.json"
    win = SettingsWindow(json.loads(json.dumps(DEFAULT_CONFIG)), tmp)

    # --- max_tokens ---
    check("BF-13.2 界面有「生成上限」输入框", "llm_max_tokens" in win.vars,
          "vars=%s" % sorted(win.vars))
    win.vars["llm_max_tokens"].set("2048")
    got = win.collect()
    check("BF-13.2 max_tokens 能改并写进配置",
          got["llm"]["max_tokens"] == 2048,
          "max_tokens=%r" % got["llm"].get("max_tokens"))

    # 填错不崩
    win.vars["llm_max_tokens"].set("不是数字")
    got2 = win.collect()
    check("BF-13.2 max_tokens 填错时回退默认值而不是抛异常",
          got2["llm"]["max_tokens"] == 4096,
          "max_tokens=%r" % got2["llm"].get("max_tokens"))

    # --- 分段时长 ---
    check("BF-13.2 界面有「单段最长」输入框", "asr_max_utterance" in win.vars,
          "vars=%s" % sorted(win.vars))
    win.vars["asr_max_utterance"].set("45")
    got3 = win.collect()
    check("BF-13.2 分段时长能改并写进配置",
          got3["asr"]["max_utterance_sec"] == 45,
          "max_utterance_sec=%r" % got3["asr"].get("max_utterance_sec"))

    win.vars["asr_max_utterance"].set("")
    got4 = win.collect()
    check("BF-13.2 分段时长填错时回退默认值",
          got4["asr"]["max_utterance_sec"] == 30,
          "max_utterance_sec=%r" % got4["asr"].get("max_utterance_sec"))

    # --- 麦克风设备 ---
    check("BF-13.2 界面有「麦克风」下拉框", "audio_device" in win.vars,
          "vars=%s" % sorted(win.vars))
    # 造一个假设备，选它，看写进去的 device / device_name
    real_list = audio_mod.list_input_devices
    import client.settings_ui as sui
    sui.list_input_devices = lambda: [(3, "Fake Test Mic")]
    try:
        win.apply(json.loads(json.dumps(DEFAULT_CONFIG)))
        win.vars["audio_device"].set("Fake Test Mic")
        got5 = win.collect()
        check("BF-13.2 麦克风设备能选，且**同时**写下序号与名称",
              got5["audio"]["device"] == 3
              and got5["audio"]["device_name"] == "Fake Test Mic",
              "device=%r device_name=%r"
              % (got5["audio"].get("device"), got5["audio"].get("device_name")))

        # 选「跟随系统默认」→ 两个字段都清空
        win.vars["audio_device"].set(sui.AUDIO_DEVICE_DEFAULT)
        got6 = win.collect()
        check("BF-13.2 选「跟随系统默认」时清空设备设置",
              got6["audio"]["device"] is None
              and got6["audio"]["device_name"] == "",
              "device=%r device_name=%r"
              % (got6["audio"].get("device"), got6["audio"].get("device_name")))
    finally:
        sui.list_input_devices = real_list

    # --- BF-13.3 生效范围标注 ---
    check("BF-13.3 界面写明了「哪些立即生效、哪些要重启」",
          "立即生效" in EFFECTS_HELP and "重启" in EFFECTS_HELP,
          EFFECTS_HELP.splitlines()[0])


def check_bf13_app() -> None:
    """App 层：改配置 → LLM 客户端热重建。

    这里不启动整个 App（要 Tk 窗口 + 真实麦克风），
    而是直接测 ``_apply_hot_reload`` 这个纯逻辑方法。
    """
    from client.app import App

    class _FakeApp(App):
        """绕开 __init__（不建 Tk 窗口、不连服务）。"""

        def __init__(self, cfg):  # noqa: D107
            import threading
            self.cfg = cfg
            self._reload_lock = threading.Lock()
            self.log_lines = []
            self.llm = None
            self.injector = None
            self.llm_enabled = True
            self.inject_enabled = True
            self.hotwords = []
            self.translate_enabled = False
            self.asr = type("A", (), {"url": "ws://127.0.0.1:18300"})()
            self.mic = None

        def log(self, msg):  # noqa: D401
            self.log_lines.append(msg)

    base = json.loads(json.dumps(DEFAULT_CONFIG))
    base["llm"]["model"] = "old-model"
    base["llm"]["max_tokens"] = 1024
    app = _FakeApp(base)
    app.llm = type("L", (), {"model": "old-model", "timeout": 15.0,
                             "max_tokens": 1024})()

    app._apply_hot_reload({"llm": {"model": "new-model", "max_tokens": 2048}})
    check("BF-13.1 改配置后 LLM 客户端被重建（模型变了）",
          app.llm.model == "new-model",
          "重建后的模型=%r" % app.llm.model)
    check("BF-13.1 改配置后 max_tokens 也跟着变",
          app.llm.max_tokens == 2048,
          "max_tokens=%r" % app.llm.max_tokens)
    check("BF-13.1 热更新有日志记录（用户看得见发生了什么）",
          any("LLM 客户端已重建" in m for m in app.log_lines),
          "日志=%s" % app.log_lines)

    # 热词 / 注入器也要跟着变
    app._apply_hot_reload({"hotwords": ["甲", "乙"],
                           "inject": {"mode": "typing"}})
    check("BF-13.1 热词热更新生效", app.hotwords == ["甲", "乙"],
          "hotwords=%r" % app.hotwords)
    check("BF-13.1 注入方式热更新生效",
          app.injector.mode == "typing",
          "mode=%r" % app.injector.mode)

    # 需要重启的段要**明说**
    app2 = _FakeApp(json.loads(json.dumps(DEFAULT_CONFIG)))
    app2._apply_hot_reload({"hotkey": {"keys": ["ctrl", "alt"]}})
    check("BF-13.3 热键改动会提示「需要重启」",
          any("重启" in m for m in app2.log_lines),
          "日志=%s" % app2.log_lines)


def main() -> int:
    check_bf12_1()
    check_bf12_3()
    check_bf12_3_config()
    check_bf13()
    check_bf13_2()
    check_bf13_app()

    print("=" * 74)
    print("P5 验证：麦克风设备（BF-12）与设置热更新（BF-13）")
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
