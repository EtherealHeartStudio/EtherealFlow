#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CherryVoice 可执行入口（专门给 PyInstaller 用）。

为什么单独放一个文件：PyInstaller 需要一个**脚本路径**作为入口，
不支持 ``python -m client.app``。这里只做一件事：转调 ``client.app.main()``。

开发时仍然用 ``python -m client.app``，两者行为一致。
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

# 冻结后 __file__ 在临时解包目录里，需要把项目根加进 sys.path 才能 import client
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from client.app import main  # noqa: E402

if __name__ == "__main__":
    multiprocessing.freeze_support()      # 打包后避免子进程反复拉起自己
    sys.exit(main())
