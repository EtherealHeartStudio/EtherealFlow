"""生成 README 用的「真流式输入」演示动图。

这个脚本不截屏，而是用 Pillow 按悬浮窗（client/overlay.py）的同一套视觉参数
重绘一遍：同一个配色、同一个圆角、同一套字号层级。这样动图里的画面和用户真正
看到的悬浮窗是一致的，而且可以随样式改动重新生成，不依赖某一台机器的桌面状态。

产出：
    docs/assets/streaming-demo.gif   动图（README 主图）
    docs/assets/streaming-demo.png   静帧海报（预览/降级用）

用法：
    .venv\\Scripts\\python.exe tools\\make_demo_gif.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    print("需要 Pillow：.venv\\Scripts\\python.exe -m pip install pillow")
    raise SystemExit(1)

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"

# ---------------------------------------------------------------- 视觉参数
W, H = 880, 176
MARGIN = 14
RADIUS = 14

BG = (18, 20, 26)
CARD = (30, 34, 43)
BORDER = (62, 70, 87)
SEP = (47, 53, 66)
ACCENT = (94, 190, 255)
ACCENT_DIM = (58, 116, 158)
DONE = (104, 222, 160)
TEXT = (240, 244, 250)
DIM = (148, 158, 176)

FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\msyh.ttc", 1),
    (r"C:\Windows\Fonts\Deng.ttf", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
]

# 演示用的识别结果。注意：这是悬浮窗里逐字长出来的最终文本，
# 标点由识别模型自己给出（本项目不做后处理加标点）。
# 长度按真人口述一句话的量级取（约 48 字），这样第一行填满后会停住、第二行接着长，
# 和真实文本框的行为一致；太短的话会在最后一两个字才折行，看起来像文字「缩回去」。
FINAL_TEXT = (
    "升级到 https 之后，再测试一次本地流式识别：首字大约一秒就能出现，"
    "后面的文字会跟着说话的节奏持续长出来。"
)

# 标点后多停一拍，让节奏像真人说话
PAUSE_AFTER = {"，": 2.0, "。": 2.6, "、": 1.6, "：": 2.0}
BASE_STEP_MS = 72


def load_font(size: int):
    for path, index in FONT_CANDIDATES:
        p = Path(path)
        if not p.exists():
            continue
        try:
            return ImageFont.truetype(str(p), size, index=index)
        except Exception:
            continue
    print("找不到中文字体，请检查 C:\\Windows\\Fonts")
    raise SystemExit(1)


FONT_TEXT = load_font(27)
FONT_LABEL = load_font(18)
FONT_TIMER = load_font(17)


# 断在这些字符之后更符合阅读习惯
BREAK_CHARS = "，。、；：！？,.;:!?）】」》"


def is_word_char(ch: str) -> bool:
    """只有 ASCII 字母数字才算「单词内部」字符。

    坑：str.isalnum() 对汉字同样返回 True，直接拿它判断会把中文当成英文单词，
    「整词换行」的逻辑就会在句子中间的空格处把行劈开（第一版折行提前到句首
    就是这么来的）。
    """
    return ch.isascii() and ch.isalnum()


def greedy_lines(text: str, font, max_width: int) -> list[str]:
    """按显示宽度贪心折行，ASCII 单词尽量不切断。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if not cur or font.getlength(trial) <= max_width:
            cur = trial
            continue
        if is_word_char(ch) and cur and is_word_char(cur[-1]) and " " in cur:
            head, _, tail = cur.rpartition(" ")
            if head:
                lines.append(head)
                cur = tail + ch
                continue
        lines.append(cur)
        cur = ch
    if cur:
        lines.append(cur)
    return lines


def balanced_two(text: str, font, max_width: int) -> list[str] | None:
    """把文本尽量均衡地断成两行，避免第二行只剩一两个字的「孤行」。"""
    best: tuple[float, int] | None = None
    for i in range(1, len(text)):
        if text[i - 1] == " " or text[i] == " ":
            continue
        left, right = text[:i], text[i:]
        if is_word_char(text[i - 1]) and is_word_char(text[i]):
            continue  # 不断开英文单词
        w1, w2 = font.getlength(left), font.getlength(right)
        if w1 > max_width or w2 > max_width:
            continue
        score = abs(w1 - w2) - (max_width * 0.12 if text[i - 1] in BREAK_CHARS else 0)
        if best is None or score < best[0]:
            best = (score, i)
    if best is None:
        return None
    return [text[: best[1]], text[best[1] :]]


def wrap(text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    lines = greedy_lines(text, font, max_width)
    if len(lines) == 2 and font.getlength(lines[1]) < max_width * 0.35:
        bal = balanced_two(lines[0] + lines[1], font, max_width)
        if bal:
            return bal
    if len(lines) <= max_lines:
        return lines
    # 超出可见行数：保留能放下的最长后缀（真实悬浮窗会滚掉更早的内容）
    for start in range(1, len(text)):
        suffix = text[start:].lstrip()
        if not suffix:
            break
        cand = greedy_lines(suffix, font, max_width)
        if len(cand) <= max_lines:
            return cand
    return lines[-max_lines:]


TEXT_X = MARGIN + 18
TEXT_MAX_W = W - MARGIN - 18 - TEXT_X
LINE_H = 36
TEXT_TOP = 72


def render(text: str, elapsed_ms: int, done: bool, phase: int) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rounded_rectangle(
        [MARGIN, MARGIN, W - MARGIN - 1, H - MARGIN - 1],
        radius=RADIUS,
        fill=CARD,
        outline=BORDER,
        width=1,
    )

    # ---- 顶栏：电平条 + 状态文字 + 计时器
    bar_x = TEXT_X
    cy = 40
    levels = [7, 13, 18, 11, 15, 8]
    for i, base in enumerate(levels):
        if done:
            h = 4
            color = DONE
        else:
            wobble = (phase * 3 + i * 5) % 11
            h = max(4, base - abs(wobble - 5))
            color = ACCENT
        x0 = bar_x + i * 6
        d.rounded_rectangle([x0, cy - h, x0 + 3, cy + h], radius=2, fill=color)

    label = "完成" if done else "正在听…"
    label_color = DONE if done else ACCENT
    d.text((bar_x + 44, cy), label, font=FONT_LABEL, fill=label_color, anchor="lm")

    secs = elapsed_ms // 1000
    timer = f"{secs // 60:02d}:{secs % 60:02d}"
    d.text((W - MARGIN - 20, cy), timer, font=FONT_TIMER, fill=DIM, anchor="rm")

    d.line([(MARGIN + 16, 56), (W - MARGIN - 16, 56)], fill=SEP, width=1)

    # ---- 正文：逐字长出来
    lines = wrap(text, FONT_TEXT, int(TEXT_MAX_W))
    lines = lines[:2]  # 悬浮窗只保留最近两行
    y = TEXT_TOP
    last_w = 0
    for line in lines:
        d.text((TEXT_X, y), line, font=FONT_TEXT, fill=TEXT)
        last_w = FONT_TEXT.getlength(line)
        y += LINE_H

    # 光标：只在「正在听」且这一帧有字时闪烁，暗示文字还在长
    if not done and lines and (phase % 2 == 0):
        caret_x = TEXT_X + last_w + 3
        caret_y = TEXT_TOP + (len(lines) - 1) * LINE_H
        d.rounded_rectangle(
            [caret_x, caret_y + 5, caret_x + 3, caret_y + 28], radius=1, fill=ACCENT
        )

    return img


def build_frames():
    """返回 (frames, durations)。"""
    frames: list[Image.Image] = []
    durations: list[int] = []

    def add(text: str, elapsed: int, done: bool, phase: int, ms: int) -> None:
        frames.append(render(text, elapsed, done, phase))
        durations.append(ms)

    # 阶段一：悬浮窗出现，还没听到内容
    for phase in range(4):
        add("", 0, False, phase, 110)

    # 阶段二：逐字长出来
    elapsed = 0
    phase = 4
    for i in range(1, len(FINAL_TEXT) + 1):
        elapsed += BASE_STEP_MS
        ch = FINAL_TEXT[i - 1]
        add(FINAL_TEXT[:i], elapsed, False, phase, BASE_STEP_MS)
        phase += 1
        extra = PAUSE_AFTER.get(ch)
        if extra and i < len(FINAL_TEXT):
            hold = int(BASE_STEP_MS * extra)
            for k in range(2):
                add(FINAL_TEXT[:i], elapsed + hold // 2 * (k + 1), False, phase + k, hold // 2)
            elapsed += hold
            phase += 2

    # 阶段三：定格。计时器停在音频真实时长（约 4 秒），不再虚增
    final_elapsed = elapsed
    for phase in range(3):
        add(FINAL_TEXT, final_elapsed, True, phase, 150)
    add(FINAL_TEXT, final_elapsed, True, 0, 2200)  # 长停，方便看清

    return frames, durations


def to_palette(img: Image.Image) -> Image.Image:
    return img.convert("P", palette=Image.ADAPTIVE, colors=64)


def selftest() -> None:
    """折行的基本不变量。第一版就是在这里出的错（中文被当成英文单词，行被提前劈开），
    所以留一组断言，改字体或改文案时能立刻发现回归。"""
    max_w = int(TEXT_MAX_W)
    for text in (FINAL_TEXT, FINAL_TEXT[:20], FINAL_TEXT[:34], ""):
        lines = wrap(text, FONT_TEXT, max_w) if text else []
        assert len(lines) <= 2, f"折行超过 2 行：{lines}"
        for ln in lines:
            w = FONT_TEXT.getlength(ln)
            assert w <= max_w, f"行超宽 {w:.0f} > {max_w}：{ln!r}"
        joined = "".join(lines).replace(" ", "")
        assert joined == text.replace(" ", ""), f"折行丢字：{lines}"
    lines = wrap(FINAL_TEXT, FONT_TEXT, max_w)
    assert len(lines) == 2
    assert FONT_TEXT.getlength(lines[1]) >= max_w * 0.35, f"孤行：{lines[1]!r}"
    widths = [round(FONT_TEXT.getlength(l)) for l in lines]
    print(f"折行自检 OK  行宽 {widths} px（上限 {max_w}）")


def main() -> int:
    selftest()
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames, durations = build_frames()

    poster = frames[-1]
    poster_path = ASSETS / "streaming-demo.png"
    poster.save(poster_path, optimize=True)

    gif_path = ASSETS / "streaming-demo.gif"
    pal = [to_palette(f) for f in frames]
    pal[0].save(
        gif_path,
        save_all=True,
        append_images=pal[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )

    total = sum(durations)
    print(f"帧数        : {len(frames)}")
    print(f"循环时长    : {total / 1000:.1f} s")
    print(f"GIF         : {gif_path}  ({gif_path.stat().st_size / 1024:.0f} KB)")
    print(f"海报        : {poster_path}  ({poster_path.stat().st_size / 1024:.0f} KB)")
    print(f"演示文本长度: {len(FINAL_TEXT)} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
