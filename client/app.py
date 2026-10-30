# -*- coding: utf-8 -*-
"""EtherealFlow · 主程序（P2：热键 + 麦克风 + 不抢焦点悬浮窗）。

正常用法::

    python -m client.app

机器自测（不需要人按热键、不需要真麦克风）::

    python -m client.app --replay path/to/16k.wav          # 按实时速度回放，走完整链路
    python -m client.app --list-devices

``--replay`` 会调用与真实按键**完全相同**的 ``_on_press`` / ``_on_release``，
所以它是这条链路的可信验证手段。

P3 会在这里接上 LLM 修正与文本注入；P4 接上流式翻译。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import win32 as w32
from .asr import AsrClient
from .audio import (MicCapture, list_input_devices, read_wav_pcm16k,
                   resolve_input_device)
from .config import (DEFAULT_CONFIG, default_config_dir, default_config_path,  # noqa: F401
                     deep_merge, load_config)
from .hotkey import create_hotkey
from .hotreload import ConfigWatcher, diff_sections
from .inject import default_injector
from .llm import default_client
from .overlay import Overlay
from .settings_ui import SettingsWindow
from .textutil import build_asr_context, smart_join
from .translator import default_translator

def emit(text: str) -> None:
    """命令行诊断输出。

    打包成**无控制台的窗口程序**后没有 stdout，``print`` 写不出去；
    所以这里退化成写日志文件，保证 ``--list-devices`` 这类诊断不会白跑。
    """
    line = str(text)
    try:
        if sys.stdout is not None:
            print(line, flush=True)
            return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        path = default_config_dir() / "logs" / "client.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class App:
    def __init__(self, cfg: dict[str, Any], config_path: Optional[str | Path] = None) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path) if config_path else None
        self.translate_enabled = bool(cfg.get("translate", {}).get("enabled", False))
        self.overlay = Overlay(width=cfg["overlay"]["width"],
                               opacity=cfg["overlay"]["opacity"],
                               font_size=cfg["overlay"]["font_size"],
                               translate=self.translate_enabled)
        self.asr = AsrClient(
            url=cfg["asr"]["url"],
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_segment_final=self._on_segment_final,
            on_error=self._on_error,
            on_status=self._on_status,
        )
        self.mic: Optional[MicCapture] = None
        self.hotkey = None
        self.translator = None
        # 长语音分段：`_prefix` 是已落定的前几段文本
        self._prefix = ""
        self._rotating = False
        self._segment_started_at: Optional[float] = None
        self.session_active = False
        self.final_text = ""
        self.done = threading.Event()
        self.log_lines: list[str] = []
        self.log_path = default_config_dir() / "logs" / "client.log"
        self.llm = default_client(cfg.get("llm"))
        self.injector = default_injector(cfg.get("inject"))
        self.llm_enabled = bool(cfg["llm"].get("enabled", True))
        self.inject_enabled = str(cfg["inject"].get("mode", "clipboard")).lower() != "none"
        self.hotwords = list(cfg.get("hotwords") or [])
        # BF-13.1：盯着配置文件，改了自动热更新（不用重启）
        self._watcher: Optional[ConfigWatcher] = None
        self._reload_lock = threading.Lock()

    # -- 配置热更新（BF-13.1） --------------------------------------------- #

    def _apply_hot_reload(self, raw: dict) -> None:
        """配置文件变了 —— 把**能热更新**的部分换掉。

        ⚠️ 用 ``deep_merge(DEFAULT_CONFIG, raw)`` 而不是直接用 raw：
        配置文件里只写「用户改过的键」（见 ``config.save_config``），
        直接用 raw 的话，缺省的那些键就**全部消失**了。
        """
        with self._reload_lock:
            old_cfg = self.cfg
            # ``deep_merge`` 返回的是**深拷贝**（BF-5）——
            # 结果与 DEFAULT_CONFIG 不共享任何嵌套对象，
            # 后面改 cfg["llm"]["xxx"] 不会污染全局默认值。
            new_cfg = deep_merge(DEFAULT_CONFIG, raw)
            changed = diff_sections(old_cfg, new_cfg)
            if not changed:
                return
            self.cfg = new_cfg

            # --- LLM：重建客户端 ---
            # 「保留一次连接预热」：新旧客户端**并存**，新的建好再换 ——
            # 正在飞行中的请求继续用旧客户端跑完，不会被打断。
            if "llm" in changed:
                old_llm = self.llm
                self.llm = default_client(new_cfg.get("llm"))
                self.llm_enabled = bool(new_cfg["llm"].get("enabled", True))
                self.log("配置已更新：LLM 客户端已重建"
                         "（模型=%s、超时=%.0fs、max_tokens=%d）"
                         % (self.llm.model or "(默认)", self.llm.timeout,
                            self.llm.max_tokens))
                del old_llm

            # --- 注入器：重建 ---
            if "inject" in changed:
                self.injector = default_injector(new_cfg.get("inject"))
                self.inject_enabled = (str(new_cfg["inject"].get("mode", "clipboard"))
                                       .lower() != "none")
                self.log("配置已更新：注入方式=%s"
                         % new_cfg["inject"].get("mode", "clipboard"))

            # --- 热词：下一次会话带上 ---
            if "hotwords" in changed:
                self.hotwords = list(new_cfg.get("hotwords") or [])
                self.log("配置已更新：热词 %d 条" % len(self.hotwords))

            # --- 识别服务：地址变了要重连 ---
            if "asr" in changed:
                new_url = new_cfg["asr"].get("url", "")
                old_url = old_cfg.get("asr", {}).get("url", "")
                if new_url and new_url != old_url:
                    self.log("配置已更新：识别服务地址变了（%s → %s），重连中"
                             % (old_url, new_url))
                    self.asr.url = new_url
                    # AsrClient 自己会重连（_run 循环里 urlopen 失败会重试）
                else:
                    self.log("配置已更新：识别参数（语言/上下文/分段时长）已生效")

            # --- 音频：下一次开麦时用 ---
            if "audio" in changed:
                self.log("配置已更新：麦克风设置将在下一次录音时生效")

            # --- 翻译：下一次说话时用 ---
            if "translate" in changed:
                self.translate_enabled = bool(new_cfg.get("translate", {})
                                              .get("enabled", False))
                self.log("配置已更新：翻译参数将在下一次说话时生效")

            cold = sorted(changed & {"hotkey", "overlay"})
            if cold:
                self.log("⚠ %s 的改动需要**重启客户端**才生效" % "、".join(cold))

    def _start_config_watcher(self) -> None:
        if self.config_path is None:
            return
        self._watcher = ConfigWatcher(self.config_path, self._apply_hot_reload)
        self._watcher.start()
        self.log("配置监视中：%s（改了会自动生效，不用重启）" % self.config_path)

    def _begin_asr_session(self) -> None:
        """开识别会话：context = 热词表 + 识别上下文。

        ⚠️ 热词**必须**同时喂给识别引擎（FR-7「词典：热词列表，
        传给识别引擎作为 context」）。v0.3.0 只把它喂给了 LLM ——
        界面和 README 都写着会传给引擎，实际没有（BF-4）。
        """
        self.asr.begin_session(
            language=self.cfg["asr"].get("language"),
            context=build_asr_context(self.hotwords,
                                      self.cfg["asr"].get("context", "")),
        )

    # -- 日志 -------------------------------------------------------------- #
    def log(self, msg: str) -> None:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        self.log_lines.append(line)
        # 打包成无控制台的窗口程序后 sys.stdout 是 None，print 会直接抛异常
        try:
            print(line, flush=True)
        except (OSError, ValueError, AttributeError):
            pass
        # 所以日志同时落盘，出问题时用户能拿来排查
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # -- 回调（都在工作线程里） -------------------------------------------- #

    def _on_partial(self, text: str) -> None:
        if not text:
            return
        full = smart_join([self._prefix, text])
        self.overlay.set_text(full)
        if self.translator is not None:
            self.translator.update(full)
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        """说太久就分段重建会话（详见 AsrClient.rotate 的说明）。"""
        limit = float(self.cfg["asr"].get("max_utterance_sec") or 0)
        if limit <= 0 or self._rotating or self._segment_started_at is None:
            return
        if time.monotonic() - self._segment_started_at < limit:
            return
        self._rotating = True
        self.log("已说 %.0f 秒，分段重建会话（避免流式解码跟不上实时）" % limit)
        self.asr.rotate()

    def _on_segment_final(self, msg: dict) -> None:
        """一段结束：把结果**追加**到前缀，然后无缝开下一段。"""
        part = (msg.get("text") or "").strip()
        if part:
            # 说英文时直接相加会粘成一个词，交给 smart_join 按需补空格
            self._prefix = smart_join([self._prefix, part])
        self._rotating = False
        self._segment_started_at = time.monotonic()
        self.log("分段完成（%d 字），累计 %d 字，继续听…" % (len(part), len(self._prefix)))
        self.overlay.set_text(self._prefix)
        if self.session_active:
            # ⚠️ keep_end_pending=True：用户可能**恰好在分段途中松了手**
            # （end_session 发现正在轮转，就记下「想结束」等着）。
            # 这时重开新会话不能把这个标记抹掉 ——
            # 抹掉的话，新会话的 final 会被当成普通的一段，
            # 最后一段的内容就丢了（实测整个后半段消失）。
            self.asr.begin_session(
                language=self.cfg["asr"].get("language"),
                context=build_asr_context(self.hotwords,
                                          self.cfg["asr"].get("context", "")),
                keep_end_pending=True)

    def _on_final(self, msg: dict) -> None:
        # 分段过的话，整句 = 已落定前缀 + 最后一段
        self.final_text = smart_join([self._prefix, msg.get("text") or ""])
        self._rotating = False
        # 轮转途中松手时，session_active 被特意保持 True（好让新会话开起来），
        # 这里收尾时置回去 —— 否则下次按热键会被「已在录音中」挡住。
        self.session_active = False
        self.overlay.set_text(self.final_text)
        stats = {k: v for k, v in msg.items() if k not in ("type", "text")}
        self.log("FINAL: %s | %s" % (self.final_text, stats))
        if not self.final_text.strip():
            self.log("识别结果为空 → 不注入任何东西")
            self.overlay.set_state("done")
            self.overlay.hide_in(0.6)
            self.done.set()
            return
        if self.translate_enabled:
            self.overlay.set_state("translating")
            threading.Thread(target=self._translate_and_inject, daemon=True).start()
        elif self.llm_enabled:
            self.overlay.set_state("thinking")
            threading.Thread(target=self._correct_and_inject, daemon=True).start()
        else:
            self.overlay.set_state("done")
            self.overlay.hide_in(0.5)
            self._inject(self.final_text, source="识别原文")

    def _translate_and_inject(self) -> None:
        """FR-5：提交**译文**而不是原文；翻译全失败则回退原文。"""
        translator = self.translator
        if translator is None:
            self._inject(self.final_text, source="识别原文")
            return
        res = translator.finish(self.final_text)
        self.log("翻译：%s | %s" % (res.describe(), res.text))
        if res.degraded:
            self.log("⚠ 未取得译文，本次提交识别原文")
        self.overlay.set_translation(res.text)
        self.overlay.set_state("done")
        self.overlay.hide_in(0.5)
        self._inject(res.text, source="译文" if res.translated else "识别原文（翻译降级）")

    def _correct_and_inject(self) -> None:
        """松手之后的第二步：LLM 修正 → 注入。失败按分类降级。"""
        outcome = self.llm.correct(self.final_text, hotwords=self.hotwords)
        tag = "［%s］" % outcome.mode if outcome.mode else ""
        if outcome.changed:
            self.log("修正后%s：%s（%.0f ms）" % (tag, outcome.text, outcome.elapsed_ms))
        elif outcome.notify:
            self.log("⚠ LLM 配置有问题，本次直接注入识别原文：%s" % outcome.error)
        elif outcome.no_change:
            # 和"降级"不是一回事：模型答的是"这句不用改"。选「不整理」时这是常态。
            self.log("%s模型判定无需改动，注入识别原文（%.0f ms）"
                     % (tag, outcome.elapsed_ms))
        else:
            self.log("LLM 未改动/已降级（%s），注入识别原文" % (outcome.code or "no-change"))
        self.overlay.set_text(outcome.text)
        self.overlay.set_state("done")
        self.overlay.hide_in(0.35)
        self._inject(outcome.text, source="修正后" if outcome.changed else "识别原文")

    def _inject(self, text: str, source: str) -> None:
        if not self.inject_enabled:
            self.log("[未注入] %s：%s" % (source, text))
            self.done.set()
            return
        if not text.strip():
            self.done.set()
            return
        # 保险：前台**恰好是悬浮窗自己**时绝不灌字（正常情况下不可能，
        # 因为悬浮窗带 WS_EX_NOACTIVATE）。这里只针对我们自己的悬浮窗，
        # 不要用"整个进程的窗口"来判断 —— 那样会把控制台窗口也算进去，
        # 导致正常注入被误跳过（实测踩到）。
        fg = w32.foreground_window()
        own = {self.overlay.hwnd, self.overlay.top_hwnd}
        if fg and fg in own:
            self.log("前台是悬浮窗自己（0x%X），跳过注入以免误伤" % fg)
            self.done.set()
            return
        res = self.injector.inject(text)
        # ⚠️ 注入日志**要等剪贴板还原完成后再打**。
        # 还原是延迟执行的（默认 500 ms 后），而「还原成功/失败」是这个结果里
        # 最该让用户看到的信息 —— 之前日志打得早，日志里永远没有
        # 「剪贴板已还原」这一句，还原失败更是完全静默。
        self.log("注入（%s，%s）：%s" % (source, res.method, res.describe()))
        for warn in list(res.warnings):
            self.log("  注入告警：%s" % warn)
        if not self.injector.restore_clipboard:
            self.done.set()
            return

        def _log_after_restore() -> None:
            deadline = time.time() + 3.0
            while time.time() < deadline and not res.restore_done:
                time.sleep(0.05)
            self.log("注入完成（%s）：%s" % (source, res.describe()))
            for warn in res.warnings:
                self.log("  注入告警：%s" % warn)
            self.done.set()

        threading.Thread(target=_log_after_restore, daemon=True).start()

    def _on_audio_block(self, pcm: bytes) -> None:
        """麦克风来了一块音频。

        除了喂给识别引擎，还负责**更新音量电平**（FR-3.5）——
        ``MicCapture.peak`` 里存着最近一块的峰值，之前算了没人读。
        """
        self.asr.feed(pcm)
        peak = getattr(self.mic, "peak", 0.0) if self.mic is not None else 0.0
        self.overlay.set_volume(peak)

    def _on_error(self, msg: str) -> None:
        self.log("ERROR: %s" % msg)
        # FR-3.6：出错要让用户**看得见**。
        # 只写日志的话，用户看到的是「悬浮窗一直空着」，完全不知道出了事。
        # 但不是所有错误都值得弹窗 —— 「音频积压」「丢弃了几个块」这类
        # 是可恢复的内部状态，用户没法处理，刷屏反而烦人。
        if self._is_actionable_error(msg):
            # 录音过程中出错 → **不要**自动隐藏悬浮窗（它正显示着流式文字）
            self.overlay.show_error(msg, auto_hide=not self.session_active)
        else:
            # 恢复性错误：把状态栏改成 error，但不覆盖正在流式显示的文字
            self.overlay.post("state", "error")

    #: 只有这些错误才值得**覆盖正文**显示（其余走状态栏）。
    _ACTIONABLE_ERROR_HINTS = ("连接失败", "未就绪", "鉴权", "401", "403",
                               "超时", "拒绝", "打开失败", "发送失败")

    def _is_actionable_error(self, msg: str) -> bool:
        return any(hint in msg for hint in self._ACTIONABLE_ERROR_HINTS)

    def _on_status(self, status: str) -> None:
        self.log("ASR 状态: %s" % status)

    # -- 会话控制 ---------------------------------------------------------- #

    def _on_press(self, use_mic: bool = True) -> None:
        if self.session_active:
            return
        self.session_active = True
        self.final_text = ""
        self._prefix = ""
        self._rotating = False
        self._segment_started_at = time.monotonic()
        self.done.clear()
        self.log("按下热键 → 开始采集")
        self.overlay.show_listening()
        self._begin_asr_session()
        if self.translate_enabled:
            self.translator = default_translator(
                self.llm, self.cfg.get("translate"),
                on_update=self.overlay.set_translation, on_error=self._on_error)
            self.translator.start()
        if not use_mic:
            return
        # 麦克风设备：**按名称优先**解析，序号只作兜底。
        # 设备序号会漂移（插拔 USB 声卡 / 换蓝牙耳机 / 系统默认设备变化），
        # 只存序号会导致「昨天还好好的麦今天没了」—— 详见 audio.resolve_input_device。
        audio_cfg = self.cfg.get("audio") or {}
        dev = resolve_input_device(audio_cfg.get("device"),
                                   audio_cfg.get("device_name", ""))
        self.mic = MicCapture(device=dev, chunk_ms=self.cfg["audio"]["chunk_ms"],
                              gain=self.cfg["audio"].get("gain", 1.0),
                              on_block=self._on_audio_block,
                              on_error=self._on_error)
        try:
            self.mic.start()
            self.log("麦克风已开：%.0f Hz%s"
                     % (self.mic.capture_rate, "（重采样中）" if self.mic.resampling else ""))
        except Exception as exc:  # noqa: BLE001
            self.log("麦克风打开失败：%r" % exc)
            self._on_error("麦克风打开失败：%s" % exc)
            self.session_active = False
            self.overlay.hide()

    def _stop_mic(self) -> None:
        if self.mic is not None:
            self.mic.stop()
            self.mic = None

    def _on_release(self) -> None:
        if not self.session_active:
            return
        self._stop_mic()
        self.log("松开热键 → 请求最终结果")
        self.overlay.set_state("thinking")
        # ⚠️ **轮转途中松手**要特殊处理。
        # 此刻服务端正在 finish 上一段（要几秒），扣住的音频还在暂存区。
        # 如果这里就把 session_active 置 False，那么 _on_segment_final 里
        # 「if self.session_active: 开新会话」就不会执行 ——
        # 扣住的音频永远发不出去，**后半段整个消失**。
        # 正确做法：让 asr 记下「想结束」（end_session 内部会处理），
        # 等新会话的 final 回来再收尾。
        self.asr.end_session()
        if not self._rotating:
            # 不是在轮转途中 → 正常路径，等 final 就行
            self.session_active = False
        else:
            # 轮转途中松手：asr 已记下「想结束」。
            # session_active **保持 True** —— 否则 _on_segment_final 里
            # 「if self.session_active: 开新会话」不执行，
            # 扣住的音频就永远发不出去（后半段整个消失）。
            # 新会话的 final 回来后，_on_final 会把 session_active 置回 False。
            self.log("（正处在分段收尾，稍后继续收尾）")

    def _on_cancel(self) -> None:
        """Esc → 取消本次输入：**一个字都不注入**。"""
        if not self.session_active:
            return
        self.session_active = False
        self._stop_mic()
        # 清掉已累积的文本 —— 取消就是「当没说过」，
        # 后面不能再拿这些字去注入。
        self._prefix = ""
        self.final_text = ""
        self._rotating = False
        self.log("Esc → 取消本次输入")
        self.asr.cancel_session()
        self.overlay.set_state("done")
        self.overlay.hide()
        self.done.set()

    # -- 运行 -------------------------------------------------------------- #

    def _make_hotkey(self):
        hk = self.cfg["hotkey"]
        params = dict(keys=hk["keys"], on_press=self._on_press,
                      on_release=self._on_release, on_cancel=self._on_cancel)
        if hk.get("backend", "polling").lower() == "hook":
            params["swallow"] = hk.get("swallow", True)
        return create_hotkey(hk.get("backend", "polling"), **params)

    def run(self, replay: Optional[str] = None, exit_after_final: bool = False) -> int:
        self.asr.start()
        self._start_config_watcher()
        if replay:
            threading.Thread(target=self._replay, args=(replay, exit_after_final),
                             daemon=True).start()
        else:
            self.hotkey = self._make_hotkey()
            self.hotkey.start()
            self.log("热键就绪：%s（后端 %s）"
                     % ("+".join(self.cfg["hotkey"]["keys"]), self.hotkey.backend))
        self.overlay.run()
        self._shutdown()
        return 0

    def _replay(self, wav: str, exit_after_final: bool) -> None:
        try:
            pcm = read_wav_pcm16k(wav)
        except Exception as exc:  # noqa: BLE001
            self.log("读取 WAV 失败：%r" % exc)
            self.overlay.stop()
            return
        chunk = self.cfg["audio"]["chunk_ms"] * 32   # 每毫秒 32 字节(16k*2)
        total = len(pcm) / 2.0 / 16000.0
        self.log("回放 %s（%.2f 秒，按实时速度推送；**不开麦克风**）" % (wav, total))
        self._on_press(use_mic=False)
        t0 = time.monotonic()
        for off in range(0, len(pcm), chunk):
            self.asr.feed(pcm[off:off + chunk])
            target = t0 + (off + chunk) / 2.0 / 16000.0
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self._on_release()
        if exit_after_final and self.done.wait(timeout=60):
            time.sleep(1.1)          # 让 overlay 的隐藏动画走完
        self.overlay.stop()

    def _shutdown(self) -> None:
        if self.hotkey is not None:
            self.hotkey.stop()
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._stop_mic()
        self.asr.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="EtherealFlow Windows 客户端")
    parser.add_argument("--config", default="", help="JSON 配置文件路径")
    parser.add_argument("--settings", action="store_true", help="打开设置界面后退出")
    parser.add_argument("--replay", default="", help="回放一份 WAV 走完整链路（自测用）")
    parser.add_argument("--exit-after-final", action="store_true",
                        help="配合 --replay：拿到最终结果就退出")
    parser.add_argument("--list-devices", action="store_true", help="列出输入设备后退出")
    parser.add_argument("--hotkey-backend", default="", choices=["", "polling", "hook"])
    parser.add_argument("--device", default=None, help="输入设备序号或名称")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM，直接注入识别原文")
    parser.add_argument("--inject-mode", default="", choices=["", "clipboard", "typing", "none"],
                        help="注入方式；none = 只打印不注入（自测用）")
    parser.add_argument("--hotwords", default="", help="热词，逗号分隔")
    parser.add_argument("--translate", action="store_true",
                        help="开启流式翻译（提交译文而不是原文）")
    parser.add_argument("--target-language", default="",
                        help="翻译目标语言：auto / English / Chinese …")
    args = parser.parse_args()

    if args.list_devices:
        emit("EtherealFlow 可用输入设备：")
        for idx, name in list_input_devices():
            emit("%3d  %s" % (idx, name))
        return 0

    if args.settings:
        cfg_path = Path(args.config) if args.config else default_config_path()
        SettingsWindow(load_config(cfg_path), cfg_path).run()
        return 0

    cfg = load_config(args.config or None)
    cfg_path = Path(args.config) if args.config else default_config_path()
    if args.hotkey_backend:
        cfg["hotkey"]["backend"] = args.hotkey_backend
    if args.device is not None:
        # 数字 = 序号；否则当作设备名。
        # 两种都同时写入 device_name，运行时按名重解析（序号会漂移）。
        if args.device.isdigit():
            cfg["audio"]["device"] = int(args.device)
            try:
                devs = dict(list_input_devices())
                cfg["audio"]["device_name"] = devs.get(int(args.device), "")
            except Exception:  # noqa: BLE001
                cfg["audio"]["device_name"] = ""
        else:
            cfg["audio"]["device_name"] = args.device
            try:
                devs = dict(list_input_devices())
                cfg["audio"]["device"] = next(
                    (i for i, n in devs.items() if n == args.device), None)
            except Exception:  # noqa: BLE001
                cfg["audio"]["device"] = None
    if args.no_llm:
        cfg["llm"]["enabled"] = False
    if args.inject_mode:
        cfg["inject"]["mode"] = args.inject_mode
    if args.hotwords:
        cfg["hotwords"] = [w.strip() for w in args.hotwords.split(",") if w.strip()]
    if args.translate:
        cfg["translate"]["enabled"] = True
    if args.target_language:
        cfg["translate"]["target_language"] = args.target_language

    app = App(cfg, config_path=cfg_path)
    try:
        return app.run(replay=args.replay or None, exit_after_final=args.exit_after_final)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
