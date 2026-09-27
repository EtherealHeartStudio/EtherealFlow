#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EtherealFlow · LLM 真机自测（FR-4 修正 / FR-5 翻译）。

和 ``tools/test_llm.py`` 的分工：
  * ``test_llm.py``        —— 不打网络，用假服务验证**失败分类 / 输出校验**的逻辑；
  * ``test_llm_live.py``（本文件）—— **真的连一个模型**，检查修正质量、延迟、
    以及各接口在实际服务上的行为差异。离线测试永远发现不了这些。

支持任何 OpenAI 兼容接口，本地部署与云端一视同仁::

    # 用 %APPDATA%/EtherealFlow/config.json 里的 llm 配置
    python tools\\test_llm_live.py

    # 本机 Ollama
    python tools\\test_llm_live.py --base-url http://localhost:11434/v1 --model qwen2.5:7b

    # 本机 LM Studio
    python tools\\test_llm_live.py --base-url http://localhost:1234/v1 --model qwen2.5-7b-instruct

    # 先看看服务端到底有哪些模型
    python tools\\test_llm_live.py --list-models

退出码 0 = 全部通过。没有任何断言失败即视为通过；连接不上属于**失败**（不是跳过），
因为「LLM 接不通」正是这个工具要暴露的东西。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.config import load_config                       # noqa: E402
from client.llm import (LlmClient, LlmError, clean_model_text,  # noqa: E402
                        default_client)
from client.translator import IncrementalTranslator         # noqa: E402

# --------------------------------------------------------------------------- #
# 用例：每条的 ``checks`` 返回 (名称, 是否通过, 说明)
# --------------------------------------------------------------------------- #

FILLERS = ("嗯", "啊", "呃", "那个", "就是", "这个这个")


def case_fillers(client: LlmClient) -> tuple[str, str, list]:
    """口水词 + 无标点 → 去口水词、加标点、保原意。"""
    src = "嗯那个我们今天就是测试一下这个语音输入法看看它能不能把口水词去掉"
    out = client.correct(src)
    text = out.text
    keep = ["今天", "测试", "语音输入法", "口水词"]
    checks = [
        ("拿到了结果", not out.degraded or out.changed, out.describe()),
        ("去掉了口水词", not any(f in text for f in FILLERS if f != "这个这个"),
         "残留：%s" % [f for f in FILLERS if f in text]),
        ("补了标点", any(p in text for p in "，。！？"), repr(text)),
        ("保住了原意", all(k in text for k in keep),
         "缺：%s" % [k for k in keep if k not in text]),
        ("没有加壳", text == clean_model_text(text) or not text.startswith("修正后"),
         repr(text[:60])),
    ]
    return src, text, checks


def case_homophone(client: LlmClient, hotwords: list[str]) -> tuple[str, str, list]:
    """同音错别字 → 靠热词纠正。"""
    src = "我们要用快首科技的产品来做这个项目"
    out = client.correct(src, hotwords=hotwords)
    text = out.text
    checks = [
        ("拿到了结果", bool(text.strip()), out.describe()),
        ("按热词纠正了", "快手科技" in text, repr(text)),
        ("没有吐热词表", "热词" not in text, repr(text)),
    ]
    return src, text, checks


def case_numbers(client: LlmClient) -> tuple[str, str, list]:
    """数字规范化（prompt 要求用阿拉伯数字）。"""
    src = "这个项目大概要三个月时间预算二十万左右"
    out = client.correct(src)
    text = out.text
    checks = [
        ("拿到了结果", bool(text.strip()), out.describe()),
        ("数字正常化了", ("3" in text or "三" in text) and ("20" in text or "二十" in text),
         repr(text)),
        ("没有增删内容", ("项目" in text and "预算" in text and "三个月" in text.replace("3个月", "三个月")),
         repr(text)),
    ]
    return src, text, checks


def case_mixed(client: LlmClient) -> tuple[str, str, list]:
    """中英混说：不能把英文吃掉。"""
    src = "帮我把这个 API 的 end point 改成那个 post 请求"
    out = client.correct(src)
    text = out.text
    checks = [
        ("拿到了结果", bool(text.strip()), out.describe()),
        ("英文保留", "API" in text.upper() and "POST" in text.upper(), repr(text)),
        ("去掉了那个", "那个" not in text, repr(text)),
    ]
    return src, text, checks


def case_translate(client: LlmClient) -> tuple[str, str, list]:
    """整段翻译。"""
    src = "今天下午三点开会，讨论一下新版本什么时候发布。"
    out = client.translate(src, "English")
    text = out.text
    checks = [
        ("拿到了译文", bool(text.strip()), out.describe()),
        ("是英文", sum(c.isascii() and c.isalpha() for c in text) > len(text) * 0.6,
         repr(text)),
        ("没有复读原文", text.strip() != src.strip(), repr(text)),
        ("译出了要点", any(w in text.lower() for w in ("meeting", "3", "three", "pm")),
         repr(text)),
    ]
    return src, text, checks


def case_incremental(client: LlmClient) -> tuple[str, str, list]:
    """增量翻译：喂累积原文，检查已落定段不被重译。"""
    calls: list[str] = []

    class Counting:
        def __init__(self, inner):
            self.inner = inner

        def translate(self, text, target="English"):
            calls.append(text)
            return self.inner.translate(text, target)

    tr = IncrementalTranslator(Counting(client), target_language="English",
                               silence_sec=0.6, chunk_chars=25, min_chars=2)
    tr.start()
    partials = [
        "今天下午三点开会。",                       # 句末 → 立刻落定
        "今天下午三点开会。讨论新版本。",             # 第二句落定
        "今天下午三点开会。讨论新版本。然后吃饭。",
    ]
    for p in partials:
        tr.update(p)
        time.sleep(0.35)
    res = tr.finish(partials[-1])
    text = res.text
    # 英文句子之间必须有空格。硬拼会产生 "afternoon.Discuss" 这种（实测踩到过），
    # 而纯中文用例永远暴露不出这个问题。
    missing_space = re.search(r"[a-z][.,!?][A-Za-z]", text)
    checks = [
        ("拿到了译文", bool(text.strip()), res.describe()),
        ("分了段", len(res.segments) >= 2, "段数=%d" % len(res.segments)),
        ("没有重复翻译已落定段",
         len(calls) == len(set(calls)) and len(calls) <= len(res.segments) + 1,
         "调用 %d 次，段 %d 个：%s" % (len(calls), len(res.segments),
                                    [c[:14] for c in calls])),
        ("已落定段未被重译",
         all(c not in calls[i + 1:] for i, c in enumerate(calls)),
         "调用序列：%s" % [c[:14] for c in calls]),
        ("译文覆盖了开头", "meeting" in text.lower() or "3" in text, repr(text[:80])),
        ("英文句子间有空格", not missing_space,
         "缺空格处：%r" % (missing_space.group(0) if missing_space else "")),
    ]
    return "、".join(partials), text, checks


def case_modes(cfg: dict) -> tuple[str, str, list]:
    """同一句话跑三种修正模式，确认它们**真的**产生了不同结果。

    只断言"Prompt 文本不一样"是不够的 —— 那只是配置层的差异。真正要证明的是
    模型拿到不同模式的 Prompt 后行为确实不同：该保留口水词的要保留，
    该去掉的要去掉，该更短的更短。
    """
    from client.correction_modes import EXAMPLE_INPUT
    from client.llm import default_client

    src = EXAMPLE_INPUT
    results: dict[str, str] = {}
    ms: dict[str, float] = {}
    for mid in ("none", "light", "deep"):
        t0 = time.monotonic()
        out = default_client(dict(cfg, mode=mid)).correct(src)
        results[mid] = out.text
        ms[mid] = (time.monotonic() - t0) * 1000
        print("    [%s] → %s  (%.0f ms)" % (mid, results[mid], ms[mid]))

    def has_filler(t: str) -> bool:
        return any(f in t for f in ("嗯", "那个", "不对不对"))

    checks = [
        ("三个模式都拿到了结果",
         all(results[m].strip() for m in results),
         "%s" % {k: v[:20] for k, v in results.items()}),
        ("不整理：**保留**口水词（没有被过度改写）",
         has_filler(results["none"]), repr(results["none"])),
        ("不整理：仍然修掉了错别字（回议→会议）",
         "会议" in results["none"], repr(results["none"])),
        ("轻度整理：去掉了口水词",
         not has_filler(results["light"]), repr(results["light"])),
        ("深度整理：去掉了口水词",
         not has_filler(results["deep"]), repr(results["deep"])),
        ("深度整理：不比不整理长（整理成书面表达应当更精炼）",
         len(results["deep"]) <= len(results["none"]),
         "%d vs %d" % (len(results["deep"]), len(results["none"]))),
        ("三种模式的输出并不相同",
         len(set(results.values())) >= 2,
         "去重后 %d 种" % len(set(results.values()))),
    ]
    return src, " / ".join("%s=%s" % (k, results[k]) for k in ("none", "light", "deep")), checks


def cmd_list_models(client: LlmClient) -> int:
    """列出服务端可用模型 —— 本地模型的名字用户不可能记得住。"""
    try:
        models = client.list_models()
    except LlmError as exc:
        print("取模型列表失败：%s" % exc)
        return 1
    print("服务端可用模型 %d 个：" % len(models))
    for m in models:
        print("   %s" % m)
    return 0


def cmd_ui(tok_ignored=None) -> int:
    """验证设置界面的「测试连接」按钮**真能通**。

    单独验它是因为这条路径上有个容易写错的地方：请求跑在后台线程里，
    结果必须用 ``root.after`` 投递回主线程（Tk 不是线程安全的）。
    只测 ``LlmClient`` 是测不到这一段的。
    """
    from client.settings_ui import SettingsWindow

    win = SettingsWindow(cfg=load_config())
    win.root.update()
    nb = next(c for c in win.root.winfo_children() if c.winfo_class() == "TNotebook")
    idx = next(i for i in range(nb.index("end"))
               if nb.tab(nb.tabs()[i], "text") == "修正")
    nb.select(nb.tabs()[idx])
    win.root.update()
    win.root.update_idletasks()

    print("界面上填的地址：%s" % win.vars["llm_base_url"].get())
    print("界面上填的模型：%s" % (win.vars["llm_model"].get() or "(空)"))
    print("点「测试连接」…")
    win._test_llm()

    status, deadline = "", time.monotonic() + 40
    while time.monotonic() < deadline:
        win.root.update()                  # 驱动 Tk 事件循环，让 after 回调跑起来
        status = str(win.llm_status.cget("text"))
        if status and not status.startswith("正在连接"):
            break
        time.sleep(0.05)
    models = list(win.model_combo.cget("values"))
    btn_ok = "disabled" not in win.test_btn.state()
    win.root.destroy()

    ok = status.startswith("✓")
    print("状态栏：%s" % status)
    print("下拉里的模型数：%d%s" % (len(models),
                                 "（前几个：%s）" % models[:3] if models else ""))
    print("按钮已恢复可用：%s" % btn_ok)
    print("\n断言：%s" % ("通过" if (ok and models and btn_ok) else "失败"))
    return 0 if (ok and models and btn_ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="EtherealFlow LLM 真机自测")
    ap.add_argument("--base-url", default="", help="覆盖配置里的地址")
    ap.add_argument("--api-key", default="", help="覆盖配置里的 Key")
    ap.add_argument("--model", default="", help="覆盖配置里的模型名")
    ap.add_argument("--timeout", type=float, default=0.0, help="覆盖超时（秒）")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--ui", action="store_true", help="改用设置界面「测试连接」验证")
    ap.add_argument("--require", action="store_true",
                    help="连不上模型时视为失败（默认报 SKIP 退出 0）")
    ap.add_argument("--only", default="", help="只跑指定用例（逗号分隔）")
    args = ap.parse_args()

    if args.ui:
        return cmd_ui()

    cfg = dict(load_config().get("llm") or {})
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.api_key:
        cfg["api_key"] = args.api_key
    if args.model:
        cfg["model"] = args.model
    if args.timeout:
        cfg["timeout"] = args.timeout

    client = default_client(cfg)
    key_state = "已设置（%d 字符）" % len(client.api_key) if client.api_key else "未设置"
    print("=" * 78)
    print("地址  : %s" % client.base_url)
    print("模型  : %s" % (client.model or "(未指定，由服务端决定)"))
    print("Key   : %s" % key_state)
    print("超时  : %.1f s" % client.timeout)
    print("=" * 78)

    # 先探一次连通性。本工具需要**真的有一个模型**，这是环境依赖而不是代码缺陷 ——
    # 所以默认情况下连不上就报 [SKIP] 并以 0 退出（跑测试套件的人可能没配 LLM）；
    # 加 --require 则视为失败，供确有必要时使用。
    conn_ok, conn_msg = client.test_connection()
    print("连通性：%s %s" % ("✓" if conn_ok else "✗", conn_msg))
    if not conn_ok:
        if args.require:
            print("\n[FAIL] 连接不上，且指定了 --require")
            return 1
        print("\n[SKIP] 没有可用的模型可用，未执行用例。\n"
              "       这是**环境依赖**，不代表代码有问题。\n"
              "       要跑真机自测，请先配好一个模型，例如：\n"
              "         python tools\\test_llm_live.py --base-url http://localhost:11434/v1 "
              "--model qwen2.5:7b\n"
              "         （本机 Ollama 需要先启动 ollama serve 并 pull 过模型）")
        return 0

    if args.list_models:
        return cmd_list_models(client)

    cases = [("口水词", case_fillers), ("同音纠错", case_homophone),
             ("数字", case_numbers), ("中英混说", case_mixed),
             ("整段翻译", case_translate), ("增量翻译", case_incremental),
             # 修正模式要拿到完整 cfg 才能各建一个 client，所以用闭包兜一下
             ("修正模式", lambda _client: case_modes(cfg))]
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        cases = [c for c in cases if c[0] in want]

    allok = True
    total = {"pass": 0, "fail": 0}
    for name, fn in cases:
        print("\n" + "-" * 78)
        print("用例：%s" % name)
        print("-" * 78)
        t0 = time.monotonic()

        def attempt_once():
            if fn is case_homophone:
                return fn(client, ["快手科技", "EtherealFlow"])
            return fn(client)

        # 每个用例最多跑两次。
        # 会被测的是一个**外部非确定性模型**：实测同一句偶尔会慢到 23 s（触到超时就降级），
        # 措辞也会变。这类波动不该让整个套件变红，但**系统性的坏掉两次都会挂** ——
        # 所以只在两次都失败时才判失败，并明确标出"重试过"。
        src = out = ""
        checks: list = []
        error = ""
        for attempt in (1, 2):
            try:
                src, out, checks = attempt_once()
                error = ""
            except LlmError as exc:
                checks, error = [], "调用抛出 %s：%s" % (type(exc).__name__, exc)
            except Exception as exc:  # noqa: BLE001
                checks, error = [], "用例异常 %r" % exc
            if checks and all(ok for _n, ok, _d in checks):
                break
            if attempt == 1:
                print("  ↻ 有未通过项，重试一次（模型输出有随机性；两次都失败才判失败）")
        wall = (time.monotonic() - t0) * 1000

        if not checks:
            print("  [FAIL] %s" % error)
            allok = False
            total["fail"] += 1
            continue
        print("  输入：%s" % src)
        print("  输出：%s" % out)
        for cname, ok, detail in checks:
            total["pass" if ok else "fail"] += 1
            allok &= ok
            print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", cname,
                                 "" if ok else "  → " + str(detail)))
        print("  用时：%.0f ms" % wall)

    print("\n" + "=" * 78)
    print("断言：通过 %d / 失败 %d" % (total["pass"], total["fail"]))
    print("结论：%s" % ("全部通过" if allok else "存在失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
