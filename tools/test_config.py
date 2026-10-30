# -*- coding: utf-8 -*-
"""EtherealFlow · 配置的**深拷贝**与**类型校验**离线自测。

这个用例盯住两个已实测确认的缺陷：

* **BF-5【中】``deep_merge`` 只做浅拷贝** —— 用户配置里**缺失**的那一段
  直接引用 ``DEFAULT_CONFIG`` 的同一个对象。改一句
  ``cfg["audio"]["device"] = 3`` 就把**模块级默认值**也改了；
  后面 ``save_config`` 拿被污染的基线做 diff，会把这次改动当成
  「跟默认值一样」而丢掉 —— 用户的设置说没就没。
  已实测：``merged["audio"] is DEFAULT_CONFIG["audio"]`` → ``True``。

* **BF-6【中】配置没有类型校验** —— 合法 JSON 但**类型错**的配置会一路传到
  使用处炸掉：``cfg["overlay"]["width"] = "abc"`` 在 ``overlay.py`` 里抛
  ``TypeError``；``cfg["hotkey"]["keys"] = "ctrl"`` 在 ``hotkey.py`` 的
  ``normalize_keys`` 里抛 ``ValueError``。两处都在**启动路径**上，
  所以「配置坏了」等于「程序起不来」。而且损坏的配置会被**静默丢弃、
  用默认值覆盖** → 用户的 API Key 和热词全没了。

纯逻辑：**不碰 Tk、不碰网络**，能离线跑（CI 上要跑）。
退出码 0 = 全部通过。
"""

from __future__ import annotations

# 强制 stdout 用 UTF-8：Windows 控制台/管道默认 cp1252，
# 打印中文会 UnicodeEncodeError（曾导致 CI 全部失败，见 tools/_utf8.py）。
import _utf8  # noqa: F401

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

from client.config import (DEFAULT_CONFIG, deep_merge, load_config,  # noqa: E402
                           load_config_with_warnings, save_config,
                           validate_config)

TMP = Path(tempfile.gettempdir()) / "cv-config-test"


def fresh_copy() -> dict:
    """一份全新的默认值副本（用来确认全局默认值没被污染）。"""
    return json.loads(json.dumps(DEFAULT_CONFIG))


# --------------------------------------------------------------------------- #
# 畸形取值表：每个字段一个「坏值」
#
# 故意**不**从实现里 import 规则表 —— 那样测试就只是在复述实现。
# 这里手写一份「规格」，实现必须跟它对得上。
# --------------------------------------------------------------------------- #
BAD_VALUES: list[tuple[str, str, object]] = [
    # (段, 字段, 坏值)
    ("hotkey", "keys", "ctrl"),                 # 字符串，不是列表
    ("hotkey", "keys", [1, 2]),                 # 列表里不是字符串
    ("hotkey", "keys", ["ctrl", "不存在的键名"]),  # 键名不认识（normalize_keys 会抛）
    ("hotkey", "backend", 123),                 # 不是字符串
    ("hotkey", "backend", "不存在的后端"),       # 字符串，但后端不认识
    ("hotkey", "swallow", "true"),              # 字符串，不是布尔
    ("hotkey", "swallow", 1),                   # 数字，不是布尔（bool 是 int 的子类）
    ("asr", "url", 5),
    ("asr", "language", 5),
    ("asr", "context", ["a"]),
    ("asr", "max_utterance_sec", "30"),
    ("asr", "max_utterance_sec", 0),
    ("asr", "connect_hint", "yes"),
    ("audio", "device", "0"),                   # 字符串，不是整数
    ("audio", "device", True),                  # 布尔不是整数
    ("audio", "chunk_ms", "160"),
    ("audio", "chunk_ms", -1),
    ("audio", "gain", "1.0"),
    ("overlay", "width", "780"),
    ("overlay", "width", 0),
    ("overlay", "opacity", "0.94"),
    ("overlay", "opacity", 1.5),
    ("overlay", "font_size", "15"),
    ("overlay", "translate", 1),
    ("llm", "enabled", "true"),
    ("llm", "base_url", 5),
    ("llm", "api_key", 5),
    ("llm", "model", ["m"]),
    ("llm", "mode", 5),
    ("llm", "strip_trailing_period", "no"),
    ("llm", "modes", "not-a-list"),
    ("llm", "timeout", "15"),
    ("llm", "timeout", 0),
    ("llm", "max_tokens", "4096"),
    ("llm", "max_tokens", -1),
    ("llm", "prompt", 5),
    ("llm", "translate_prompt", 5),
    ("inject", "mode", 5),
    ("inject", "restore_clipboard", "yes"),
    ("inject", "preserve_nontext_clipboard", 1),
    ("inject", "restore_delay_ms", "500"),
    ("inject", "restore_delay_ms", 0),
    ("inject", "typing_max_chars", "500"),
    ("translate", "enabled", "true"),
    ("translate", "target_language", 5),
    ("translate", "silence_sec", "1.5"),
    ("translate", "chunk_chars", 0),
    ("translate", "min_chars", "2"),
    ("history", "max_items", "20"),
    ("history", "max_items", 0),
]


def main() -> int:
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    # ===================================================================== #
    # BF-5：deep_merge 必须是真深拷贝
    # ===================================================================== #
    snapshot = fresh_copy()

    merged = deep_merge(DEFAULT_CONFIG, {"audio": {"gain": 2.0}})
    check("BF-5：合并结果的段不是 DEFAULT_CONFIG 的同一个对象",
          merged["audio"] is not DEFAULT_CONFIG["audio"],
          "merged[audio] is DEFAULT_CONFIG[audio] → %s"
          % (merged["audio"] is DEFAULT_CONFIG["audio"]))
    check("BF-5：合并结果的列表也不是同一个对象",
          merged["hotkey"]["keys"] is not DEFAULT_CONFIG["hotkey"]["keys"],
          "keys is → %s" % (merged["hotkey"]["keys"] is DEFAULT_CONFIG["hotkey"]["keys"]))

    merged["audio"]["device"] = 3
    merged["hotkey"]["keys"].append("alt")
    check("BF-5：改合并结果不影响 DEFAULT_CONFIG",
          DEFAULT_CONFIG["audio"]["device"] == snapshot["audio"]["device"]
          and DEFAULT_CONFIG["hotkey"]["keys"] == snapshot["hotkey"]["keys"],
          "device=%r keys=%r"
          % (DEFAULT_CONFIG["audio"]["device"], DEFAULT_CONFIG["hotkey"]["keys"]))

    # 缺省的那一段也一样：用户配置没写 audio 时，合并结果也必须是副本
    merged2 = deep_merge(DEFAULT_CONFIG, {"llm": {"model": "x"}})
    merged2["audio"]["chunk_ms"] = 999
    check("BF-5：用户配置里缺省的段，改它也不影响 DEFAULT_CONFIG",
          DEFAULT_CONFIG["audio"]["chunk_ms"] == snapshot["audio"]["chunk_ms"],
          "chunk_ms=%r" % DEFAULT_CONFIG["audio"]["chunk_ms"])

    # 深拷贝的另一半：合并结果也不能跟「传入的 override」共享引用
    override = {"audio": {"gain": 3.0}}
    merged3 = deep_merge(DEFAULT_CONFIG, override)
    merged3["audio"]["gain"] = 7.0
    check("BF-5：合并结果跟 override 也不共享引用",
          override["audio"]["gain"] == 3.0, "override gain=%r" % override["audio"]["gain"])

    # ===================================================================== #
    # BF-5：deep_merge 的语义不能变
    # ===================================================================== #
    base = {"a": {"x": 1, "y": 2}, "b": 3, "c": [1, 2]}
    check("语义：override 覆盖 base",
          deep_merge(base, {"b": 9})["b"] == 9, "")
    check("语义：dict 递归合并（base 独有的键保留）",
          deep_merge(base, {"a": {"x": 9}})["a"] == {"x": 9, "y": 2},
          repr(deep_merge(base, {"a": {"x": 9}})["a"]))
    check("语义：非 dict 直接覆盖（不合并）",
          deep_merge(base, {"a": 5})["a"] == 5, repr(deep_merge(base, {"a": 5})["a"]))
    check("语义：override 里没有的键不受影响",
          deep_merge(base, {})["b"] == 3, "")
    check("语义：override 为空 / None 不炸",
          deep_merge(base, None)["b"] == 3, "")
    check("语义：列表整体覆盖，不做逐元素合并",
          deep_merge(base, {"c": [9]})["c"] == [9], repr(deep_merge(base, {"c": [9]})["c"]))
    check("语义：入参不被改动",
          base == {"a": {"x": 1, "y": 2}, "b": 3, "c": [1, 2]}, repr(base))

    # ===================================================================== #
    # BF-6：类型校验 —— 逐字段回退，不崩
    # ===================================================================== #
    bad_failures: list[str] = []
    for section, field, bad in BAD_VALUES:
        raw = {section: {field: bad}}
        try:
            cfg, notes = validate_config(raw)
        except Exception as exc:  # noqa: BLE001 - 这里就是要抓「崩了」
            bad_failures.append("%s.%s=%r → 抛异常 %r" % (section, field, bad, exc))
            continue
        want = DEFAULT_CONFIG[section][field]
        got = cfg[section][field]
        if got != want:
            bad_failures.append("%s.%s=%r → 落到了 %r（应为 %r）"
                                % (section, field, bad, got, want))
        elif not notes:
            bad_failures.append("%s.%s=%r → 值回退了但没有回退说明"
                                % (section, field, bad))
    check("BF-6：每个字段的坏值都安全回退默认值且不抛异常",
          not bad_failures, "；".join(bad_failures[:6]) or "全部 %d 项通过"
          % len(BAD_VALUES))

    # 坏值不能**连累**别的字段
    cfg, notes = validate_config({"audio": {"device": "x"}, "overlay": {"width": 900}})
    check("BF-6：坏字段回退时，别的字段不受影响",
          cfg["overlay"]["width"] == 900 and cfg["audio"]["device"] is None,
          "width=%r device=%r" % (cfg["overlay"]["width"], cfg["audio"]["device"]))

    # 整段不是对象 → 整段回退
    cfg, notes = validate_config({"overlay": "不是一个对象"})
    check("BF-6：整段类型错时整段回退",
          cfg["overlay"] == DEFAULT_CONFIG["overlay"] and notes,
          "notes=%s" % notes)

    # 顶层字段（hotwords）也要校验
    cfg, notes = validate_config({"hotwords": "不是列表"})
    check("BF-6：顶层字段 hotwords 也会被校验",
          cfg["hotwords"] == DEFAULT_CONFIG["hotwords"] and notes,
          "hotwords=%r notes=%s" % (cfg["hotwords"], notes))

    # bool 不能当数字混过去，数字也不能当 bool
    cfg, _ = validate_config({"audio": {"gain": True}})
    check("BF-6：布尔值不算数字（gain=True 退回默认）",
          cfg["audio"]["gain"] == DEFAULT_CONFIG["audio"]["gain"],
          "gain=%r" % cfg["audio"]["gain"])
    cfg, _ = validate_config({"overlay": {"translate": 1}})
    check("BF-6：数字不算布尔（translate=1 退回默认）",
          cfg["overlay"]["translate"] == DEFAULT_CONFIG["overlay"]["translate"],
          "translate=%r" % cfg["overlay"]["translate"])

    # ===================================================================== #
    # BF-6：完全合法的配置 → 一个字都不改
    # ===================================================================== #
    good = {
        "hotkey": {"keys": ["ctrl", "alt"], "backend": "hook", "swallow": False},
        "asr": {"url": "ws://127.0.0.1:19000", "language": "Chinese",
                "context": "上下文", "max_utterance_sec": 45, "connect_hint": False},
        "audio": {"device": 2, "device_name": "USB Mic",
                  "chunk_ms": 320, "gain": 1.5},
        "overlay": {"width": 640, "opacity": 0.8, "font_size": 18, "translate": True},
        "llm": {"enabled": False, "base_url": "http://127.0.0.1:8318/v1",
                "api_key": "sk-test-config-roundtrip", "model": "qwen",  # release-check: allow
                "mode": "deep", "strip_trailing_period": True,
                "modes": [{"id": "x", "name": "n", "desc": "d",
                            "prompt": "p", "example": "e"}],
                "timeout": 22.5, "max_tokens": 8192,
                "prompt": "自定义 {text}", "translate_prompt": ""},
        "inject": {"mode": "typing", "restore_clipboard": False,
                   "preserve_nontext_clipboard": False, "restore_delay_ms": 250,
                   "typing_max_chars": 300},
        "translate": {"enabled": True, "target_language": "English",
                      "silence_sec": 2.5, "chunk_chars": 55, "min_chars": 3},
        "hotwords": ["EtherealFlow", "Confucius4"],
        "history": {"max_items": 50},
    }
    cfg, notes = validate_config(good)
    diffs = {k: (v, cfg.get(k)) for k, v in good.items() if cfg.get(k) != v}
    check("BF-6：完全合法的配置一字不改",
          not diffs and not notes, "差异=%s notes=%s" % (diffs, notes))

    # 合法但**不完整**的配置：缺的字段补默认值，给的字段原样保留
    cfg, notes = validate_config({"overlay": {"width": 700}})
    check("BF-6：部分字段缺失时补默认值、给的字段保留",
          cfg["overlay"]["width"] == 700
          and cfg["overlay"]["opacity"] == DEFAULT_CONFIG["overlay"]["opacity"]
          and not notes,
          "width=%r opacity=%r notes=%s"
          % (cfg["overlay"]["width"], cfg["overlay"]["opacity"], notes))

    # ===================================================================== #
    # BF-6：不是 dict 的 JSON → 安全回退
    # ===================================================================== #
    for raw in ([1, 2, 3], "abc", 42, None, True):
        try:
            cfg, notes = validate_config(raw)
            ok = cfg == DEFAULT_CONFIG and notes
        except Exception as exc:  # noqa: BLE001
            ok = False
            notes = "抛异常 %r" % exc
        check("BF-6：根节点不是对象时安全回退（%r）" % (raw,), bool(ok),
              "cfg==DEFAULT_CONFIG → %s notes=%s" % (cfg == DEFAULT_CONFIG, notes))

    # ===================================================================== #
    # 未知键要保留（老行为，别破坏）
    # ===================================================================== #
    cfg, notes = validate_config({"我自己的段": {"x": 1}, "hotkey": {"backend": "hook",
                                                                     "未知字段": 3}})
    check("未知键：顶层的未知段保留",
          cfg.get("我自己的段") == {"x": 1}, repr(cfg.get("我自己的段")))
    check("未知键：段内的未知字段保留",
          cfg["hotkey"].get("未知字段") == 3, repr(cfg["hotkey"].get("未知字段")))
    check("未知键：不会因此被判成非法",
          not notes, "notes=%s" % notes)

    # ===================================================================== #
    # 落盘往返：坏文件 → 备份 .bak + 回退默认值 + 不崩
    # ===================================================================== #
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    broken = TMP / "broken.json"
    broken.write_text("{ 这不是合法 JSON", encoding="utf-8")
    cfg = load_config(broken)
    bak = broken.with_name(broken.name + ".bak")
    check("BF-6：坏文件不崩，回退默认值",
          cfg == DEFAULT_CONFIG, "cfg==DEFAULT_CONFIG → %s" % (cfg == DEFAULT_CONFIG))
    check("BF-6：坏文件被备份成 .bak",
          bak.exists() and bak.read_text(encoding="utf-8") == "{ 这不是合法 JSON",
          "bak 存在=%s" % bak.exists())
    check("BF-6：备份不覆盖原文件",
          broken.read_text(encoding="utf-8") == "{ 这不是合法 JSON", "")

    # 类型错的文件：也要备份（里面可能有用户的 API Key）
    typed_bad = TMP / "typed_bad.json"
    payload = json.dumps({"llm": {"api_key": "sk-test-keep-me", "timeout": "oops"},  # release-check: allow
                          "hotwords": ["保留我"]}, ensure_ascii=False)
    typed_bad.write_text(payload, encoding="utf-8")
    warnings: list[str] = []
    cfg = load_config(typed_bad, warnings=warnings)
    bak2 = typed_bad.with_name(typed_bad.name + ".bak")
    check("BF-6：类型错的文件也备份成 .bak",
          bak2.exists() and bak2.read_text(encoding="utf-8") == payload,
          "bak 存在=%s" % bak2.exists())
    check("BF-6：类型错的文件：坏字段回退、好字段保留",
          cfg["llm"]["timeout"] == DEFAULT_CONFIG["llm"]["timeout"]
          and cfg["llm"]["api_key"] == "sk-test-keep-me"
          and cfg["hotwords"] == ["保留我"],
          "timeout=%r api_key=%r hotwords=%r"
          % (cfg["llm"]["timeout"], cfg["llm"]["api_key"], cfg["hotwords"]))
    check("BF-6：回退的字段有说明（能提示用户）",
          len(warnings) == 1 and "llm.timeout" in warnings[0],
          "warnings=%s" % warnings)

    # 合法文件：不备份、不回退
    fine = TMP / "fine.json"
    fine.write_text(json.dumps({"overlay": {"width": 700}}, ensure_ascii=False),
                    encoding="utf-8")
    warnings2: list[str] = []
    cfg = load_config(fine, warnings=warnings2)
    check("BF-6：合法文件不回退、不备份、不报错",
          cfg["overlay"]["width"] == 700 and not warnings2
          and not fine.with_name(fine.name + ".bak").exists(),
          "width=%r warnings=%s" % (cfg["overlay"]["width"], warnings2))

    # 文件不存在 → 默认值
    cfg = load_config(TMP / "根本没有这个文件.json")
    check("BF-6：文件不存在时回退默认值",
          cfg == DEFAULT_CONFIG, "cfg==DEFAULT_CONFIG → %s" % (cfg == DEFAULT_CONFIG))

    # load_config_with_warnings 也能拿到回退说明
    _, notes3 = load_config_with_warnings(typed_bad)
    check("BF-6：load_config_with_warnings 返回回退说明",
          len(notes3) == 1 and "llm.timeout" in notes3[0], "notes=%s" % notes3)

    # ===================================================================== #
    # BF-5 端到端：改完配置存盘，重启后改动还在
    #
    # 这是 BF-5 的**真实后果**：以前 deep_merge 返回浅拷贝，
    # load_config 拿到的 cfg["audio"] 就是 DEFAULT_CONFIG["audio"]，
    # 于是用户改一句 cfg["audio"]["device"] = 7 就把**全局默认值**也改了；
    # 紧接着 save_config 拿被污染的默认值做 diff，
    # 「device == 默认值」→ 这次改动被当成没改过而**丢掉**，
    # 根本没写进文件。用户设了麦克风，保存，重启 —— 没了。
    #
    # ⚠️ 断言必须落在**文件内容**和**另一个进程**上：
    # 在同一进程里 reload 的话，被污染的全局默认值会把值「补」回来，
    # 看起来一切正常 —— 那是假象。
    # ===================================================================== #
    rt = TMP / "roundtrip.json"
    if rt.exists():
        rt.unlink()
    rt.write_text(json.dumps({"overlay": {"width": 700}}, ensure_ascii=False),
                  encoding="utf-8")
    cfg_rt = load_config(rt)
    cfg_rt["audio"]["device"] = 7            # 改一个「缺省段」的字段
    save_config(cfg_rt, rt)
    saved_text = rt.read_text(encoding="utf-8")
    check("BF-5 端到端：改动真的写进了配置文件",
          '"device": 7' in saved_text.replace('"device":7', '"device": 7'),
          "文件内容=%s" % saved_text.replace("\n", " ")[:160])
    check("BF-5 端到端：存盘重载后其它字段没被默认值带偏",
          load_config(rt)["overlay"]["width"] == 700, "")

    # 换一个**干净的进程**重新加载 —— 模拟「重启客户端」
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);"
         "from client.config import load_config;"
         "print(load_config(%r)['audio']['device'])" % (str(ROOT), str(rt))],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    fresh = probe.stdout.strip().splitlines()[-1] if probe.stdout.strip() else ""
    check("BF-5 端到端：换进程重载（= 重启客户端）后改动还在",
          fresh == "7",
          "新进程读到 device=%r（stderr=%s）"
          % (fresh, (probe.stderr or "").strip()[-200:]))

    # ===================================================================== #
    # 全程跑完，DEFAULT_CONFIG 必须还是原样
    # ===================================================================== #
    check("BF-5：跑完全部用例后 DEFAULT_CONFIG 没被污染",
          DEFAULT_CONFIG == snapshot,
          "差异=%s" % {k: (snapshot.get(k), DEFAULT_CONFIG.get(k))
                       for k in set(snapshot) | set(DEFAULT_CONFIG)
                       if snapshot.get(k) != DEFAULT_CONFIG.get(k)})

    print("=" * 74)
    print("S4 验证：配置深拷贝（BF-5）与类型校验（BF-6）")
    print("=" * 74)
    for name, ok, detail in rows:
        print("%s %s" % ("[PASS]" if ok else "[FAIL]", name))
        if detail:
            print("        %s" % detail)
    fails = [n for n, ok, _d in rows if not ok]
    print("-" * 74)
    print("结论：%s（%d/%d）"
          % ("全部通过" if not fails else "失败项：%s" % fails,
             sum(1 for _n, ok, _d in rows if ok), len(rows)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
