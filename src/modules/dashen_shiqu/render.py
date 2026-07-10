"""是区吗判定书 PIL 渲染。

视觉严格仿照原 astrbot 版 HTML 模板（dark #12161e 背景、金色标题、按档位着色的评分与判定、
胜/负/平着色、队友评分冷灰分离）。使用项目统一的 font_resolver 解析中文字体。
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("dashen_shiqu.render requires Pillow") from exc

try:
    from overstats.src.modules.font_resolver import load_font, resolve_resource_dir
    from overstats.src.modules.render_base import finalize_rendered_image
except ModuleNotFoundError:  # pragma: no cover
    from src.modules.font_resolver import load_font, resolve_resource_dir
    from src.modules.render_base import finalize_rendered_image


# ── 视觉常量（与原始 HTML 模板一致）──
BG = (18, 22, 30)
TITLE_COLOR = (240, 180, 124)        # #f0b47c
H2_COLOR = (201, 152, 106)           # #c9986a
H3_COLOR = (224, 192, 144)           # #e0c090
BODY_COLOR = (220, 225, 235)         # #dce1eb
MUTED_COLOR = (110, 118, 129)        # #6e7681
SEP_COLOR = (110, 118, 129)
HERO_COLOR = (232, 213, 183)         # #e8d5b7
MATE_COLOR = (176, 190, 197)         # #b0bec5
RESULT_COLORS = {
    "胜": (92, 239, 52),
    "负": (236, 110, 114),
    "平": (250, 173, 20),
    "未知": (140, 140, 140),
}
SCORE_COLORS = {
    "god": (230, 126, 34), "boom": (255, 107, 107), "butterfly": (167, 139, 250),
    "ok": (78, 205, 196), "mid": (249, 202, 36), "bad": (225, 112, 85), "terrible": (214, 48, 49),
}
MATE_SCORE_COLORS = {
    "god": (246, 168, 99), "boom": (245, 137, 137), "butterfly": (191, 172, 250),
    "ok": (126, 221, 214), "mid": (245, 211, 90), "bad": (240, 156, 136), "terrible": (238, 101, 102),
}
_VERDICT_CLASS_BY_LABEL = {
    "你是职业吗？": "god", "来了，暴力炸！": "boom", "化蛹成蝶（？）": "butterfly",
    "恭喜，你不是区！": "ok", "不幸，你可能是区？": "mid", "哦灭跌多，你就是区！": "bad",
    "你个大区！！！": "terrible",
}


from dataclasses import dataclass


@dataclass(frozen=True)
class RenderedImage:
    content: bytes
    media_type: str = "image/png"


def _score_class(score: int) -> str:
    if score >= 83:
        return "god"
    if score >= 75:
        return "boom"
    if score >= 68:
        return "butterfly"
    if score >= 60:
        return "ok"
    if score >= 52:
        return "mid"
    if score >= 43:
        return "bad"
    return "terrible"


def _verdict_class(label: str) -> str:
    return _VERDICT_CLASS_BY_LABEL.get((label or "").strip(), "terrible")


# ── emoji 支持（对齐原 HTML 模板的判定档位 / 标题 emoji）──
_VERDICT_EMOJI_BY_LABEL = {
    "你是职业吗？": "😱",
    "来了，暴力炸！": "🤤",
    "化蛹成蝶（？）": "🦋",
    "恭喜，你不是区！": "😂",
    "不幸，你可能是区？": "🤔",
    "哦灭跌多，你就是区！": "🎉",
    "你个大区！！！": "😡",
}
_TITLE_EMOJI = "🔍"


def _verdict_emoji(label: str) -> str:
    return _VERDICT_EMOJI_BY_LABEL.get((label or "").strip(), "")


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F]")

# 半宽数字/小数点：这些字符用半宽拉丁字体渲染，避免 CJK 字体把 123.45 画成全宽。
_NUMERIC_RE = re.compile(r"[0-9.]")

# CJK 汉字（用于词边界判断，决定两端对齐时哪些间隙可扩展）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_latindigit(ch: str) -> bool:
    """是否为半宽拉丁字母或数字/小数点（构成「英文单词 / 数字」的字符）。"""
    if _NUMERIC_RE.match(ch):
        return True
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _is_word_gap(prev_ch: str, ch: str) -> bool:
    """判断 prev_ch 与 ch 之间是否为「词边界」，可在此扩展对齐间隙：

    - 汉字 ↔ 数字/英文（如 汉字 3 / 3 汉字 / 汉字 Redis / Redis 汉字）
    - 空格（_add_num_spacing 插入的 汉字 数字 半宽空格）
    纯汉字之间的字间隙不在此列，两端对齐时不扩展。
    """
    if ch == " " or prev_ch == " ":
        return True
    prev_lat = _is_latindigit(prev_ch)
    cur_lat = _is_latindigit(ch)
    prev_cjk = bool(_CJK_RE.match(prev_ch))
    cur_cjk = bool(_CJK_RE.match(ch))
    return (prev_cjk and cur_lat) or (prev_lat and cur_cjk)

# 全宽 CJK 标点：其字形右侧留白较大，后接半宽数字时视觉间距偏大，
# 全宽 CJK 标点：字形右侧留白较大，后接任意字符时视觉间距偏大，
# 绘制时对紧随的字符施加负字距（向左收紧）。缩短 1/3。
_FULLWIDTH_PUNCT_AFTER_KERN = set("、，。；：！？…—～·」』）】》")
# 负字距比例（相对字号）：用于「全宽标点 + 后随字符」组合的视觉收紧。
_PUNCT_AFTER_KERN_RATIO = 1 / 3


def _is_emoji(ch: str) -> bool:
    return bool(ch) and bool(_EMOJI_RE.match(ch))


# ── 全角/半角标点与数字间隔（逐字对齐原 astrbot _to_half_width_punct）──
# 汉字与数字边界：用零宽前后瞻，在边界插入半宽空格且不消费字符，
# 使「第3局」两侧边界都命中 → 「第 3 局」（修复原先只命中单侧导致「第 3局」）。
_NUM_SPACING_RE = re.compile(r"(?<=[\u4e00-\u9fff])(?=\d)|(?<=\d)(?=[\u4e00-\u9fff])")

# 半宽空格比例：原 astrbot HTML 里逗号后的空格是浏览器默认拉丁字体的正常空格（≈0.25–0.33em），
# 比 CJK 字体自带的全宽空格（1em）更紧凑。渲染时把 ASCII 空格收敛为该比例。
_HALF_SPACE_RATIO = 0.15


def _to_half_width_punct(text: str) -> str:
    """全角标点转半角（与原 astrbot _to_half_width_punct 逐字一致）：
    ：→": "，→", "（→( ）→) ；→; ！? 保留全角。"""
    text = text.replace("：", ": ")
    text = text.replace("，", ", ")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("；", ";")
    return text


def _add_num_spacing(text: str) -> str:
    """汉字与数字边界插入半宽空格（两侧边界都命中，保证「第 3 局」对称）。"""
    return _NUM_SPACING_RE.sub(" ", text)


def _truncate_teammate_id(name: str) -> str:
    """截断队友 ID 用于显示：

    - 「#」前最多 8 个字符（汉字 ID 部分）
    - 「#」后最多 6 位数字（数字 ID / 判别式）
    无「#」则整体最多 8 个字符。
    """
    name = str(name or "")
    if "#" in name:
        before, after = name.split("#", 1)
        before = before[:8]
        digits = re.sub(r"\D", "", after)[:6]
        return f"{before}#{digits}"
    return name[:8]


def _normalize_llm_text(text: str) -> str:
    """LLM 文本归一化：半角标点 + 中文数字间隔（逐字对齐原 _format_text）。"""
    return _add_num_spacing(_to_half_width_punct(text))


def _glyph_width(draw, ch: str, font, emoji_font=None, half_font=None) -> float:
    """单字符绘制宽度。空格收敛为半宽（_HALF_SPACE_RATIO），其余按字体实际度量。"""
    if ch == " ":
        return max(2, int(font.size * _HALF_SPACE_RATIO))
    f = _char_font(ch, font, emoji_font, half_font)
    return draw.textlength(ch, font=f)


def _is_scalable_emoji_font(path: Path) -> bool:
    """验证该彩色 emoji 字体是否「可缩放」（COLRv1 矢量）。

    旧版 NotoColorEmoji.ttf 是位图字体（CBDT），仅支持固定像素尺寸，
    任意尺寸加载会抛 ``invalid pixel size``，导致 emoji 退化成豆腐块。
    COLRv1 矢量版可任意尺寸加载，故用它作为「可用」判据。
    """
    try:
        from PIL import ImageFont

        ImageFont.truetype(str(path), 64)
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def _ensure_noto_color_emoji() -> Optional[Path]:
    """确保本机有可缩放（COLRv1 矢量）的 Noto Color Emoji 字体，用于真彩色渲染。

    - Windows 直接返回 None（使用系统 Segoe UI Emoji，本身可缩放）。
    - 本地缓存 / 系统字体若已存在但为「位图旧版」（任意尺寸加载抛 invalid pixel size），
      则删除并重新下载 @main 的 COLRv1 可缩放版本，避免 emoji 退化成豆腐块。
    - 否则从 CDN（jsDelivr 优先，GitHub 兜底）下载到 res/NotoColorEmoji.ttf。
    失败返回 None，调用方退化为单色/豆腐块。
    """
    if sys.platform.startswith("win"):
        return None

    local = resolve_resource_dir() / "NotoColorEmoji.ttf"
    if local.exists():
        # 已是可缩放版本则直接复用；否则是位图旧版，删掉重下 COLRv1。
        if _is_scalable_emoji_font(local):
            return local
        try:
            local.unlink()
        except Exception:
            pass

    # 常见系统已安装路径（同样验证可缩放）
    system_paths = (
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf"),
    )
    for p in system_paths:
        if p.exists() and _is_scalable_emoji_font(p):
            return p

    # 下载 @main 的 COLRv1 可缩放版本到 res/ 缓存（仅首次）
    urls = (
        "https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@main/fonts/NotoColorEmoji.ttf",
        "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf",
    )
    try:
        import httpx

        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    local.write_bytes(resp.content)
                    # 校验：下载后必须可缩放（COLRv1），否则丢弃回退
                    if local.stat().st_size > 100_000 and _is_scalable_emoji_font(local):
                        return local
                    try:
                        local.unlink()
                    except Exception:
                        pass
                except Exception:
                    continue
    except Exception:
        return None
    return None


@lru_cache(maxsize=16)
def _emoji_font(size: int) -> Optional[ImageFont.ImageFont]:
    """解析 emoji 字体（Windows 的 Segoe UI Emoji / 其它平台的 Noto Color Emoji 等）。

    非 Windows 平台若没有系统 emoji 字体，会自动下载 Noto Color Emoji 到 res/。
    找不到时返回 None，emoji 退化为 base 字体（可能显示为豆腐块）。
    """
    noto = _ensure_noto_color_emoji()
    candidates = [
        Path("C:/Windows/Fonts/seguiemj.ttf"),
        Path("C:/Windows/Fonts/seguiemj_0.ttf"),
        resolve_resource_dir() / "NotoColorEmoji.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf"),
        Path("/System/Library/Fonts/Apple Color Emoji.ttf"),
    ]
    if noto is not None:
        # 已确保可用的 Noto 字体优先（真彩色）
        candidates.insert(0, noto)
    # 兜底：单色矢量 emoji 字体（任意尺寸可缩放，Pillow 一定能渲染，
    # 避免 NotoColorEmoji 位图字体在任意尺寸下触发“invalid pixel size”退化成豆腐块）。
    mono = _ensure_noto_emoji_mono()
    if mono is not None:
        candidates.append(mono)
    for c in candidates:
        if not c.exists():
            continue
        try:
            return ImageFont.truetype(str(c), size)
        except Exception:
            continue
    return None


@lru_cache(maxsize=1)
def _ensure_noto_emoji_mono() -> Optional[Path]:
    """确保本机有单色矢量 emoji 字体（Noto Emoji），作为彩色 emoji 不可用时的兜底。

    NotoColorEmoji.ttf 是位图彩色字体，仅支持固定像素尺寸；render 传入 38/64/34 等
    任意尺寸时 freetype 抛 ``invalid pixel size``，导致 emoji 退化成豆腐块。Noto Emoji
    是 outline 矢量字体，任意尺寸可缩放，Pillow 稳定渲染。优先复用已缓存/系统字体，
    否则从 CDN 下载到 res/NotoEmoji-Regular.ttf（仅首次，best-effort）。
    """
    local = resolve_resource_dir() / "NotoEmoji-Regular.ttf"
    if local.exists() and local.stat().st_size > 50_000:
        return local
    system_paths = (
        Path("/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoEmoji-Regular.ttf"),
        Path("/usr/share/fonts/noto-emoji/NotoEmoji-Regular.ttf"),
    )
    for p in system_paths:
        if p.exists():
            return p
    urls = (
        "https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@main/fonts/NotoEmoji-Regular.ttf",
        "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoEmoji-Regular.ttf",
    )
    try:
        import httpx

        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    local.write_bytes(resp.content)
                    if local.stat().st_size > 50_000:
                        return local
                except Exception:
                    continue
    except Exception:
        return None
    return None


@lru_cache(maxsize=16)
def _is_regular_latin_font(path: Path) -> bool:
    """判断该拉丁字体是否为「常规字重、非斜体」（如 Arial / DejaVu Sans Regular）。

    res/en.ttf 实为 Helvetica Bold Oblique（粗体斜体），用于数字会让数字变粗斜，
    故需过滤掉 bold/oblique/italic 字重；优先用常规字体。
    """
    try:
        family, style = ImageFont.truetype(str(path), 12).getname()
    except Exception:
        return False
    s = (style or "").lower()
    if "bold" in s or "oblique" in s or "italic" in s:
        return False
    return True


def _half_width_font(size: int) -> Optional[ImageFont.ImageFont]:
    """半宽数字字体（用于渲染数字与小数点，避免 CJK 字体把 123.45 画成全宽）。

    优先用系统常规（Regular、非斜体）拉丁字体；明确排除 res/en.ttf —— 它实为
    Helvetica Bold Oblique，会让数字变粗斜。找不到常规拉丁字体时回退 None（用 base 字体）。
    """
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/opentype/liberation/LiberationSans-Regular.ttf"),
    ]
    for c in candidates:
        if c.exists() and _is_regular_latin_font(c):
            try:
                return ImageFont.truetype(str(c), size)
            except Exception:
                continue
    return None


def _char_font(
    ch: str,
    base_font: ImageFont.ImageFont,
    emoji_font,
    half_font=None,
) -> ImageFont.ImageFont:
    if emoji_font and _is_emoji(ch):
        return emoji_font
    if half_font and _NUMERIC_RE.match(ch):
        return half_font
    return base_font


def _draw_text_emoji(
    draw: ImageDraw.ImageDraw,
    cx: int,
    y: int,
    text: str,
    base_font: ImageFont.ImageFont,
    fill: Tuple[int, int, int],
    emoji_font,
    half_font=None,
) -> int:
    """按字符绘制单行文本（水平居中），emoji 用 emoji 字体，数字/小数点用半宽字体；返回结束 x。"""
    widths = [_glyph_width(draw, ch, base_font, emoji_font, half_font) for ch in text]
    x = cx - sum(widths) / 2
    for ch, w in zip(text, widths):
        f = _char_font(ch, base_font, emoji_font, half_font)
        dy = int(base_font.size * 0.06) if (emoji_font and _is_emoji(ch)) else 0
        draw.text((x, y - dy), ch, font=f, fill=fill)
        x += w
    return x


# 行首禁则：这些标点/符号不应出现在行首，换行时应压缩到上一行结尾。
_LINE_START_FORBIDDEN = set(
    "，。、；：！？…—～·「」『』“”‘’（）《》【】〔〕〖〗〈〉"
    "．，．；：！？｛｝（）＂＇"
    ",.;:!?)]}>\"'"
)


def _is_line_start_forbidden(ch: str) -> bool:
    return ch in _LINE_START_FORBIDDEN


def _wrap_segments(
    segments: Sequence[Tuple[str, Tuple[int, int, int]]],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width: int,
    emoji_font=None,
    half_font=None,
) -> List[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]]]:
    """把一个由 (文本, 颜色) 组成的序列按字符贪婪换行。

    emoji 字符使用 emoji_font 测量宽度；数字/小数点用 half_font；每个字符携带自身字体，
    供 _draw_segments 使用。

    行首禁则：溢出时若即将换行的字符是标点/符号，不把它「提到上一行结尾」（避免从下行
    提字上来的观感），而是把它的**前一个字连同该符号一起推到下一行开头**，符号既不在行首，
    也不向上提字。
    """
    lines: List[Tuple[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]], bool]] = []
    cur: List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]] = []
    cur_w = 0
    for text, color in segments:
        for ch in text:
            f = _char_font(ch, font, emoji_font, half_font)
            cw = _glyph_width(draw, ch, font, emoji_font, half_font)
            if cur_w + cw > max_width and cur:
                last_ch = cur[-1][0]
                # 不拆英文/数字单词：若溢出点处于拉丁串内部（前字与当前字都是拉丁/数字），
                # 把整个拉丁串连同当前字符一起推到下一行，避免 KD 这类单词被从中间拆开。
                if _is_latindigit(last_ch) and _is_latindigit(ch):
                    run = []
                    while cur and _is_latindigit(cur[-1][0]):
                        run.insert(0, cur.pop())
                    if cur:
                        cur_w = sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c, _, _ in cur)
                        lines.append((cur, True))
                        cur = run + [(ch, color, f)]
                        cur_w = sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c, _, _ in cur)
                        continue
                    # 整行就是一个超长拉丁串，无法整体下移：放回并退回普通断行（必要时才拆词）
                    cur = run
                    cur_w = sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c, _, _ in cur)
                # 行首禁则：把禁则符号 + 前驱一起推到下一行开头（不向上提字）。
                # 若前驱是拉丁字符，把整个拉丁串一起推下，避免拆开英文单词（如 KD 被拆）。
                if _is_line_start_forbidden(ch) and len(cur) > 1:
                    if _is_latindigit(cur[-1][0]):
                        run = []
                        while cur and _is_latindigit(cur[-1][0]):
                            run.insert(0, cur.pop())
                        cur_w = sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c, _, _ in cur)
                        lines.append((cur, True))
                        cur = run + [(ch, color, f)]
                        cur_w = sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c, _, _ in cur)
                        continue
                    last_ch2, last_color, last_f = cur.pop()
                    last_w = _glyph_width(draw, last_ch2, font, emoji_font, half_font)
                    cur_w -= last_w
                    lines.append((cur, True))
                    cur = [(last_ch2, last_color, last_f), (ch, color, f)]
                    cur_w = last_w + cw
                    continue
                # 因宽度溢出而断行：此行是「完整行」，需两端对齐
                if cur:
                    lines.append((cur, True))
                cur = []
                cur_w = 0
            cur.append((ch, color, f))
            cur_w += cw
    if cur:
        # 段落最后一行（自然结束，非溢出），左对齐即可
        lines.append((cur, False))

    # 反向回填：贪心断行后，上一行可能因「行首禁则推下」而富余（尤其纯汉字行
    # 不做两端对齐拉伸，会留下大段空白）。此时把下一行开头的「首词」提上来填满
    # 上一行，消除行尾大空白。例如「…盲盒，西拉、源氏、死」+「神、斩仇…」→
    # 把「神、」回填成「…死神、」，避免「死」后留白、且「神、」上提符合观感。
    # 回填只在「并入后不超宽」时发生，且不跨段。
    lines = _backfill_lines(lines, draw, font, max_width, emoji_font, half_font)
    return lines


def _line_width(chars, draw, font, emoji_font, half_font) -> int:
    return sum(_glyph_width(draw, c[0], font, emoji_font, half_font) for c in chars)


def _backfill_lines(
    lines,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width: int,
    emoji_font=None,
    half_font=None,
):
    """从后向前逐行回填：把下一行开头的字符尽量搬到上一行结尾，消除上一行因
    「行首禁则推下」留下的富余空白（尤其纯汉字行不做两端对齐拉伸时的大段留白）。

    规则：
    - 逐字上提，直到上一行放不下下一行首字符为止；
    - 若上提后下一行剩余首字符变成「行首禁则符号」，则把它也一并提上来
      （禁则符号不单独留在行首）；
    - 段末行（is_full=False）始终不参与上提（保持自然结尾，不被拉平）。
    这样「…源氏、死」+「神、斩仇…」会变成「…源氏、死神、斩仇…」，把「神、」
    上提填行，避免「死」后留白。
    """
    if len(lines) < 2:
        return lines
    out = [list(item) for item in lines]  # 可变 [(chars, is_full)]

    # 从倒数第二行向上遍历；段落最后一行（is_full=False）不参与作为「被填」的上一行
    i = len(out) - 2
    while i >= 0:
        cur_chars, cur_full = out[i]
        if not cur_full:
            # 上一行是段末自然行，不往上拉（保持原样），继续看更上面
            i -= 1
            continue
        nxt_chars, nxt_full = out[i + 1]
        # 贪心逐字上提。允许略微超出一个字宽容差：绘制端会对「完整行」做两端对齐
        # （slack<0 时负向收紧），所以上一行多提一个字不会超出图片，反而能消除行尾大留白。
        while nxt_chars:
            w = _glyph_width(draw, nxt_chars[0][0], font, emoji_font, half_font)
            if _line_width(cur_chars, draw, font, emoji_font, half_font) + w > max_width + font.size:
                break
            cur_chars.append(nxt_chars.pop(0))
            # 若提完后下一行新首字符是「行首禁则符号」，必须把它也一并带走
            # （禁则符号单独悬在下一行行首比当前行略微超出更糟）。放宽两个字宽容差，
            # 允许上一行末尾多带禁则符号（如「神、」整体连回上一行）。
            if nxt_chars and _is_line_start_forbidden(nxt_chars[0][0]):
                w2 = _glyph_width(draw, nxt_chars[0][0], font, emoji_font, half_font)
                if _line_width(cur_chars, draw, font, emoji_font, half_font) + w2 <= max_width + 2 * font.size:
                    cur_chars.append(nxt_chars.pop(0))
        out[i][0] = cur_chars
        out[i][1] = True
        if not nxt_chars:
            # 下一行被吃空：它原本若是段末行，则当前行降级为段末行
            out.pop(i + 1)
            if not nxt_full:
                out[i][1] = False
            # 继续向上，看能否把更上面的行也填进来
        else:
            out[i + 1][0] = nxt_chars
            i -= 1
    # 清理被吃空的行
    out = [tuple(item) for item in out if item[0]]
    return out


def _draw_segments(
    draw: ImageDraw.ImageDraw,
    lines: List[Tuple[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]], bool]],
    x: int,
    y: int,
    font: ImageFont.ImageFont,
    line_h: int,
    max_width: int,
) -> int:
    cy = y
    for chars, is_full in lines:
        n = len(chars)
        # 各字符基础宽度（用每字符各自的字体度量，与绘制一致）
        base_w = [
            (int(font.size * _HALF_SPACE_RATIO) if ch == " " else draw.textlength(ch, font=fnt))
            for ch, _, fnt in chars
        ]
        # 全宽标点负字距（向左收紧），需计入行宽，否则行尾到不了右边缘
        kerns = [0] * n
        for i in range(1, n):
            if chars[i - 1][0] in _FULLWIDTH_PUNCT_AFTER_KERN and chars[i][0] != " ":
                kerns[i] = int(font.size * _PUNCT_AFTER_KERN_RATIO)
        line_w = sum(base_w) - sum(kerns)
        # 完整行（因溢出断行）做两端对齐：
        # 仅扩展「词边界」间隙（汉字↔数字 / 汉字↔英文 / 空格）。
        # 绝不动纯汉字字间隙，也不动英文单词内部（如 K-D、G-P-S）的间距；
        # 词边界不足（如纯汉字行）则不拉伸，保持左对齐。
        gap_extra = 0.0
        expand_pos: set = set()
        if is_full and n > 1:
            slack = max_width - line_w
            if slack != 0:
                for i in range(1, n):
                    if _is_word_gap(chars[i - 1][0], chars[i][0]):
                        # 记录间隙「前一个字符」的索引：gap_extra 加在 chars[i-1] 的推进上，
                        # 才能真正加宽 chars[i-1]→chars[i] 这个词边界（而非其后一个间隙）。
                        expand_pos.add(i - 1)
                if expand_pos:
                    # slack>0 拉伸；slack<0 压缩（行被反向回填略超宽时，收回 max_width 不外溢）
                    gap_extra = slack / len(expand_pos)
                elif slack < 0:
                    # 纯汉字行无词边界却被回填略超宽：均匀负向收紧所有字间隙回收到 max_width
                    gap_extra = slack / (n - 1)
        cx = x
        for i, (ch, color, f) in enumerate(chars):
            dy = int(font.size * 0.06) if _is_emoji(ch) else 0
            if kerns[i]:
                cx -= kerns[i]
            draw.text((cx, cy - dy), ch, font=f, fill=color)
            adv = base_w[i] + (gap_extra if i in expand_pos else 0.0)
            cx += adv
        cy += line_h
    return cy


def _measure_segments_width(
    draw: ImageDraw.ImageDraw,
    segments: Sequence[Tuple[str, Tuple[int, int, int]]],
    font: ImageFont.ImageFont,
    emoji_font=None,
    half_font=None,
) -> float:
    """按逐字符字体度量一串 segments 的总宽度（与实际绘制一致）。"""
    total = 0.0
    for text, _ in segments:
        for ch in text:
            total += _glyph_width(draw, ch, font, emoji_font, half_font)
    return total


def _fit_font_for_segments(
    draw: ImageDraw.ImageDraw,
    segments: Sequence[Tuple[str, Tuple[int, int, int]]],
    base_font: ImageFont.ImageFont,
    max_width: int,
    min_size: int = 18,
) -> ImageFont.ImageFont:
    """为「必须单行显示」的 segments 选择合适字号：超宽则逐步缩小直到放下（不小于 min_size）。"""
    size = base_font.size
    while size > min_size:
        f = load_font(size, prefer_cjk=True)
        w = _measure_segments_width(
            draw, segments, f, _emoji_font(size), _half_width_font(size)
        )
        if w <= max_width:
            return f
        size -= 2
    return load_font(min_size, prefer_cjk=True)


def render_shiqu_image(result: Dict[str, Any], generated_at: str = "") -> RenderedImage:
    """将结构化判定结果渲染为 PNG 图片（严格仿照原 HTML 视觉）。"""
    width = 760
    pad_x = 40
    max_w = width - pad_x * 2
    bg = Image.new("RGB", (width, 1000), BG)
    draw = ImageDraw.Draw(bg)

    # 字体
    # 第二行 score（xx/100）、第三行 verdict、第四/五行 small（提示、时间）保持原大小；
    # 「时间往下」的正文区（h2 / h3 / body）在放大版基础上再缩小 10%。
    f_title = load_font(28, bold=True, prefer_cjk=True)
    f_score = load_font(64, bold=True, prefer_cjk=True)
    f_verdict = load_font(34, bold=True, prefer_cjk=True)
    f_h2 = load_font(40, bold=True, prefer_cjk=True)
    f_h3 = load_font(35, bold=True, prefer_cjk=True)
    f_body = load_font(32, prefer_cjk=True)
    f_small = load_font(18, prefer_cjk=True)

    score = int(result.get("score", 0) or 0)
    score = max(0, min(100, score))
    verdict = str(result.get("verdict") or "")
    target_id = str(result.get("target_id") or "未知玩家")
    summary = _normalize_llm_text(str(result.get("summary") or ""))
    overall = _normalize_llm_text(str(result.get("overall_comment") or ""))
    gen_time = generated_at or ""
    disclaimer = "* 功能仅限娱乐, 切勿因为ai瞎编影响心情"

    score_color = SCORE_COLORS.get(_score_class(score), (255, 215, 0))
    verdict_color = SCORE_COLORS.get(_verdict_class(verdict), (255, 215, 0))

    # 先排版所有文本块，估算高度后再绘制
    blocks: List[Tuple[str, Any]] = []  # ("title"|"score"|"verdict"|"small"|"h2"|"h3"|"seg", payload)

    def add_seg_line(text_segments, font, gap_before=0):
        blocks.append(("seg", (text_segments, font, gap_before)))

    blocks.append(("title", f"{_TITLE_EMOJI} {target_id} 是区吗判定书"))
    blocks.append(("score", (score, score_color)))
    blocks.append(("verdict", (verdict, verdict_color)))
    blocks.append(("small", disclaimer))
    if gen_time:
        blocks.append(("small", f"生成时间: {gen_time}"))

    # 数据概况
    blocks.append(("h3", "数据概况"))
    add_seg_line([(summary, BODY_COLOR)], f_body, gap_before=6)

    # 逐局点评
    blocks.append(("h2", "逐局点评"))
    for item in result.get("match_comments") or []:
        idx = str(item.get("index", "?"))
        res = str(item.get("result", "未知"))
        # 逐局多英雄名之间的分隔符统一用半角「/」（LLM 常返回「源氏、半藏」）。
        hero = _to_half_width_punct(str(item.get("hero", "未知英雄"))).replace("、", "/")
        comment = _normalize_llm_text(str(item.get("comment", "")))
        res_color = RESULT_COLORS.get(res, RESULT_COLORS["未知"])
        segs = [
            (_add_num_spacing(f"第{idx}局: "), HERO_COLOR),
            (res, res_color),
            (" ", SEP_COLOR),
            (f"{hero}: ", HERO_COLOR),
            (comment, BODY_COLOR),
        ]
        add_seg_line(segs, f_body, gap_before=8)

    # 综合评价
    blocks.append(("h2", "综合评价"))
    add_seg_line([(overall, BODY_COLOR)], f_body, gap_before=6)
    blocks.append(("small", disclaimer))

    # 队友点评
    blocks.append(("h2", "队友点评"))
    mates = result.get("teammate_comments") or []
    if mates:
        for item in mates:
            name = _truncate_teammate_id(item.get("name", "未知队友"))
            tm_score = int(item.get("score", 0) or 0)
            games = item.get("games")
            # (共xx局)：删掉「同」字；局数最多两位（>99 截断为 99）
            games_disp = min(int(games), 99) if games is not None else None
            games_text = _add_num_spacing(_to_half_width_punct(f"（共{games_disp}局）")) if games_disp is not None else ""
            tm_verdict = str(item.get("verdict") or "")
            tm_comment = _normalize_llm_text(str(item.get("comment") or ""))
            tm_score_color = MATE_SCORE_COLORS.get(_score_class(tm_score), (176, 160, 128))
            # 队友评价按档位着色 + 带 emoji（与主判定书一致）
            tm_verdict_color = MATE_SCORE_COLORS.get(_verdict_class(tm_verdict), MATE_COLOR)
            tm_emoji = _verdict_emoji(tm_verdict)
            tm_verdict_disp = _normalize_llm_text(tm_verdict)
            verdict_seg = f"{tm_emoji}{tm_verdict_disp}".strip() if tm_verdict else ""
            # 第一行：队友ID(共x局)：评分 xx/100 —— 必须单行显示，超宽则自动缩小字号压缩
            head_segs = [
                (f"{name}{games_text}：", HERO_COLOR),
                (f"评分 {tm_score}/100", tm_score_color),
            ]
            head_font = _fit_font_for_segments(draw, head_segs, f_body, max_w)
            add_seg_line(head_segs, head_font, gap_before=10)
            # 第二行起：评价 + 点评（换行到评分之后）
            body_segs = []
            if verdict_seg:
                body_segs.append((verdict_seg, tm_verdict_color))
            if tm_comment:
                if body_segs:
                    body_segs.append((" ", SEP_COLOR))
                body_segs.append((tm_comment, MATE_COLOR))
            if body_segs:
                add_seg_line(body_segs, f_body, gap_before=4)
    else:
        add_seg_line([("暂无共同游戏≥2局的队友。", MATE_COLOR)], f_body, gap_before=8)

    # ── 计算总高度 ──
    y = 36
    layout: List[Tuple[str, Any, int]] = []  # (kind, payload, top_y)
    for kind, payload in blocks:
        if kind == "title":
            y += 16
            layout.append((kind, payload, y))
            y += 64
        elif kind == "score":
            y += 6
            layout.append((kind, payload, y))
            y += 84
        elif kind == "verdict":
            y += 0
            layout.append((kind, payload, y))
            y += 48
        elif kind == "small":
            y += 4
            layout.append((kind, payload, y))
            y += 26
        elif kind == "h2":
            y += 24
            layout.append((kind, payload, y))
            y += 58
        elif kind == "h3":
            y += 18
            layout.append((kind, payload, y))
            y += 50
        elif kind == "seg":
            segs, font, gap = payload
            y += gap
            lines = _wrap_segments(
                segs, draw, font, max_w,
                emoji_font=_emoji_font(font.size),
                half_font=_half_width_font(font.size),
            )
            layout.append((kind, (segs, font, lines), y))
            y += len(lines) * int(font.size * 1.5) + 4
    y += 36

    img = Image.new("RGB", (width, y), BG)
    draw = ImageDraw.Draw(img)

    for kind, payload, top_y in layout:
        if kind == "title":
            _draw_text_emoji(
                draw, width // 2, top_y, payload, f_title, TITLE_COLOR,
                _emoji_font(f_title.size), _half_width_font(f_title.size),
            )
            # 标题下方加一条 markdown「---」风格水平分隔线：满宽（0~width）、1px、上下留白
            line_y = top_y + int(f_title.size * 1.6)
            draw.line([(0, line_y), (width, line_y)], fill=TITLE_COLOR, width=1)
        elif kind == "score":
            score_val, color = payload
            draw.text((width // 2, top_y), f"{score_val}/100", font=f_score, fill=color, anchor="ma")
        elif kind == "verdict":
            label, color = payload
            vlabel = f"{_verdict_emoji(label)} {_to_half_width_punct(label)}".strip()
            _draw_text_emoji(
                draw, width // 2, top_y, vlabel, f_verdict, color,
                _emoji_font(f_verdict.size), _half_width_font(f_verdict.size),
            )
        elif kind == "small":
            draw.text((width // 2, top_y), payload, font=f_small, fill=MUTED_COLOR, anchor="ma")
        elif kind == "h2":
            draw.text((pad_x, top_y), payload, font=f_h2, fill=H2_COLOR)
        elif kind == "h3":
            draw.text((pad_x, top_y), payload, font=f_h3, fill=H3_COLOR)
        elif kind == "seg":
            segs, font, lines = payload
            _draw_segments(draw, lines, pad_x, top_y, font, int(font.size * 1.5), max_w)

    return RenderedImage(content=finalize_rendered_image(img), media_type="image/png")
