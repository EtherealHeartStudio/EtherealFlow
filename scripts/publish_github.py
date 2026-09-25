#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CherryVoice · 一键发布到 GitHub。

用法::

    python scripts/publish_github.py --repo 用户名/仓库名              # 推送
    python scripts/publish_github.py --repo 用户名/仓库名 --create     # 顺便用 gh 建 Release

它会**按顺序把关**，任何一步不过就停下，不会把不该推的东西推上去：

1. 跑 ``tools/check_release.py`` —— 有 BLOCKER 直接拒绝；
2. 检查工作区是否干净（未提交的改动不会悄悄溜进去）；
3. 检查本地有没有提交、有没有 tag；
4. 配置 ``origin`` 并推送 ``main`` 与所有 tag；
5. 若本机有 ``gh`` 且加了 ``--create``，就用 ``gh release create`` 建 Release。

注意：**推送需要你自己的 GitHub 凭据**（HTTPS 走凭据管理器 / SSH key），
这一步脚本不代劳，也不会去碰你的 token。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_CANDIDATES = [r"C:\Program Files\Git\cmd\git.exe", "/usr/bin/git"]


def tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for cand in GIT_CANDIDATES:
        if name == "git" and Path(cand).exists():
            return cand
    for cand in (r"C:\Program Files\GitHub CLI\gh.exe",):
        if name == "gh" and Path(cand).exists():
            return cand
    return None


def run(exe: str, *args: str, check: bool = False, timeout: int = 300):
    proc = subprocess.run([exe, *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError("%s %s 失败：%s" % (Path(exe).name, " ".join(args),
                                              (proc.stderr or proc.stdout).strip()))
    return proc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="GitHub 仓库，形如 用户名/仓库名")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--create", action="store_true", help="用 gh 创建 Release")
    parser.add_argument("--notes", default="RELEASE_NOTES.md")
    parser.add_argument("--skip-check", action="store_true", help="跳过发布前安全检查（不推荐）")
    args = parser.parse_args()

    git = tool("git")
    if not git:
        print("找不到 git。")
        return 2
    if "/" not in args.repo or args.repo.count("/") != 1:
        print("--repo 格式应为 用户名/仓库名，例如 octocat/CherryVoice")
        return 2

    # ---- 1) 发布前安全检查 ---- #
    if not args.skip_check:
        print("[1/5] 发布前安全检查…")
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "check_release.py")],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        print((proc.stdout or "").strip()[-1500:])
        if proc.returncode != 0:
            print("\n[停] 安全检查未通过，拒绝发布。修完再跑一次。")
            return 1
    else:
        print("[1/5] 已跳过安全检查（--skip-check）")

    # ---- 2) 工作区必须干净 ---- #
    print("\n[2/5] 检查工作区…")
    dirty = run(git, "status", "--porcelain").stdout.strip()
    if dirty:
        print("[停] 有未提交的改动，请先提交或 stash：")
        for line in dirty.splitlines()[:15]:
            print("   %s" % line)
        return 1
    print("     工作区干净")

    # ---- 3) 必须有提交和 tag ---- #
    print("\n[3/5] 检查提交与 tag…")
    if run(git, "rev-parse", "HEAD").returncode != 0:
        print("[停] 还没有任何提交。先跑：python tools/git_prepare.py --commit")
        return 1
    head = run(git, "log", "--oneline", "-1").stdout.strip()
    tags = [t for t in run(git, "tag", "--list").stdout.split() if t]
    print("     最新提交：%s" % head)
    print("     tag：%s" % (", ".join(tags) or "（无）"))

    # ---- 4) origin + push ---- #
    url = "https://github.com/%s.git" % args.repo
    print("\n[4/5] 配置 origin 并推送…")
    existing = run(git, "remote", "get-url", "origin").stdout.strip()
    if existing:
        if existing != url:
            print("     更新 origin：%s → %s" % (existing, url))
            run(git, "remote", "set-url", "origin", url, check=True)
        else:
            print("     origin 已是 %s" % url)
    else:
        run(git, "remote", "add", "origin", url, check=True)
        print("     已添加 origin = %s" % url)

    print("     正在推送分支 %s …" % args.branch)
    push = run(git, "push", "-u", "origin", args.branch)
    sys.stdout.write(push.stdout)
    sys.stderr.write(push.stderr)
    if push.returncode != 0:
        print("\n[停] 推送失败。常见原因：")
        print("  * 远端仓库还没建 —— 先去 GitHub 建一个空仓库（不要勾 README）")
        print("  * 没有凭据 —— HTTPS 需要凭据管理器里存过 token，或改用 SSH 地址")
        return 1

    if tags:
        print("     正在推送 tag …")
        tagpush = run(git, "push", "origin", "--tags")
        sys.stdout.write(tagpush.stdout)
        sys.stderr.write(tagpush.stderr)

    # ---- 5) 可选：建 Release ---- #
    print("\n[5/5] 创建 Release…")
    gh = tool("gh")
    if not args.create:
        print("     （未加 --create，跳过。也可手动在 GitHub 上建 Release）")
    elif not gh:
        print("     本机没装 gh CLI，跳过。可在 GitHub 网页上手动建 Release，"
              "把 build/dist/CherryVoice/ 打包上传。")
    else:
        notes = ROOT / args.notes
        note_args = ["--notes-file", str(notes)] if notes.exists() else ["--generate-notes"]
        tag = tags[-1] if tags else args.branch
        created = run(gh, "release", "create", tag, "--title", "CherryVoice %s" % tag,
                      *note_args)
        sys.stdout.write(created.stdout)
        sys.stderr.write(created.stderr)
        if created.returncode != 0:
            print("     建 Release 失败，可到 GitHub 网页手动创建。")

    print("\n%s" % ("=" * 70))
    print("发布完成：https://github.com/%s" % args.repo)
    print("可执行文件在 build/dist/CherryVoice/，可打包后作为 Release 附件上传。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
