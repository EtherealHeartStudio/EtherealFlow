#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EtherealFlow · 打包成免安装的 Windows 可执行文件。

用法（在仓库根目录）::

    .venv\\Scripts\\python.exe scripts\\build_exe.py                # 目录版（默认）
    .venv\\Scripts\\python.exe scripts\\build_exe.py --onefile      # 单文件版
    .venv\\Scripts\\python.exe scripts\\build_exe.py --console      # 保留控制台窗口，便于排查

默认打 **目录版 + 无控制台**：

* 目录版启动快、被杀软误报的概率低（单文件版每次启动都要解包）；
* 无控制台是因为它是输入法，不该常驻一个黑框 —— 日志已经同时落到
  ``%APPDATA%/EtherealFlow/logs/client.log``，出问题看那个文件。

``--collect-all sounddevice`` 是必须的：PortAudio 的 DLL 是包内数据文件，
不显式收集的话打出来的 exe 一运行就报找不到库。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "run_client.py"
APP_NAME = "EtherealFlow"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 EtherealFlow")
    parser.add_argument("--onefile", action="store_true", help="打成单个 exe（启动稍慢）")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排查用）")
    parser.add_argument("--name", default=APP_NAME)
    parser.add_argument("--keep-build", action="store_true", help="保留 build/ 中间产物")
    args = parser.parse_args()

    if not ENTRY.exists():
        print("找不到入口脚本：%s" % ENTRY)
        return 2

    build_dir = ROOT / "build"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", args.name,
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.console else "--windowed",
        "--distpath", str(build_dir / "dist"),
        "--workpath", str(build_dir / "work"),
        "--specpath", str(build_dir),
        # PortAudio 的 DLL 在 sounddevice 包里，必须一起收进去
        "--collect-all", "sounddevice",
        # tkinter 的 tcl/tk 数据由 PyInstaller 的 hook 处理，这里只确保模块被打进来
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.messagebox",
        # 用不到的重家伙，排除掉能显著减小体积
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--exclude-module", "pytest",
        str(ENTRY),
    ]
    print("执行：\n  %s\n" % " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print("\n打包失败（PyInstaller 返回 %d）" % proc.returncode)
        return proc.returncode

    target = (build_dir / "dist" / (args.name + (".exe" if args.onefile else "")))
    if not target.exists():
        print("\n打包命令成功但找不到产物：%s" % target)
        return 3

    if not args.keep_build:
        shutil.rmtree(build_dir / "work", ignore_errors=True)

    size_mb = (target.stat().st_size / 1024 / 1024 if args.onefile else
               sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
               / 1024 / 1024)
    print("\n%s" % ("=" * 70))
    print("打包完成：%s" % target)
    print("体积：%.1f MB（%s）" % (size_mb, "单文件" if args.onefile else "目录版"))
    print("自检：%s --list-devices" % (target if args.onefile else target / (args.name + ".exe")))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
