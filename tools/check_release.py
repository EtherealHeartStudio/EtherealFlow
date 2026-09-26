# -*- coding: utf-8 -*-
"""EtherealFlow · 发布前安全检查（需求文档 §11.3）。

    [ ] 全文搜索确认无 API Key / Token / 个人路径泄露
    [ ] 硬编码的本地路径改为可配置
    [ ] README 说明依赖的外部服务
    [ ] 说明模型权重的 License

这个脚本把上面几条变成**可重复执行的检查**，而不是靠人肉 grep。

用法::

    python tools/check_release.py              # 扫仓库
    python tools/check_release.py --root .     # 指定根目录

退出码：0 = 没有 BLOCKER；1 = 有 BLOCKER（不允许发布）。
WARN 只是提醒，不阻断发布，但应当逐条确认。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "logs",
             ".mypy_cache", ".pytest_cache", "node_modules"}
TEXT_EXT = {".py", ".md", ".json", ".sh", ".service", ".txt", ".yaml", ".yml",
            ".toml", ".cfg", ".ini", ".spec", ".bat", ".ps1", ".cmd", ""}
BIG_BINARY_EXT = {".gguf", ".safetensors", ".bin", ".pt", ".pth", ".onnx", ".mp4",
                  ".zip", ".7z", ".tar", ".gz", ".exe", ".dll", ".so", ".pyd",
                  ".wav", ".mp3", ".png", ".gif", ".jpg", ".jpeg", ".webp", ".svg",
                  ".pdf"}
# 说明：这里必须把 README 会用到的图片/动画后缀都列上。漏掉 .gif 的后果是
# 「较大的文件」那一节对演示动图完全不报告 —— 而它恰恰是最容易悄悄塞进
# 几 MB 二进制、把仓库拖肿的东西。

# ---- 规则 ---------------------------------------------------------------- #
# (名称, 严重级别, 正则, 说明, 允许出现的文件/路径片段)
RULES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("OpenAI 风格 Key", "BLOCKER", r"\bsk-[A-Za-z0-9_\-]{16,}",
     "疑似真实 API Key", ()),
    ("GitHub Token", "BLOCKER", r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}",
     "疑似 GitHub Token", ()),
    ("AWS Key", "BLOCKER", r"\bAKIA[0-9A-Z]{16}\b", "疑似 AWS Access Key", ()),
    ("私钥", "BLOCKER", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥内容", ()),
    ("Bearer 长串", "BLOCKER", r"Bearer\s+[A-Za-z0-9\-_\.]{24,}",
     "疑似真实鉴权头", ()),
    ("本机用户名路径", "BLOCKER", r"[A-Za-z]:\\Users\\(?!<|%|\$)[A-Za-z0-9_.\-]+",
     "个人绝对路径（应改为可配置或用占位符）", ()),
    ("本机工作区路径", "BLOCKER", r"AgentWorkSpace|粉樱桃_Laptop", "个人工作区路径泄露", ()),  # release-check: allow
    ("WSL 专用账号路径", "WARN", r"/home/(?!user\b|r2t2\b)[a-z0-9_]+/",
     "个人 home 路径（r2t2 是文档规定的固定账号，允许）", ()),
    ("空 API Key 字段以外的赋值", "WARN",
     r"""["']api_key["']\s*[:=]\s*["'][^"']{8,}["']""",
     "疑似硬编码的 API Key", ()),
]

# 扫描器自己的规则字符串、以及测试里的假数据，都会命中上面的模式。
# 在被命中的那一行加这个注释即可豁免 —— 比维护路径白名单更不容易漏。
ALLOW_MARKER = "release-check: allow"

MUST_HAVE = ["README.md", "LICENSE", ".gitignore"]
MUST_NOT_SHIP = [".venv", "logs", "config.json", "settings.json", ".env"]
MODEL_EXT = {".gguf", ".safetensors"}


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for name in filenames:
            yield Path(dirpath) / name


def _git_exe() -> str | None:
    import shutil
    found = shutil.which("git")
    if found:
        return found
    for cand in (r"C:\Program Files\Git\cmd\git.exe", "/usr/bin/git"):
        if Path(cand).exists():
            return cand
    return None


def tracked_files(root: Path) -> tuple[list[str] | None, str]:
    """有 git 时用 ``git ls-files`` 拿到**真正会被提交**的清单。

    这比遍历目录准得多：目录遍历只能靠 .gitignore 猜，而 git 知道确切答案。
    返回 ``(相对路径列表, 来源说明)``；``None`` 表示 git 不可用，退回遍历。
    """
    if not (root / ".git").exists():
        return None, "目录遍历（尚无 .git）"
    git = _git_exe()
    if not git:
        return None, "目录遍历（找不到 git）"
    try:
        proc = subprocess.run([git, "ls-files"], cwd=str(root), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            return None, "目录遍历（git ls-files 失败）"
        files = [line for line in proc.stdout.splitlines() if line.strip()]
        return files, "git ls-files（%d 个已跟踪文件）" % len(files)
    except (OSError, subprocess.SubprocessError):
        return None, "目录遍历（git 调用异常）"


def untracked_not_ignored(root: Path) -> list[str]:
    """列出**会被提交但还没 add** 的文件（用 git status，比 gitingore 猜测可靠）。"""
    git = _git_exe()
    if not git or not (root / ".git").exists():
        return []
    try:
        proc = subprocess.run([git, "ls-files", "--others", "--exclude-standard"],
                              cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return [l for l in proc.stdout.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def line_ending_problems(root: Path) -> tuple[list[str], list[str]]:
    """盯住 shell 脚本的行尾。

    为什么专门查 ``.sh``：CRLF 的 shell 脚本交给 bash 会在第一行
    ``set -euo pipefail\\r`` 上报错，而报错信息并不指向行尾，排查很费时间。
    本项目的部署路径正是「Windows 上编辑、WSL 里 bash 执行」，
    所以仓库里一旦存进 CRLF 的 .sh，别人 clone 下来就会踩到。

    看的是 ``git ls-files --eol`` 里的 **index** 那一列（``i/lf``）：
    它才是真正被提交、被别人拿到的内容，且不受本机 ``core.autocrlf`` 影响。
    """
    git = _git_exe()
    if not git or not (root / ".git").exists():
        return [], []
    try:
        proc = subprocess.run([git, "ls-files", "--eol"], cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return [], []
    blockers: list[str] = []
    warns: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        fields = parts[0].split()
        path = parts[1].strip()
        idx = next((f for f in fields if f.startswith("i/")), "")
        wtree = next((f for f in fields if f.startswith("w/")), "")
        if not path.endswith(".sh"):
            continue
        if idx != "i/lf":
            blockers.append("%s 在 git 索引里的行尾是 %s —— shell 脚本必须以 LF 入库，"
                            "否则别人 clone 出来跑 bash 会在 "
                            "`set -euo pipefail\\r` 上直接失败" % (path, idx))
        if wtree not in ("w/lf", "w/-text", "w/none"):
            warns.append("%s 的工作区副本行尾是 %s —— 从 /mnt 直接喂给 WSL 的 bash 会出错。"
                         "修法：git rm --cached -r . && git reset --hard"
                         % (path, wtree))
    return blockers, warns


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--quiet", action="store_true", help="只输出问题")
    args = parser.parse_args()
    root = Path(args.root).resolve()

    blockers: list[str] = []
    warns: list[str] = []
    infos: list[str] = []
    scanned = 0
    big_files: list[tuple[str, float]] = []
    gi = root / ".gitignore"

    print("扫描仓库：%s" % root)
    print("=" * 76)

    tracked, source = tracked_files(root)
    if tracked is not None:
        file_list = [root / p for p in tracked]
        print("文件清单来源：%s ← 这才是**真正会被提交**的东西" % source)
    else:
        file_list = list(iter_files(root))
        print("文件清单来源：%s" % source)

    for path in sorted(file_list):
        r = rel(path, root)
        if not path.exists():
            continue
        scanned += 1
        size_mb = path.stat().st_size / 1024 / 1024

        if path.suffix.lower() in BIG_BINARY_EXT or size_mb > 5:
            big_files.append((r, size_mb))
        if size_mb > 20 or path.suffix.lower() in MODEL_EXT:
            blockers.append("%s：大文件/模型权重（%.1f MB）不应提交" % (r, size_mb))
            continue
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, level, pattern, why, allow in RULES:
            if any(a in r for a in allow):
                continue
            for m in re.finditer(pattern, content):
                line_no = content[:m.start()].count("\n") + 1
                line = content.splitlines()[line_no - 1] if content else ""
                if ALLOW_MARKER in line:
                    continue
                snippet = m.group(0)
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                entry = "%s:%d  %s → %r" % (r, line_no, why, snippet)
                (blockers if level == "BLOCKER" else warns).append(entry)

    # 必备文件
    for name in MUST_HAVE:
        if (root / name).exists():
            infos.append("存在 %s" % name)
        else:
            (blockers if name == "LICENSE" else warns).append("缺少 %s" % name)

    # 不该出现在仓库里的东西。
    # 有 git 时直接问 git "这个路径被跟踪了吗"，这比照 .gitignore 文本猜要准。
    ignore_text = gi.read_text(encoding="utf-8", errors="ignore") if gi.exists() else ""
    for name in MUST_NOT_SHIP:
        p = root / name
        if not p.exists():
            continue
        if tracked is not None:
            tracked_here = any(t == name or t.startswith(name + "/") for t in tracked)
            if not tracked_here:
                infos.append("%s 存在但未被 git 跟踪（不会提交）" % name)
                continue
            blocker = ("仓库里存在 %s，且**已被 git 跟踪**（必须从索引里移除）" % name)
        else:
            covered = name in ignore_text or ("." + name) in ignore_text
            if covered:
                infos.append("%s 存在但已被 .gitignore 覆盖（不会提交）" % name)
                continue
            blocker = "仓库里存在 %s，且 .gitignore 未覆盖" % name
        if name == ".venv":
            blockers.append(blocker)
        elif name in ("config.json", "settings.json", ".env"):
            blockers.append(blocker + "（可能含 API Key）")
        else:
            warns.append(blocker)

    # .gitignore 覆盖检查
    gi = root / ".gitignore"
    if gi.exists():
        text = gi.read_text(encoding="utf-8", errors="ignore")
        for need, label in [("*.gguf", "模型权重"), (".venv", "虚拟环境"),
                            ("config", "本地配置"), ("*.log", "日志")]:
            if need not in text:
                warns.append(".gitignore 未覆盖 %s（%s）" % (label, need))

    # README 里必须说明的事
    readme = (root / "README.md")
    if readme.exists():
        text = readme.read_text(encoding="utf-8", errors="ignore")
        for kw, label in [("NetEase", "模型权重 License"),
                          ("Apache", "上游代码 License"),
                          ("R2T2", "外部 ASR 依赖")]:
            if kw not in text:
                warns.append("README 未说明 %s（找不到关键词 %r）" % (label, kw))

    # 还没 add 的新文件：它们不在暂存区里，但一提交就会进去，得一起扫
    pending = untracked_not_ignored(root)
    if pending:
        warns.append("有 %d 个文件会被提交但还没 git add（本次已一并扫描）：%s"
                     % (len(pending), ", ".join(pending[:6])))
        for rel_p in pending:
            path = root / rel_p
            if path.suffix.lower() in TEXT_EXT and path.exists() and path.stat().st_size < 5e6:
                content = path.read_text(encoding="utf-8", errors="ignore")
                for name, level, pattern, why, allow in RULES:
                    if any(a in rel_p for a in allow):
                        continue
                    for m in re.finditer(pattern, content):
                        line_no = content[:m.start()].count("\n") + 1
                        line = content.splitlines()[line_no - 1] if content else ""
                        if ALLOW_MARKER in line:
                            continue
                        entry = "%s:%d  %s → %r" % (rel_p, line_no, why, m.group(0)[:60])
                        (blockers if level == "BLOCKER" else warns).append(entry)

    # 行尾：仓库里存进 CRLF 的 .sh 是「一 clone 就坏」的隐患，必须在发布前拦住
    eol_blockers, eol_warns = line_ending_problems(root)
    blockers.extend(eol_blockers)
    warns.extend(eol_warns)

    # ---- 输出 ---- #
    if not args.quiet:
        print("扫描文件数：%d" % scanned)
        if big_files:
            print("\n较大的文件（确认是否该进仓库）：")
            for r, mb in sorted(big_files, key=lambda x: -x[1])[:12]:
                print("  %-52s %8.2f MB" % (r, mb))
        for line in infos:
            print("  [INFO] %s" % line)

    print("\n" + "=" * 76)
    if blockers:
        print("BLOCKER（%d 条）—— 不修完不能发布：" % len(blockers))
        for b in blockers:
            print("  ✗ %s" % b)
    if warns:
        print("\nWARN（%d 条）—— 逐条确认：" % len(warns))
        for w in warns:
            print("  ! %s" % w)
    if not blockers and not warns:
        print("干净：没有发现泄露或缺失。")
    print("=" * 76)
    print("结论：%s" % ("发现 BLOCKER，禁止发布" if blockers
                       else ("有 WARN，确认后可发布" if warns else "可以发布")))
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
