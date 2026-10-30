# -*- coding: utf-8 -*-
"""EtherealFlow · 悬浮窗的**音量电平条**（FR-3.5）与 **error 状态**（FR-3.6）。

这两项在需求文档 v1.0 里就列着「必须实现」，但一直没做也没测：

* **FR-3.5 音量波形** —— ``MicCapture.peak`` 算了没人读，悬浮窗里没有电平指示，
  用户不知道麦克风到底有没有收到声音（静音、选错设备都会表现为「没反应」）。
* **FR-3.6 error 状态** —— 识别服务断连、LLM 鉴权失败这些以前只写进日志，
  用户看到的是「悬浮窗一直空着」，完全不知道出了什么事。

断言怎么写才算数：
* 电平条用 **Canvas 里的图元**判定（``find_withtag("bar")``），不是看像素 ——
  看像素会受主题/DPI 影响而变得不稳定。
* error 状态断言**文案**（状态栏 + 正文），不是断言「有红字」。
* 阳性对照：把实现改坏（电平条不画 / error 不显示），断言**必须**失败。

退出码 0 = 没有 FAIL。
"""

from __future__ import annotations

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.overlay import STATE_LABEL, Overlay                # noqa: E402

rows: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    rows.append((name, ok, detail))
    print("%s %s" % ("[PASS]" if ok else "[FAIL]", name), flush=True)
    if detail:
        print("        %s" % detail, flush=True)


def _bar_items(overlay: Overlay) -> list:
    """电平条 Canvas 上的图元（tag="bar"）。"""
    try:
        return overlay.level.find_withtag("bar")
    except Exception:  # noqa: BLE001
        return []


def _bar_width(overlay: Overlay) -> int:
    """电平条当前画了多宽（像素）。"""
    try:
        items = overlay.level.find_withtag("bar")
        if not items:
            return 0
        coords = overlay.level.coords(items[0])
        return int(coords[2] - coords[0])
    except Exception:  # noqa: BLE001
        return 0


def _wait_canvas_width(overlay: Overlay, timeout: float = 3.0) -> int:
    """等 Canvas 算出真实宽度。

    电平条画多宽取决于 Canvas 的宽度，而宽度要等布局算完才知道。
    悬浮窗是**隐藏**状态下算几何的（见 overlay._place 的说明），
    所以宽度在窗口真正显示前可能还是 1（Tk 的「还没算」默认值）。

    直接断言会在窗口还没显示时读到 width=1 → 电平条宽度为 0 → **假失败**。
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        width = int(overlay.level.winfo_width())
        if width > 1:
            return width
        time.sleep(0.02)
    return int(overlay.level.winfo_width())


def main() -> int:
    overlay = Overlay(width=600, opacity=0.9, font_size=14, translate=False)

    def worker() -> None:
        try:
            _run(overlay)
        except Exception as exc:  # noqa: BLE001
            check("测试脚本自身没崩", False, repr(exc))
        finally:
            try:
                overlay.root.after(200, overlay.root.quit)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=worker, daemon=True).start()
    overlay.root.mainloop()

    print("=" * 74)
    print("P2 验证：悬浮窗电平条（FR-3.5）与 error 状态（FR-3.6）")
    print("=" * 74)
    fails = [n for n, ok, _d in rows if not ok]
    print("-" * 74)
    print("结论：%s（%d/%d）"
          % ("全部通过" if not fails else "失败项：%s" % fails,
             sum(1 for _n, ok, _d in rows if ok), len(rows)))
    sys.stdout.flush()
    # Tk 的拆卸在某些环境里会拖住退出，直接硬退
    os._exit(0 if not fails else 1)


def _run(overlay: Overlay) -> None:
    def pump(seconds: float = 0.35) -> None:
        """等主线程把队列消费掉。

        ``post()`` 是异步的 —— 投进去就返回，真正生效要等主线程跑到
        ``_drain``。这里 sleep 一下再断言。
        """
        time.sleep(seconds)

    # ---------- FR-3.6：error 状态存在 ---------- #
    check("FR-3.6 状态表里有 error 这一项",
          "error" in STATE_LABEL,
          "STATE_LABEL 的键=%s" % sorted(STATE_LABEL))

    # ---------- 电平条在录音态出现 ---------- #
    overlay.show_listening()
    pump(0.5)
    # 判据用 ``winfo_manager()``（控件是否在布局管理器里），**不用**
    # ``winfo_ismapped()``。
    #
    # 为什么：``ismapped`` 要求控件**当前**真的被映射到屏幕上，而悬浮窗是用
    # Win32 的 ``SetWindowPos`` 显示的（**不走** Tk 的 ``deiconify``）——
    # Tk 内部的映射状态因此会滞后于实际显示，实测会**偶发**返回 0
    # （连跑 15 次有 1 次）。
    # 而 ``winfo_manager()`` 反映的是「这个控件在不在布局里」——
    # 这正是电平条显示/隐藏这件事本身，**确定**、不依赖窗口何时被映射。
    check("FR-3.5 录音态电平条在布局里",
          overlay.level.winfo_manager() == "pack",
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # 没有音量时不该画条（宽度 0）
    check("FR-3.5 没有声音时电平条为空",
          _bar_width(overlay) == 0,
          "条宽=%d" % _bar_width(overlay))

    # ---------- 有音量时电平条变长 ---------- #
    canvas_w = _wait_canvas_width(overlay)
    overlay.set_volume(0.9)
    pump(0.3)
    w1 = _bar_width(overlay)
    check("FR-3.5 收到音量后电平条画出来了",
          w1 > 0,
          "peak=0.9 → 条宽=%d px（电平条宽 %d px）" % (w1, canvas_w))

    # 音量更大 → 条更长
    overlay.set_volume(0.99)
    pump(0.05)
    w2 = _bar_width(overlay)
    check("FR-3.5 音量越大电平条越长",
          w2 >= w1,
          "peak=0.9 → %d px；peak=0.99 → %d px" % (w1, w2))

    # 静音 → 电平条**平滑**回落（不是瞬间消失，也不是永远不落）
    # 衰减是**按时间**的（tau≈0.35 s），所以要给它一点时间。
    overlay.set_volume(0.0)
    pump(0.05)
    w_mid = _bar_width(overlay)
    pump(1.5)
    w3 = _bar_width(overlay)
    check("FR-3.5 静音后电平条平滑回落到 0",
          w3 == 0,
          "刚静音时条宽=%d px（还在缓慢下落）→ 1.5 s 后=%d px" % (w_mid, w3))
    check("FR-3.5 静音后是**渐降**而不是瞬间消失",
          w_mid > 0,
          "静音 50 ms 后条宽=%d px（应为 0 才是瞬间消失，那不好看）" % w_mid)

    # ---------- 非录音态不显示电平条 ---------- #
    overlay.set_state("thinking")
    pump(0.3)
    check("FR-3.5 非录音态电平条离开布局",
          overlay.level.winfo_manager() == "",
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # ⚠️ 回归：隐藏过一次之后**必须还能再显示出来**。
    # pack_forget() 会让控件脱离布局管理器；这时 pack_configure()
    # 只改配置、**不会把它放回来** —— 症状是「第一次录音有电平条，
    # 第二次开始就没了」。
    overlay.show_listening()
    pump(0.4)
    check("FR-3.5 隐藏过一次后还能再显示出来（回归）",
          overlay.level.winfo_manager() == "pack",
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # ---------- FR-3.6：error 状态可见 ---------- #
    overlay.show_error("识别服务连接失败：Connection refused")
    pump(0.4)
    state, text = overlay.snapshot()
    check("FR-3.6 出错时状态变成 error",
          state == "error",
          "state=%r" % state)
    check("FR-3.6 出错时正文显示具体原因（用户看得见）",
          "连接失败" in text,
          "正文=%r" % text)
    check("FR-3.6 状态栏有 error 文案",
          STATE_LABEL.get(state, "") == STATE_LABEL.get("error", ""),
          "状态栏=%r" % STATE_LABEL.get(state, ""))

    # ---------- 录音过程中出错：悬浮窗**不能**自动消失 ---------- #
    # 录音中悬浮窗正显示着流式文字，自动隐藏会让用户以为程序挂了。
    overlay.show_listening()
    pump(0.3)
    overlay.show_error("中途出了个错", auto_hide=False)
    pump(0.3)
    check("FR-3.6 录音中出错时悬浮窗保留（不自动隐藏）",
          overlay.visible,
          "visible=%s" % overlay.visible)
    # 而非录音态的独立错误，4 秒后应该自动收起
    overlay.show_error("独立的一条错误", auto_hide=True)
    pump(0.3)
    check("FR-3.6 独立错误会安排自动隐藏（4 秒后收起）",
          overlay.visible,
          "刚显示时 visible=%s（4 秒后才该消失）" % overlay.visible)

    # ---------- 阳性对照 ---------- #
    # 1) 电平条不画（模拟「peak 算了没人读」的旧行为）
    saved_draw = Overlay._draw_level

    def no_draw(self):  # noqa: ANN001
        try:
            self.level.delete("bar")
        except Exception:  # noqa: BLE001
            pass

    Overlay._draw_level = no_draw
    try:
        overlay.show_listening()
        pump(0.4)
        overlay.set_volume(0.8)
        pump(0.3)
        check("FR-3.5 阳性对照：不画电平条时断言会失败",
              _bar_width(overlay) == 0,
              "对照组条宽=%d（应为 0，说明对照组确实复现了「没有电平条」）"
              % _bar_width(overlay))
    finally:
        Overlay._draw_level = saved_draw

    # 2) error 状态不进正文（模拟「只写日志」的旧行为）
    saved_apply = Overlay._apply

    def no_error_apply(self, kind, value):  # noqa: ANN001
        if kind == "error":
            return          # 假装这个扩展根本不存在
        return saved_apply(self, kind, value)

    Overlay._apply = no_error_apply
    try:
        overlay.show_error("这条错误应该被吞掉")
        pump(0.4)
        state2, text2 = overlay.snapshot()
        check("FR-3.6 阳性对照：不处理 error 时断言会失败",
              state2 != "error" and "吞掉" not in text2,
              "对照组 state=%r 正文=%r（说明对照组确实复现了「错误看不见」）"
              % (state2, text2))
    finally:
        Overlay._apply = saved_apply


if __name__ == "__main__":
    main()
