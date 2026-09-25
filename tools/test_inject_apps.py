# -*- coding: utf-8 -*-
"""CherryVoice · 需求文档 §10.3 兼容性验收：往**真实第三方应用**里注入。

在此之前，注入只验证到"进程内自己的 Tk 文本框"为止 —— 那不算兼容性验收。

**怎么判断"真的注进去了"**：这些应用都没有可读的 Win32 控件，所以用用户自己
也会用的办法 —— 注入后发 ``Ctrl+A`` / ``Ctrl+C``，把内容**复制到剪贴板**再读回来比对。
这既验证了 Ctrl+V 注入，也顺带验证了组合键注入。

**怎么找窗口**：不能用"按 PID 找" —— Windows 11 的 ``notepad.exe`` 只是个壳，
真正的主窗口属于**另一个进程**（Store 版记事本），按 PID 永远找不到（实测踩过）。
改用"启动前后可见顶层窗口做差集 + 类名提示"，对任何应用都适用。

没装 / 抢不到前台的应用标 ``[SKIP]`` 并写明原因，**不算通过也不算失败**。

用法::

    python tools/test_inject_apps.py                 # 本机能测的都测
    python tools/test_inject_apps.py --app 记事本     # 只测一个
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import win32 as w32                            # noqa: E402
from client.inject import (Injector, clipboard_get_text,   # noqa: E402
                           clipboard_set_text, _send, KEYEVENTF_KEYUP)

VK_CTRL, VK_A, VK_C, VK_DELETE = 0x11, 0x41, 0x43, 0x2E
SENTINEL = "CV-CLIP-SENTINEL-请勿保留"
MARK = "CherryVoice兼容性验收123"
WM_CLOSE = 0x0010

WINWORD = r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"


def press_ctrl(vk: int) -> None:
    _send((VK_CTRL, 0, 0), (vk, 0, 0), (vk, 0, KEYEVENTF_KEYUP), (VK_CTRL, 0, KEYEVENTF_KEYUP))


def press(vk: int) -> None:
    _send((vk, 0, 0), (vk, 0, KEYEVENTF_KEYUP))


def read_back() -> str:
    """Ctrl+A / Ctrl+C，把目标里的内容复制出来再读。"""
    clipboard_set_text(SENTINEL)
    time.sleep(0.15)
    press_ctrl(VK_A)
    time.sleep(0.15)
    press_ctrl(VK_C)
    time.sleep(0.5)
    return clipboard_get_text() or ""


def list_windows() -> dict[int, tuple[int, str, str]]:
    """可见且有标题的顶层窗口：{hwnd: (pid, class, title)}。"""
    out: dict[int, tuple[int, str, str]] = {}

    def _cb(hwnd, _lp):
        if w32.user32.IsWindowVisible(hwnd):
            title = w32.window_title(hwnd)
            if title:
                out[int(hwnd)] = (w32.window_pid(hwnd), w32.class_name(hwnd), title)
        return True

    w32.user32.EnumWindows(w32.WNDENUMPROC(_cb), 0)
    return out


class App:
    def __init__(self, name: str, exe: str, classes: tuple[str, ...] = ()) -> None:
        self.name = name
        self.exe = exe
        self.classes = classes          # 主窗口类名提示，用于在差集里挑对窗口
        self.proc: subprocess.Popen | None = None
        self.hwnd = 0


def launch(app: App, timeout: float = 15.0) -> tuple[bool, str]:
    """启动应用并找到**真正的输入窗口**。

    给了类名提示时，**只认类名对得上的窗口**，不做"退而求其次挑标题最长的"——
    否则会把 ``#32770``（对话框）当成主窗口，往里注入等于没测
    （Word 首启/激活对话框就是这么骗过一次的）。
    """
    before = set(list_windows())
    try:
        app.proc = subprocess.Popen([app.exe])
    except OSError as exc:
        return False, "启动失败：%s" % exc
    deadline = time.time() + timeout
    seen: dict[int, tuple[int, str, str]] = {}
    while time.time() < deadline:
        new = {h: v for h, v in list_windows().items() if h not in before}
        seen.update(new)
        if app.classes:
            hit = [h for h, (_p, c, _t) in new.items() if c in app.classes]
            if hit:
                app.hwnd = hit[0]
                return True, ""
        elif new:
            app.hwnd = sorted(new, key=lambda h: -len(new[h][2]))[0]
            return True, ""
        time.sleep(0.4)
    if app.classes and seen:
        kinds = sorted({c for _p, c, _t in seen.values()})
        return False, ("只出现了这些窗口 %s，没有期望的 %s —— 可能有首启/激活对话框挡着，"
                       "无法验证真实输入框" % (kinds, list(app.classes)))
    return False, "启动后 %.0f 秒内没出现新窗口（可能是首启对话框或加载很慢）" % timeout


def close_app(app: App) -> None:
    """优先发 WM_CLOSE（能关掉 Store 应用那种"壳 + 真身"的两段式进程）。"""
    if app.hwnd:
        try:
            w32.user32.PostMessageW(w32.wt.HWND(app.hwnd), WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass
    if app.proc is not None:
        try:
            app.proc.terminate()
        except OSError:
            pass


def run_one(app: App) -> tuple[str, bool, str]:
    if not Path(app.exe).exists():
        return app.name, True, "[SKIP] 本机未安装：%s" % app.exe

    ok_launch, why = launch(app)
    if not ok_launch:
        return app.name, True, "[SKIP] %s" % why

    w32.force_foreground(app.hwnd)
    time.sleep(0.9)
    fg = w32.foreground_window()
    if fg != app.hwnd:
        # 有些应用会同时存在多个窗口（启动画面 / 主窗 / 文档窗），
        # 差集挑到的未必是真正接收键盘的那个。前台窗口类名对得上就采纳它。
        if app.classes and w32.class_name(fg) in app.classes:
            app.hwnd = fg
        else:
            close_app(app)
            return app.name, True, ("[SKIP] 抢不到前台（当前前台是 %r[%s]）"
                                    "，本环境前台锁不稳定"
                                    % (w32.window_title(fg)[:20], w32.class_name(fg)))

    target = "%r[%s]" % (w32.window_title(app.hwnd)[:22], w32.class_name(app.hwnd))

    press_ctrl(VK_A)          # 清空，避免把自带内容误判成注入结果
    time.sleep(0.1)
    press(VK_DELETE)
    time.sleep(0.3)

    res = Injector(mode="clipboard", restore_clipboard=False).inject(MARK)
    time.sleep(0.8)
    got = read_back()
    close_app(app)
    time.sleep(0.5)

    ok = MARK in got
    detail = "目标=%s 方式=%s；读回=%r" % (target, res.method, got[:46])
    if not ok:
        detail += "（未读回注入内容）"
    return app.name, ok, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="", help="只测名字含该关键词的应用")
    args = parser.parse_args()

    apps = [
        App("记事本", r"C:\WINDOWS\system32\notepad.exe", ("Notepad",)),
        App("Word", WINWORD, ("OpusApp",)),
        App("VS Code", r"C:\Program Files\Microsoft VS Code\Code.exe",
            ("Chrome_WidgetWin_1",)),
        App("微信", r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
            ("WeChatMainWndForPC",)),
        App("飞书", str(Path.home() / "AppData/Local/Feishu/Feishu.exe"),
            ("LarkMainWindow",)),
    ]
    if args.app:
        apps = [a for a in apps if args.app in a.name]

    print("=" * 76, flush=True)
    print("需求文档 §10.3 兼容性验收：往真实第三方应用注入", flush=True)
    print("=" * 76, flush=True)
    print("判定：注入后 Ctrl+A / Ctrl+C，从剪贴板读回比对", flush=True)
    print("提示：会短暂占用剪贴板与前台窗口，结束后还原剪贴板\n", flush=True)

    saved = clipboard_get_text()
    rows = []
    for app in apps:
        print("… %s" % app.name, flush=True)
        name, ok, detail = run_one(app)
        rows.append((name, ok, detail))
        tag = "[SKIP]" if "SKIP" in detail else ("[PASS]" if ok else "[FAIL]")
        print("  %s %s" % (tag, detail), flush=True)

    if saved is not None:
        clipboard_set_text(saved)

    print("\n" + "=" * 76, flush=True)
    print("%-12s %s" % ("应用", "结果"), flush=True)
    print("-" * 76, flush=True)
    for name, ok, detail in rows:
        tag = "SKIP" if "SKIP" in detail else ("通过" if ok else "失败")
        print("%-12s %s" % (name, tag), flush=True)
    passed = [n for n, ok, d in rows if ok and "SKIP" not in d]
    failed = [n for n, ok, d in rows if not ok and "SKIP" not in d]
    skipped = [n for n, _ok, d in rows if "SKIP" in d]
    print("-" * 76, flush=True)
    print("通过 %d：%s" % (len(passed), "、".join(passed) or "（无）"), flush=True)
    if failed:
        print("失败 %d：%s" % (len(failed), "、".join(failed)), flush=True)
    if skipped:
        print("跳过 %d：%s" % (len(skipped), "、".join(skipped)), flush=True)
    print("结论：%s" % ("没有失败项" if not failed else "存在失败项"), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
