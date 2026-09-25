# -*- coding: utf-8 -*-
"""CherryVoice · P2 关键验证：悬浮窗**不抢焦点**。

需求文档把它列为「最该先验证的」：悬浮窗一旦抢走前台焦点，
后续文本注入就会打到错误的窗口。

**怎么做到不靠肉眼、也不靠运气**：

1. 记录 ``(前台窗口, 其线程, 该线程的键盘焦点窗口)`` 三元组作为基线；
2. 显示悬浮窗并持续灌入增长的文字（模拟流式识别），期间多次采样；
3. 要求三元组**始终不变** —— 其中"键盘焦点窗口"是最硬的指标，
   即使 Windows 前台锁让我们没法自己指定前台窗口，它依然有效；
4. 断言 ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`` 加在**顶层窗口**上
   （这是踩过的坑：``winfo_id()`` 给的是 ``TkChild``，加错了地方等于没加）；
5. **阳性对照**：另外开一个普通可激活窗口并强行激活，看检测器能不能发现变化。
   如果连阳性对照都看不出变化，说明本次环境的检测是无效的，会如实报出来；
6. 顺便把悬浮窗自己那块矩形截图存下来，供人眼确认视觉效果。

退出码 0 = 全部通过。截图路径由 ``--shot`` 指定。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import win32 as w32          # noqa: E402
from client.overlay import Overlay       # noqa: E402

WS_EX_TOPMOST = 0x00000008
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

SAMPLE_TEXT = [
    "舰长",
    "舰长，升级",
    "舰长，升级 https",
    "舰长，升级 https 之后，再次测试微软语音服务，",
    "舰长，升级 https 之后，再次测试微软语音服务，这是一段中文语音测试。",
]


def describe(hwnd: int) -> str:
    if not hwnd:
        return "(无)"
    return "0x%X [%s] pid=%d %r" % (hwnd, w32.class_name(hwnd), w32.window_pid(hwnd),
                                    w32.window_title(hwnd)[:40])


def grab(rect: tuple[int, int, int, int], path: Path) -> bool:
    """只截悬浮窗自己那块矩形（不截全屏，避免碰到用户其它窗口内容）。"""
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return False
    ps = (
        'Add-Type -AssemblyName System.Windows.Forms,System.Drawing;'
        'Add-Type -MemberDefinition \'[DllImport("user32.dll")] public static extern bool '
        'SetProcessDPIAware();\' -Name U -Namespace W;'
        '[W.U]::SetProcessDPIAware() | Out-Null;'
        '$bmp = New-Object System.Drawing.Bitmap %d, %d;'
        '$g = [System.Drawing.Graphics]::FromImage($bmp);'
        '$g.CopyFromScreen(%d, %d, 0, 0, $bmp.Size);'
        '$bmp.Save("%s", [System.Drawing.Imaging.ImageFormat]::Png);'
        '$g.Dispose(); $bmp.Dispose()'
    ) % (w, h, x, y, str(path).replace("\\", "\\\\"))
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=90)
        return r.returncode == 0 and path.exists()
    except (OSError, subprocess.SubprocessError):
        return False


class FocusTest:
    def __init__(self, shot: Path | None) -> None:
        self.shot = shot
        self.results: list[tuple[str, bool, str]] = []
        self.overlay = Overlay(width=780)
        self.root = self.overlay.root

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))

    # ------------------------------------------------------------------ #

    def run(self) -> int:
        threading.Thread(target=self._sequence, daemon=True).start()
        self.root.mainloop()
        return self._report()

    def _sequence(self) -> None:
        try:
            base = w32.foreground_focus_state()
            print("基线：前台=%s" % describe(base[0]))
            print("      键盘焦点=%s（线程 %d）" % (describe(base[2]), base[1]))

            self.overlay.show_listening()
            time.sleep(0.5)

            top = self.overlay.top_hwnd
            self.check("悬浮窗顶层窗口已识别", bool(top) and top != self.overlay.hwnd,
                       "winfo_id=0x%X → 顶层=0x%X [%s]"
                       % (self.overlay.hwnd or 0, top or 0, w32.class_name(top or 0)))
            self.check("顶层窗口含 WS_EX_NOACTIVATE",
                       bool(self.overlay.exstyle & w32.WS_EX_NOACTIVATE),
                       "顶层 exstyle=0x%08X" % self.overlay.exstyle)
            self.check("顶层窗口含 WS_EX_TOOLWINDOW（不进 Alt+Tab）",
                       bool(self.overlay.exstyle & w32.WS_EX_TOOLWINDOW),
                       "顶层 exstyle=0x%08X" % self.overlay.exstyle)
            self.check("顶层窗口置顶", bool(self.overlay.exstyle & WS_EX_TOPMOST),
                       "顶层 exstyle=0x%08X" % self.overlay.exstyle)
            self.check("悬浮窗可见", bool(w32.user32.IsWindowVisible(top or 0)))

            # 边灌文字边采样
            fg_stolen, focus_stolen = [], []
            for i, text in enumerate(SAMPLE_TEXT):
                self.overlay.set_text(text)
                time.sleep(0.4)
                fg, _tid, focus = w32.foreground_focus_state()
                if fg != base[0]:
                    fg_stolen.append("第%d次采样：前台 → %s" % (i + 1, describe(fg)))
                if focus != base[2]:
                    focus_stolen.append("第%d次采样：焦点 → %s" % (i + 1, describe(focus)))
            n = len(SAMPLE_TEXT)
            self.check("文字流式增长期间前台窗口始终未变", not fg_stolen,
                       "; ".join(fg_stolen) or "%d 次采样全部保持" % n)
            self.check("文字流式增长期间键盘焦点始终未被抢走", not focus_stolen,
                       "; ".join(focus_stolen) or "%d 次采样全部保持" % n)

            self.overlay.set_state("thinking")
            time.sleep(0.4)
            fg, _tid, focus = w32.foreground_focus_state()
            self.check("切换状态（🎙→✨）后仍未抢焦点",
                       fg == base[0] and focus == base[2],
                       "前台=%s 焦点=%s" % (describe(fg), describe(focus)))

            # 截图（人眼确认视觉效果）
            if self.shot:
                rect = self.overlay.screen_rect()
                ok = grab(rect, self.shot)
                self.check("悬浮窗截图已保存", ok,
                           "%s rect=%s" % (self.shot, rect) if ok else "截图失败")

            # 阳性对照：普通可激活窗口应当**能**被发现变化
            ctl = tk.Toplevel(self.root)
            ctl.title("CV positive control")
            ctl.geometry("360x160+120+120")
            tk.Label(ctl, text="positive control", font=("Microsoft YaHei UI", 12)).pack(expand=True)
            ctl.update()
            ctl_hwnd = w32.top_level_window(int(ctl.winfo_id()))
            w32.user32.ShowWindow(w32.wt.HWND(ctl_hwnd), w32.SW_SHOWNORMAL)
            w32.force_foreground(ctl_hwnd)
            time.sleep(0.5)
            fg2, _t2, focus2 = w32.foreground_focus_state()
            detected = (fg2 != base[0]) or (focus2 != base[2])
            self.check("阳性对照：检测器能发现真正的抢焦点行为", detected,
                       "对照窗口=%s → 前台=%s 焦点=%s"
                       % (describe(ctl_hwnd), describe(fg2), describe(focus2))
                       if detected else
                       "注意：本环境有前台锁，对照组也没能改变前台/焦点，"
                       "因此上面的『未抢焦点』结论在本次运行中**未被阳性验证**")
            ctl.destroy()
        except Exception as exc:  # noqa: BLE001
            self.check("执行过程中未抛异常", False, repr(exc))
        finally:
            self.root.after(0, self.overlay.stop)

    def _report(self) -> int:
        print("=" * 72)
        print("P2 关键验证：悬浮窗不抢焦点")
        print("=" * 72)
        allok = True
        for name, ok, detail in self.results:
            allok &= ok
            print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
            if detail:
                print("        %s" % detail)
        passed = sum(1 for _n, ok, _d in self.results if ok)
        print("-" * 72)
        print("结论：%s（%d/%d 通过）"
              % ("全部通过" if allok else "存在失败项", passed, len(self.results)))
        return 0 if allok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shot", default="", help="把悬浮窗矩形截图保存到该路径（png）")
    args = parser.parse_args()
    shot = Path(args.shot) if args.shot else None
    return FocusTest(shot).run()


if __name__ == "__main__":
    sys.exit(main())
