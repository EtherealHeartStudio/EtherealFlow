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

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

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
    # ↓ S4 新增：``deep_merge`` 改真深拷贝（BF-5）+ 逐字段类型校验（BF-6）。
    # 坏配置以前会崩在启动（BF-6），而浅拷贝会污染全局默认值（BF-5）。
    ("配置深拷贝+类型校验", "tools/test_config.py", False),
    ("LLM 失败分类", "tools/test_llm.py", False),
    ("增量翻译", "tools/test_translator.py", False),
    ("音频降级路径", "tools/test_audio_fallback.py", False),
    ("热键逻辑+钩子状态机", "tools/test_hotkey.py", False),
    ("识别客户端重连", "tools/test_asr_reconnect.py", True),
    ("发布前安全检查", "tools/check_release.py", False),
    ("悬浮窗不抢焦点", "tools/test_overlay_focus.py", True),
    ("文本注入", "tools/test_injection.py", True),
    # 需要一个**真实存在**的模型。没有可用的模型时它自报 [SKIP] 并退出 0，
    # 所以默认跑套件不会因为"用户还没配 LLM"而变红；想强制要求时加 --require。
    ("LLM 真机（修正/翻译）", "tools/test_llm_live.py", True),
    # ↓ 这两个以前**不在套件里**，所以 v0.1.0–v0.3.0 的 CI 从来没跑过它们。
    # 2026-10-24 把它们加进来，立刻暴露出「悬浮窗在创建/显示时抢焦点」
    # 这个会让产品「说了话但什么都没进去」的严重缺陷。
    # 缺识别服务时它们会明确 SKIP 并退出 0（见 tools/_deps.py）。
    ("完整闭环（识别→修正→注入）", "tools/test_closed_loop.py", True),
    ("翻译接入主流程", "tools/test_translate_flow.py", True),
    # ↓ S2 新增的三个回归用例。每一个都验证过一个**已发布版本里存在**的缺陷：
    #   · 轮转丢内容（说超 30 秒丢 32%）
    #   · Esc 取消是死代码（文档写了，代码里从不触发）
    #   · 热词没进识别引擎（界面和 README 都说传了，实际没有）
    # 缺识别服务时它们会明确 SKIP 并退出 0（见 tools/_deps.py）。
    ("长语音轮转不丢内容", "tools/test_rotate_no_loss.py", True),
    ("Esc 取消不注入", "tools/test_cancel_no_inject.py", True),
    ("热词到达识别引擎", "tools/test_hotwords_reach_asr.py", False),
    # ↓ S4 新增。每一个都验证一个**已发布版本里存在**的缺陷：
    #   · 麦克风尾部音频每次都丢（松手前没 flush）
    #   · 麦克风设备只能手改配置文件（序号会漂移，选错就没反应）
    #   · 设置改完必须重启才生效（LLM 客户端不会热重建）
    #   · 悬浮窗没有音量电平条，也没有 error 状态（出错只有日志里看得到）
    ("麦克风设备+设置热更新", "tools/test_audio_and_hot_reload.py", False),
    ("悬浮窗电平条+错误状态", "tools/test_overlay_states.py", True),
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
    parser.add_argument("--only", nargs="*", default=[],
                        help="只跑名字里含这些关键词的（匹配中文显示名或脚本名）")
    args = parser.parse_args()

    selected = []
    for name, script, slow in TESTS:
        if args.fast and slow:
            continue
        if args.only:
            # 同时匹配**中文显示名**和**脚本名** —— 只匹配显示名的话，
            # `--only translator` 一个都选不中（脚本叫 test_translator.py，
            # 显示名是「增量翻译」），却会「成功」地跑 0 个测试并退出 0，
            # 看起来像全绿，其实什么都没跑。静默跳过是这类工具最危险的坑。
            hay = (name + " " + script).lower()
            if not any(k.lower() in hay for k in args.only):
                continue
        selected.append((name, script))

    if args.only and not selected:
        print("没有任何测试匹配 %s —— 可用关键词：" % " ".join(args.only))
        for name, script, _s in TESTS:
            print("  %-28s %s" % (name, script))
        return 2

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
