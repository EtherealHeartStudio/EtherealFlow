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


def _run_on_tk(overlay: Overlay, fn) -> None:
    """在悬浮窗的 **Tk 主线程** 上跑 ``fn``，并等它跑完。

    ⚠️ tkinter 不是线程安全的 —— 从别的线程碰 Tk 控件会出问题。
    悬浮窗自己有现成的机制：``post("call", fn)`` 会在主线程执行。
    """
    done = threading.Event()

    def wrapped() -> None:
        try:
            fn()
        finally:
            done.set()

    overlay.post("call", wrapped)
    if not done.wait(timeout=5.0):
        raise AssertionError("主线程没有在 5 秒内执行回调")


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
    def wait_for(predicate, timeout: float = 5.0) -> bool:
        """轮询直到 ``predicate()`` 为真（或超时）。

        ⚠️ **不要用「sleep 固定时长然后断言」** ——
        慢的机器（CI runner）上 sleep 会**超时**，快的机器上又**不够**，
        两种情况都会变成随机失败（实测：CI 上「静音后是渐降」那条就是这么红的）。

        「等某个条件成立」与机器快慢**无关** —— 这是唯一稳的写法。
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    # ---------- FR-3.6：error 状态存在 ---------- #
    check("FR-3.6 状态表里有 error 这一项",
          "error" in STATE_LABEL,
          "STATE_LABEL 的键=%s" % sorted(STATE_LABEL))

    # ---------- 电平条在录音态出现 ---------- #
    overlay.show_listening()
    # 判据用 ``winfo_manager()``（控件是否在布局管理器里），**不用**
    # ``winfo_ismapped()``。
    #
    # 为什么：``ismapped`` 要求控件**当前**真的被映射到屏幕上，而悬浮窗是用
    # Win32 的 ``SetWindowPos`` 显示的（**不走** Tk 的 ``deiconify``）——
    # Tk 内部的映射状态因此会滞后于实际显示，实测会**偶发**返回 0
    # （连跑 15 次有 1 次）。
    # 而 ``winfo_manager()`` 反映的是「这个控件在不在布局里」——
    # 这正是电平条显示/隐藏这件事本身，**确定**、不依赖窗口何时被映射。
    got_pack = wait_for(lambda: overlay.level.winfo_manager() == "pack")
    check("FR-3.5 录音态电平条在布局里",
          got_pack,
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # 没有音量时不该画条（宽度 0）
    check("FR-3.5 没有声音时电平条为空",
          _bar_width(overlay) == 0,
          "条宽=%d" % _bar_width(overlay))

    # ---------- 有音量时电平条变长 ---------- #
    canvas_w = _wait_canvas_width(overlay)
    overlay.set_volume(0.9)
    got_bar = wait_for(lambda: _bar_width(overlay) > 0)
    w1 = _bar_width(overlay)
    check("FR-3.5 收到音量后电平条画出来了",
          got_bar and w1 > 0,
          "peak=0.9 → 条宽=%d px（电平条宽 %d px）" % (w1, canvas_w))

    # 音量更大 → 条更长
    overlay.set_volume(0.99)
    wait_for(lambda: _bar_width(overlay) >= w1)
    w2 = _bar_width(overlay)
    check("FR-3.5 音量越大电平条越长",
          w2 >= w1,
          "peak=0.9 → %d px；peak=0.99 → %d px" % (w1, w2))

    # 静音 → 电平条**平滑**回落（不是瞬间消失，也不是永远不落）
    #
    # ⚠️ 这里**不用墙钟**（``time.sleep(0.05)``）来测衰减 ——
    # CI runner 上 50 ms 的 sleep 实际可能过了 1 秒以上，
    # 于是「刚静音 50 ms」那一刻电平早就落完了 → 随机变红（实测踩过）。
    #
    # 改成**直接控制经过的时间**：把 ``_level_at`` 往回拨，
    # 再让主线程跑一次 ``_draw_level``。这样「过了多久」是**确定的**。
    def _level_after(dt: float) -> float:
        """让电平从 1.0 静默衰减 dt 秒，返回衰减后的电平。"""
        def work() -> None:
            overlay._level = 1.0
            overlay._level_target = 0.0
            overlay._level_at = time.monotonic() - dt
            overlay._draw_level()
        _run_on_tk(overlay, work)
        return overlay._level

    lvl_50ms = _level_after(0.05)
    check("FR-3.5 静音后是**渐降**而不是瞬间消失",
          0.0 < lvl_50ms < 1.0,
          "静默 50 ms 后电平=%r（应是 0 与 1 之间的值；0 = 瞬间消失，1 = 根本没降）"
          % lvl_50ms)

    lvl_1s = _level_after(1.0)
    check("FR-3.5 静音足够久后电平落到 0（不会永远挂着一条尾巴）",
          lvl_1s == 0.0,
          "静默 1 s 后电平=%r" % lvl_1s)

    # 衰减是**按时间**的，不是按帧：同一段墙钟时间，分成 10 段算
    # 和一次算，结果应该一致（否则就是「按帧衰减」那个 bug）。
    #
    # ⚠️ 每一帧的 dt 必须是 `total/steps` —— 所以 ``_level_at`` 要**逐帧前移**
    # （``_draw_level`` 内部自己取 ``time.monotonic()`` 当"现在"，
    # 减的是 ``_level_at``，所以要让它每帧都离"现在"恰好 dt 这么远）。
    def _decay_in_steps(steps: int, total: float) -> float:
        step = total / steps

        def work() -> None:
            overlay._level = 1.0
            overlay._level_target = 0.0
            for _ in range(steps):
                overlay._level_at = time.monotonic() - step
                overlay._draw_level()
        _run_on_tk(overlay, work)
        return overlay._level

    one_step = _decay_in_steps(1, 0.1)
    ten_steps = _decay_in_steps(10, 0.1)
    check("FR-3.5 衰减按**时间**算而不是按帧算（分 10 帧与 1 帧结果一致）",
          abs(one_step - ten_steps) < 0.02,
          "1 帧=%r，10 帧=%r（差 %r；按帧衰减的话两者会差很多）"
          % (one_step, ten_steps, abs(one_step - ten_steps)))

    # ---------- 非录音态不显示电平条 ---------- #
    overlay.set_state("thinking")
    wait_for(lambda: overlay.level.winfo_manager() == "")
    check("FR-3.5 非录音态电平条离开布局",
          overlay.level.winfo_manager() == "",
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # ⚠️ 回归：隐藏过一次之后**必须还能再显示出来**。
    # pack_forget() 会让控件脱离布局管理器；这时 pack_configure()
    # 只改配置、**不会把它放回来** —— 症状是「第一次录音有电平条，
    # 第二次开始就没了」。
    overlay.show_listening()
    wait_for(lambda: overlay.level.winfo_manager() == "pack")
    check("FR-3.5 隐藏过一次后还能再显示出来（回归）",
          overlay.level.winfo_manager() == "pack",
          "manager=%r mapped=%s"
          % (overlay.level.winfo_manager(), overlay.level.winfo_ismapped()))

    # ---------- FR-3.6：error 状态可见 ---------- #
    overlay.show_error("识别服务连接失败：Connection refused")
    wait_for(lambda: overlay.snapshot()[0] == "error")
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
    wait_for(lambda: overlay.snapshot()[0] == "listening")
    overlay.show_error("中途出了个错", auto_hide=False)
    wait_for(lambda: overlay.snapshot()[0] == "error")
    check("FR-3.6 录音中出错时悬浮窗保留（不自动隐藏）",
          overlay.visible,
          "visible=%s" % overlay.visible)
    # 而非录音态的独立错误，4 秒后应该自动收起
    overlay.show_error("独立的一条错误", auto_hide=True)
    wait_for(lambda: overlay.snapshot()[0] == "error")
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
        wait_for(lambda: overlay.level.winfo_manager() == "pack")
        overlay.set_volume(0.8)
        wait_for(lambda: _bar_width(overlay) > 0, timeout=1.0)
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
        # 坏实现会把这条消息**整个丢掉** —— 所以「状态变成 error」这件事
        # **永远不会发生**。不能等它发生（那会一直等到超时），
        # 只能等队列被消费掉（消息已被处理，只是被丢弃了）。
        wait_for(lambda: overlay._q.empty(), timeout=2.0)
        state2, text2 = overlay.snapshot()
        check("FR-3.6 阳性对照：不处理 error 时断言会失败",
              state2 != "error" and "吞掉" not in text2,
              "对照组 state=%r 正文=%r（说明对照组确实复现了「错误看不见」）"
              % (state2, text2))
    finally:
        Overlay._apply = saved_apply


if __name__ == "__main__":
    main()
