#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CherryVoice · 准备本地 git 仓库（为发布做准备）。

做的事：

1. ``git init``（分支 ``main``，已存在则跳过）；
2. ``git add -A``，然后**打印真正会被提交的文件清单** —— 这是验证
   ``.gitignore`` 真的挡住了 ``.venv`` / ``build`` / 配置文件的硬证据；
3. 检查提交身份；有身份就提交并打 tag，没有就**停下并打印你要执行的命令**
   （不替你编一个假身份写进历史）。

用法::

    python tools/git_prepare.py                 # 准备 + 打印将要提交的内容
    python tools/git_prepare.py --commit        # 有身份时提交并打 v1.0.0 tag
    python tools/git_prepare.py --tag v1.0.0
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_CANDIDATES = [
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    "/usr/bin/git",
]


def find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for cand in GIT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def run(git: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([git, *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args),
                                              (proc.stderr or proc.stdout).strip()))
    return proc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="有身份时提交并打 tag")
    parser.add_argument("--tag", default="v1.0.0")
    parser.add_argument("--message", default="", help="提交信息（默认自动生成）")
    args = parser.parse_args()

    git = find_git()
    if not git:
        print("找不到 git 可执行文件。请先安装 Git for Windows。")
        return 2
    print("git: %s" % git)
    print("版本：%s" % run(git, "--version").stdout.strip())
    print("仓库：%s\n" % ROOT)

    # ---- 1) init ---- #
    if (ROOT / ".git").exists():
        print("已有 .git，跳过 init")
        branch = run(git, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        print("当前分支：%s" % (branch or "(无提交)"))
    else:
        run(git, "init", "-b", "main")
        print("已执行 git init -b main")
        # 这些仓库级设置能让中文路径与文件名正常显示
        run(git, "config", "core.quotepath", "false")
        run(git, "config", "core.autocrlf", "input", check=False)

    # ---- 2) add + 展示真正会被提交的内容 ---- #
    run(git, "add", "-A")
    porcelain = run(git, "status", "--porcelain").stdout
    staged = [line for line in porcelain.splitlines() if line and not line.startswith("??")]
    untracked = [line[3:] for line in porcelain.splitlines() if line.startswith("??")]

    files = run(git, "diff", "--cached", "--name-only").stdout.split()
    print("=" * 74)
    print("将要提交的文件（%d 个）：" % len(files))
    for f in sorted(files):
        print("  %s" % f)

    # 关键断言：这些东西绝不能出现在提交里
    forbidden = [".venv/", "build/", "dist/", "__pycache__", ".gguf", ".safetensors",
                 "config.json", "client.log", "native.log"]
    leaked = [f for f in files if any(bad in f for bad in forbidden)]
    print("=" * 74)
    if leaked:
        print("[FAIL] .gitignore 没挡住这些，绝不能提交：")
        for f in leaked:
            print("   ! %s" % f)
        return 1
    print("[PASS] .venv / build / 模型权重 / 本地配置 / 日志 都没有进入暂存区")
    if untracked:
        print("未跟踪（已被忽略或被规则挡住，共 %d 项）：" % len(untracked))
        for f in untracked[:10]:
            print("   - %s" % f)
        if len(untracked) > 10:
            print("   … 其余 %d 项" % (len(untracked) - 10))

    # ---- 3) 身份检查 ---- #
    print("=" * 74)
    name = run(git, "config", "user.name", check=False).stdout.strip()
    email = run(git, "config", "user.email", check=False).stdout.strip()
    print("提交身份：name=%r email=%r" % (name or None, email or None))

    if not args.commit:
        print("\n（未加 --commit，只做准备。确认上面的清单没问题后再提交。）")
        return 0

    if not name or not email:
        print("\n[停] 没有配置提交身份。**我不会替你编一个假身份写进提交历史**。")
        print("请先执行（换成你自己的信息）：")
        print('  git config --global user.name  "你的名字"')
        print('  git config --global user.email "you@example.com"')
        print("然后重新运行：python tools/git_prepare.py --commit")
        print("\n文件已经 staged，你也可以直接自己提交：")
        print('  git commit -m "CherryVoice v1.0.0: 流式语音输入法首发"')
        return 0

    has_staged = bool(run(git, "diff", "--cached", "--name-only").stdout.strip())
    if not has_staged:
        print("没有待提交的改动，跳过 commit")
    else:
        message = args.message
        if not message:
            subject = run(git, "log", "--oneline", "-1", check=False).stdout.strip()
            message = ("CherryVoice v1.0.0: Windows 流式语音输入法\n\n"
                       "- WSL 流式识别服务（WebSocket，包装 Confucius4-R2T2 llama.cpp 后端）\n"
                       "- Windows 客户端：全局热键 / 16k 采集 / 不抢焦点悬浮窗\n"
                       "- LLM 整段修正（失败分类 + 输出校验）\n"
                       "- 流式双语翻译（增量分段，只翻译新增部分）\n"
                       "- 文本注入（剪贴板 + Ctrl+V，逐字键入兜底，剪贴板保护）\n"
                       "- 设置界面 / PyInstaller 打包 / 发布前安全检查"
                       if not subject else
                       "补充：发布脚本、发布说明、git 化的发布前检查")
        run(git, "-c", "user.name=%s" % name, "-c", "user.email=%s" % email,
            "commit", "-m", message)
        print("已提交：%s" % message.splitlines()[0])

    existing_tag = run(git, "tag", "--list", args.tag, check=False).stdout.strip()
    if existing_tag:
        print("tag %s 已存在，跳过" % args.tag)
    else:
        run(git, "tag", "-a", args.tag, "-m", "CherryVoice %s" % args.tag)
        print("已打 tag %s" % args.tag)

    print("\n最近提交：")
    print(run(git, "log", "--oneline", "-3").stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
