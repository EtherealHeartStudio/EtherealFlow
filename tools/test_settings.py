# -*- coding: utf-8 -*-
"""EtherealFlow · 设置界面的配置往返测试（FR-7）。

不需要人点界面：构建窗口 → 填一套配置 → ``collect()`` 读回 → 存盘 → 重新加载，
断言"存进去的和读出来的一致"。顺手把界面截个图，供人眼检查。

退出码 0 = 通过。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.config import (DEFAULT_CONFIG, DEFAULT_LLM_TIMEOUT,  # noqa: E402
                           load_config, mask_secret, save_config)
from client.settings_ui import SettingsWindow                                    # noqa: E402

SHOT = Path(tempfile.gettempdir()) / "cv-settings.png"


def main() -> int:
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, ok, detail))

    tmp = Path(tempfile.gettempdir()) / "cv-settings-test.json"
    if tmp.exists():
        tmp.unlink()

    # 一份"什么都改过"的配置
    want = load_config(None)
    want["hotkey"]["keys"] = ["ctrl", "alt", "space"]
    want["hotkey"]["backend"] = "hook"
    want["asr"]["url"] = "ws://127.0.0.1:19999"
    want["asr"]["language"] = "English"
    want["asr"]["context"] = "测试上下文"
    want["llm"]["enabled"] = False
    want["llm"]["base_url"] = "http://127.0.0.1:9999/v1"
    want["llm"]["api_key"] = "sk-test-should-roundtrip-1234"   # release-check: allow
    want["llm"]["model"] = "some-model"
    want["llm"]["timeout"] = 12.5
    want["llm"]["prompt"] = "自定义修正模板：{text}"
    want["translate"]["enabled"] = True
    want["translate"]["target_language"] = "Japanese"
    want["translate"]["silence_sec"] = 2.5
    want["translate"]["chunk_chars"] = 55
    want["inject"]["mode"] = "typing"
    want["inject"]["restore_clipboard"] = False
    want["inject"]["preserve_nontext_clipboard"] = False
    want["hotwords"] = ["EtherealFlow", "Confucius4", "网易有道"]

    win = SettingsWindow(load_config(None), tmp)
    win.apply(want)
    got = win.collect()

    for section in ("hotkey", "asr", "llm", "translate", "inject"):
        diffs = {k: (want[section][k], got[section].get(k))
                 for k in want[section] if want[section][k] != got[section].get(k)}
        check("界面往返一致：%s" % section, not diffs, "差异=%s" % diffs if diffs else "OK")
    check("界面往返一致：hotwords",
          want["hotwords"] == got["hotwords"],
          "%r vs %r" % (want["hotwords"], got["hotwords"]))

    # 存盘 → 重新加载
    saved = save_config(got, tmp)
    reloaded = load_config(saved)
    check("存盘后重新加载：改动都保住了", reloaded == got,
          "不一致处=%s" % {k: (got.get(k), reloaded.get(k))
                        for k in got if got.get(k) != reloaded.get(k)})
    check("只写差异：默认值没被固化进文件",
          "swallow" not in saved.read_text(encoding="utf-8"),
          "文件大小 %d 字节" % saved.stat().st_size)
    check("API Key 能原样往返（本地配置，不进仓库）",
          reloaded["llm"]["api_key"] == "sk-test-should-roundtrip-1234",   # release-check: allow
          mask_secret(reloaded["llm"]["api_key"]))

    # 坏配置不能把程序搞崩
    broken = Path(tempfile.gettempdir()) / "cv-broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    fallback = load_config(broken)
    check("配置损坏时回退默认值（不崩）", fallback == DEFAULT_CONFIG, "已回退")

    # 数字填错也不能崩
    win.apply(want)
    win.vars["llm_timeout"].set("不是数字")
    win.vars["translate_chunk"].set("")
    weird = win.collect()
    check("数字项填错时回退默认值而不是抛异常",
          weird["llm"]["timeout"] == DEFAULT_LLM_TIMEOUT
          and weird["translate"]["chunk_chars"] == 40,
          "timeout=%r chunk=%r" % (weird["llm"]["timeout"], weird["translate"]["chunk_chars"]))

    # 键位解析
    win.apply(want)
    win.vars["hotkey_keys"].set("Ctrl + Alt + Space")
    check("快捷键字符串能解析（大小写/空格/中文加号都认）",
          win.collect()["hotkey"]["keys"] == ["ctrl", "alt", "space"],
          "%r" % win.collect()["hotkey"]["keys"])
    win.vars["hotkey_keys"].set("")
    check("快捷键填空时回退默认 ctrl+win",
          win.collect()["hotkey"]["keys"] == ["ctrl", "win"],
          "%r" % win.collect()["hotkey"]["keys"])

    # ---------- 修正模式（卡片选择 + 自定义） ---------- #
    from client.correction_modes import new_mode, upsert_mode

    def cards() -> list:
        return list(win.cards_frame.winfo_children())

    check("修正页默认画出三张内置模式卡片", len(cards()) == 3, "%d 张" % len(cards()))
    win._select_mode("deep")
    check("点卡片能切换当前模式", win.collect()["llm"]["mode"] == "deep",
          win.collect()["llm"]["mode"])
    win.vars["llm_strip_period"].set(True)
    check("「去除结尾句号」能写进配置",
          win.collect()["llm"]["strip_trailing_period"] is True, "")

    tmp_modes = {"modes": win._modes}
    mine = new_mode(tmp_modes, "测试模式", "一句话说明", "把 {text} 整理好", "示例")
    upsert_mode(tmp_modes, mine)
    win._modes = tmp_modes["modes"]
    win._rebuild_cards()
    check("新增自定义模式后多出一张卡片", len(cards()) == 4, "%d 张" % len(cards()))
    win._select_mode(mine["id"])
    got_modes = win.collect()
    check("自定义模式写进配置且被选中",
          got_modes["llm"]["mode"] == mine["id"]
          and [x["id"] for x in got_modes["llm"]["modes"]] == [mine["id"]],
          "mode=%s modes=%s" % (got_modes["llm"]["mode"], got_modes["llm"]["modes"]))
    check("内置模式不会被写进配置文件（它们在代码里）",
          all(x["id"] not in ("none", "light", "deep")
              for x in got_modes["llm"]["modes"]),
          "%s" % [x["id"] for x in got_modes["llm"]["modes"]])

    tmp2 = Path(tempfile.gettempdir()) / "cv-settings-modes.json"
    save_config(got_modes, tmp2)
    back = load_config(tmp2)
    check("存盘重载后模式与自定义项都还在",
          back["llm"]["mode"] == mine["id"] and len(back["llm"]["modes"]) == 1,
          "mode=%s 自定义=%d 条" % (back["llm"]["mode"], len(back["llm"]["modes"])))
    # 配置里带的**内部标记**不该被写进文件
    check("写进文件的自定义模式不含内部字段",
          "custom" not in back["llm"]["modes"][0],
          "%s" % sorted(back["llm"]["modes"][0]))

    # 模式 id 失效（用户手改了配置）时界面要能兜住
    broken2 = load_config(tmp2)
    broken2["llm"]["mode"] = "早就删掉的模式"
    win.apply(broken2)
    check("配置里的模式 id 失效时界面不崩且回退默认",
          win.vars["llm_mode"].get() == "light" and len(cards()) == 4,
          "当前=%s 卡片=%d" % (win.vars["llm_mode"].get(), len(cards())))

    # ---------- 布局回归 ---------- #
    # 断言输入控件**真正长在行 Frame 里**。这条守的是两个都真实发生过的 bug：
    #
    #   a) ``_row()`` 里忘了指定容器，控件被装到页签上 —— 标题一列、控件另起一列；
    #   b) 用 ``pack(in_=line)`` 补救：几何位置对了，但控件仍以页签为父容器，
    #      且它**先于行 Frame 创建**，Z 序上被后建的 Frame 盖住 ——
    #      位置全对、``winfo_ismapped()`` 也是 1，画面上却什么都看不见。
    #
    # 所以判据必须是**父子层级**（``ch.master``），不能只看几何归属：
    # 只看几何的话 (b) 会被判为通过，而它恰恰是最难发现的那个。
    nb = next(c for c in win.root.winfo_children() if c.winfo_class() == "TNotebook")
    stray = []
    for i in range(nb.index("end")):
        tid = nb.tabs()[i]
        nb.select(tid)                     # 不选中页签，Tk 不做布局
        win.root.update()
        win.root.update_idletasks()
        page = win.root.nametowidget(tid)
        title = nb.tab(tid, "text")
        for ch in page.winfo_children():
            if ch.winfo_class() not in ("TEntry", "TCombobox"):
                continue
            if str(ch.master) == str(page):
                stray.append("%s/%s" % (title, ch.winfo_class()))
    check("布局：输入控件必须真正长在行 Frame 里（父容器不能是页签）",
          not stray, "越位控件：%s" % stray)

    nb.select(nb.tabs()[0])
    win.root.update()

    # 截图（人眼确认）
    win.apply(got)
    win.root.update()
    win.root.after(120, lambda: (win.root.update(), _shoot(win)))
    win.root.after(900, win.root.destroy)
    try:
        win.root.mainloop()
    except Exception:  # noqa: BLE001
        pass
    check("设置界面截图已保存（人工可看）", SHOT.exists(), str(SHOT))

    print("=" * 74)
    print("P5 验证：设置界面（FR-7 六个分组）")
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


def _shoot(win) -> None:
    """只截设置窗口自己那块矩形。"""
    import subprocess
    win.root.update_idletasks()
    x, y = win.root.winfo_rootx(), win.root.winfo_rooty()
    w, h = win.root.winfo_width(), win.root.winfo_height()
    ps = (
        'Add-Type -AssemblyName System.Windows.Forms,System.Drawing;'
        'Add-Type -MemberDefinition \'[DllImport("user32.dll")] public static extern bool '
        'SetProcessDPIAware();\' -Name U -Namespace W;'
        '[W.U]::SetProcessDPIAware() | Out-Null;'
        '$b = New-Object System.Drawing.Bitmap %d, %d;'
        '$g = [System.Drawing.Graphics]::FromImage($b);'
        '$g.CopyFromScreen(%d, %d, 0, 0, $b.Size);'
        '$b.Save("%s", [System.Drawing.Imaging.ImageFormat]::Png)'
    ) % (w, h, x, y, str(SHOT).replace("\\", "\\\\"))
    try:
        subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                        "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    sys.exit(main())
