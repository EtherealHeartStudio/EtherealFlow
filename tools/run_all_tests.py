#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EtherealFlow · 一键跑完所有离线自测。

这些测试**都不需要**人按键、不需要人说话、不需要真的 API Key、
也不需要识别服务或麦克风（少数项会自动降级成 SKIP 并说明原因）。

用法::

    python tools/run_all_tests.py              # 全部
    python tools/run_all_tests.py --fast       # 跳过较慢的（回放类、界面类）
    python tools/run_all_tests.py --only llm translator

退出码 0 = 没有硬失败（SKIP 不算失败）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
# 进度同时落盘：某些执行环境要等进程结束才回传 stdout，
# 万一卡住就什么都看不到；写文件的话超时后还能知道卡在哪一个。
PROGRESS = ROOT / "build" / "test_progress.log"


def note(text: str) -> None:
    print(text, flush=True)
    try:
        PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        with open(PROGRESS, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass

# (名字, 脚本, 是否较慢, 需要的额外参数)
TESTS: list[tuple[str, str, bool]] = [
    ("签名审计", "tools/test_win32_signatures.py", False),
    ("配置往返", "tools/test_settings.py", False),
    ("LLM 失败分类", "tools/test_llm.py", False),
    ("增量翻译", "tools/test_translator.py", False),
    ("音频降级路径", "tools/test_audio_fallback.py", False),
    ("热键逻辑+钩子状态机", "tools/test_hotkey.py", False),
    ("识别客户端重连", "tools/test_asr_reconnect.py", True),
    ("发布前安全检查", "tools/check_release.py", False),
    ("悬浮窗不抢焦点", "tools/test_overlay_focus.py", True),
    ("文本注入", "tools/test_injection.py", True),
]


def run_one(name: str, script: str, root: Path) -> tuple[str, bool, int, str]:
    path = root / script
    if not path.exists():
        return name, False, -1, "脚本不存在"
    t0 = time.monotonic()
    proc = subprocess.run([PY, str(path)], cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=900)
    elapsed = time.monotonic() - t0
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # 取最后几行里有价值的结论
    tail = [l for l in out.splitlines() if l.strip()][-3:]
    detail = " | ".join(tail)[-220:]
    return name, proc.returncode == 0, proc.returncode, \
        "%.1fs · %s" % (elapsed, detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="跳过较慢的测试")
    parser.add_argument("--only", nargs="*", default=[], help="只跑名字里含这些关键词的")
    args = parser.parse_args()

    selected = []
    for name, script, slow in TESTS:
        if args.fast and slow:
            continue
        if args.only and not any(k.lower() in name.lower() for k in args.only):
            continue
        selected.append((name, script))

    try:
        PROGRESS.unlink()
    except OSError:
        pass
    note("将运行 %d 个测试（Python: %s）" % (len(selected), PY))
    note("=" * 78)
    results = []
    for name, script in selected:
        note("… %s 开始" % name)
        name, ok, code, detail = run_one(name, script, ROOT)
        results.append((name, ok, code, detail))
        note("  %s %s" % ("[PASS]" if ok else "[FAIL]", detail))

    print("=" * 78)
    print("%-26s %s" % ("测试", "结果"))
    print("-" * 78)
    for name, ok, code, _d in results:
        print("%-26s %s" % (name, "通过" if ok else "失败（退出码 %d）" % code))
    failed = [n for n, ok, _c, _d in results if not ok]
    print("-" * 78)
    print("共 %d 个，通过 %d 个" % (len(results), len(results) - len(failed)))
    if failed:
        print("失败：%s" % "、".join(failed))
    print("结论：%s" % ("全部通过" if not failed else "存在失败项"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
