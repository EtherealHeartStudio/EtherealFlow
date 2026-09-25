# -*- coding: utf-8 -*-
"""CherryVoice · 发布前安全检查（需求文档 §11.3）。

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
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "logs",
             ".mypy_cache", ".pytest_cache", "node_modules"}
TEXT_EXT = {".py", ".md", ".json", ".sh", ".service", ".txt", ".yaml", ".yml",
            ".toml", ".cfg", ".ini", ".spec", ".bat", ".ps1", ".cmd", ""}
BIG_BINARY_EXT = {".gguf", ".safetensors", ".bin", ".pt", ".pth", ".onnx", ".mp4",
                  ".zip", ".7z", ".exe", ".dll", ".so", ".pyd", ".wav", ".png"}

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


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


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

    for path in sorted(iter_files(root)):
        r = rel(path, root)
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
    # 注意：判断的是"会不会被提交"，不是"本地存不存在" —— .venv 本来就该在本地，
    # 只要 .gitignore 盖住了就没事。这也是需求文档 §11.1 的原意。
    ignore_text = gi.read_text(encoding="utf-8", errors="ignore") if gi.exists() else ""
    for name in MUST_NOT_SHIP:
        p = root / name
        if not p.exists():
            continue
        covered = name in ignore_text or ("." + name) in ignore_text
        if covered:
            infos.append("%s 存在但已被 .gitignore 覆盖（不会提交）" % name)
            continue
        if name == ".venv":
            blockers.append("仓库里存在 %s，且 .gitignore 未覆盖" % name)
        elif name in ("config.json", "settings.json", ".env"):
            blockers.append("仓库里存在 %s（可能含 API Key），且 .gitignore 未覆盖" % name)
        else:
            warns.append("仓库里存在 %s（日志/临时目录），且 .gitignore 未覆盖" % name)

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
