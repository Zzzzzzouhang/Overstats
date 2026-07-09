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


def _is_emoji(ch: str) -> bool:
    return bool(ch) and bool(_EMOJI_RE.match(ch))


# ── 全角/半角标点与数字间隔（逐字对齐原 astrbot _to_half_width_punct）──
_NUM_SPACING_RE = re.compile(r"([\u4e00-\u9fff])(\d)|(\d)([\u4e00-\u9fff])")

# 半宽空格比例：原 astrbot HTML 里逗号后的空格是浏览器默认拉丁字体的正常空格（≈0.25–0.33em），
# 比 CJK 字体自带的全宽空格（1em）更紧凑。渲染时把 ASCII 空格收敛为该比例。
_HALF_SPACE_RATIO = 0.30


def _to_half_width_punct(text: str) -> str:
    """全角标点转半角（与原 astrbot _to_half_width_punct 逐字一致）：
    ：→": "，→", "（→( ）→) ；→; ！? 保留全角。"""
    text = text.replace("：", ": ")
    text = text.replace("，", ", ")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("；", ";")
    return text


def _add_num_spacing(text: str) -> str:
    """中文字符与数字之间插入空格，提升可读性（PIL 下用半宽空格绘制）。"""
    return _NUM_SPACING_RE.sub(lambda m: f"{m.group(1) or m.group(3)} {m.group(2) or m.group(4)}", text)


def _normalize_llm_text(text: str) -> str:
    """LLM 文本归一化：半角标点 + 中文数字间隔（逐字对齐原 _format_text）。"""
    return _add_num_spacing(_to_half_width_punct(text))


def _glyph_width(draw, ch: str, font, emoji_font=None) -> float:
    """单字符绘制宽度。空格收敛为半宽（_HALF_SPACE_RATIO），其余按字体实际度量。"""
    if ch == " ":
        return max(2, int(font.size * _HALF_SPACE_RATIO))
    f = _char_font(ch, font, emoji_font)
    return draw.textlength(ch, font=f)


@lru_cache(maxsize=1)
def _ensure_noto_color_emoji() -> Optional[Path]:
    """确保本机有 Noto Color Emoji 字体（用于非 Windows 平台的真彩色 emoji 渲染）。

    - Windows 直接返回 None（使用系统 Segoe UI Emoji）。
    - 已存在系统字体或 res/ 缓存则直接复用。
    - 否则从 CDN（jsDelivr 优先，GitHub 兜底）下载到 res/NotoColorEmoji.ttf。
    失败返回 None，调用方退化为单色/豆腐块。
    """
    if sys.platform.startswith("win"):
        return None

    local = resolve_resource_dir() / "NotoColorEmoji.ttf"
    if local.exists():
        return local

    # 常见系统已安装路径
    system_paths = (
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf"),
    )
    for p in system_paths:
        if p.exists():
            return p

    # 下载到 res/ 缓存（仅首次）
    urls = (
        "https://cdn.jsdelivr.net/gh/googlefonts/noto-emoji@main/fonts/NotoColorEmoji.ttf",
        "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf",
    )
    try:
        import httpx

        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    local.write_bytes(resp.content)
                    if local.stat().st_size > 100_000:  # 至少 100KB，过滤错误页
                        return local
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
    for c in candidates:
        if c.exists():
            try:
                return ImageFont.truetype(str(c), size)
            except Exception:
                continue
    return None


def _char_font(ch: str, base_font: ImageFont.ImageFont, emoji_font) -> ImageFont.ImageFont:
    return emoji_font if (emoji_font and _is_emoji(ch)) else base_font


def _draw_text_emoji(
    draw: ImageDraw.ImageDraw,
    cx: int,
    y: int,
    text: str,
    base_font: ImageFont.ImageFont,
    fill: Tuple[int, int, int],
    emoji_font,
) -> int:
    """按字符绘制单行文本（水平居中），emoji 用 emoji 字体；返回绘制结束的 x 坐标。"""
    widths = [_glyph_width(draw, ch, base_font, emoji_font) for ch in text]
    x = cx - sum(widths) / 2
    for ch, w in zip(text, widths):
        f = _char_font(ch, base_font, emoji_font)
        dy = int(base_font.size * 0.06) if (emoji_font and _is_emoji(ch)) else 0
        draw.text((x, y - dy), ch, font=f, fill=fill)
        x += w
    return x


def _wrap_segments(
    segments: Sequence[Tuple[str, Tuple[int, int, int]]],
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width: int,
    emoji_font=None,
) -> List[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]]]:
    """把一个由 (文本, 颜色) 组成的序列按字符贪婪换行。

    emoji 字符使用 emoji_font 测量宽度；每个字符携带自身字体，供 _draw_segments 使用。
    """
    lines: List[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]]] = [[]]
    cur_w = 0
    for text, color in segments:
        for ch in text:
            f = _char_font(ch, font, emoji_font)
            cw = _glyph_width(draw, ch, font, emoji_font)
            if cur_w + cw > max_width and lines[-1]:
                lines.append([])
                cur_w = 0
            lines[-1].append((ch, color, f))
            cur_w += cw
    return lines


def _draw_segments(
    draw: ImageDraw.ImageDraw,
    lines: List[List[Tuple[str, Tuple[int, int, int], ImageFont.ImageFont]]],
    x: int,
    y: int,
    font: ImageFont.ImageFont,
    line_h: int,
) -> int:
    cy = y
    for line in lines:
        cx = x
        for ch, color, f in line:
            dy = int(font.size * 0.06) if _is_emoji(ch) else 0
            draw.text((cx, cy - dy), ch, font=f, fill=color)
            cx += int(font.size * _HALF_SPACE_RATIO) if ch == " " else draw.textlength(ch, font=f)
        cy += line_h
    return cy


def render_shiqu_image(result: Dict[str, Any], generated_at: str = "") -> RenderedImage:
    """将结构化判定结果渲染为 PNG 图片（严格仿照原 HTML 视觉）。"""
    width = 760
    pad_x = 40
    max_w = width - pad_x * 2
    bg = Image.new("RGB", (width, 1000), BG)
    draw = ImageDraw.Draw(bg)

    # 字体
    f_title = load_font(38, bold=True, prefer_cjk=True)
    f_score = load_font(64, bold=True, prefer_cjk=True)
    f_verdict = load_font(34, bold=True, prefer_cjk=True)
    f_h2 = load_font(30, bold=True, prefer_cjk=True)
    f_h3 = load_font(26, bold=True, prefer_cjk=True)
    f_body = load_font(24, prefer_cjk=True)
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
        hero = _to_half_width_punct(str(item.get("hero", "未知英雄")))
        comment = _normalize_llm_text(str(item.get("comment", "")))
        res_color = RESULT_COLORS.get(res, RESULT_COLORS["未知"])
        segs = [
            (f"第{idx}局: ", HERO_COLOR),
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
            name = str(item.get("name", "未知队友"))
            tm_score = int(item.get("score", 0) or 0)
            games = item.get("games")
            games_text = _to_half_width_punct(f"（共同{games}局）") if games is not None else ""
            tm_verdict = str(item.get("verdict") or "")
            tm_comment = _normalize_llm_text(str(item.get("comment") or ""))
            tm_score_color = MATE_SCORE_COLORS.get(_score_class(tm_score), (176, 160, 128))
            # 队友评价按档位着色 + 带 emoji（与主判定书一致）
            tm_verdict_color = MATE_SCORE_COLORS.get(_verdict_class(tm_verdict), MATE_COLOR)
            tm_emoji = _verdict_emoji(tm_verdict)
            tm_verdict_disp = _normalize_llm_text(tm_verdict)
            verdict_seg = f"{tm_emoji}{tm_verdict_disp}".strip() if tm_verdict else ""
            segs = [
                (f"{name}{games_text}：", HERO_COLOR),
                (f"评分 {tm_score}/100", tm_score_color),
            ]
            if verdict_seg:
                segs.append((f" {verdict_seg}", tm_verdict_color))
            if tm_comment:
                segs.append((tm_comment, MATE_COLOR))
            add_seg_line(segs, f_body, gap_before=10)
    else:
        add_seg_line([("暂无共同游戏≥2局的队友。", MATE_COLOR)], f_body, gap_before=8)

    # ── 计算总高度 ──
    y = 36
    layout: List[Tuple[str, Any, int]] = []  # (kind, payload, top_y)
    for kind, payload in blocks:
        if kind == "title":
            y += 16
            layout.append((kind, payload, y))
            y += 50
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
            y += 44
        elif kind == "h3":
            y += 18
            layout.append((kind, payload, y))
            y += 38
        elif kind == "seg":
            segs, font, gap = payload
            y += gap
            lines = _wrap_segments(segs, draw, font, max_w, emoji_font=_emoji_font(font.size))
            layout.append((kind, (segs, font, lines), y))
            y += len(lines) * int(font.size * 1.5) + 4
    y += 36

    img = Image.new("RGB", (width, y), BG)
    draw = ImageDraw.Draw(img)

    for kind, payload, top_y in layout:
        if kind == "title":
            _draw_text_emoji(draw, width // 2, top_y, payload, f_title, TITLE_COLOR, _emoji_font(f_title.size))
        elif kind == "score":
            score_val, color = payload
            draw.text((width // 2, top_y), f"{score_val}/100", font=f_score, fill=color, anchor="ma")
        elif kind == "verdict":
            label, color = payload
            vlabel = f"{_verdict_emoji(label)} {_to_half_width_punct(label)}".strip()
            _draw_text_emoji(draw, width // 2, top_y, vlabel, f_verdict, color, _emoji_font(f_verdict.size))
        elif kind == "small":
            draw.text((width // 2, top_y), payload, font=f_small, fill=MUTED_COLOR, anchor="ma")
        elif kind == "h2":
            draw.text((pad_x, top_y), payload, font=f_h2, fill=H2_COLOR)
        elif kind == "h3":
            draw.text((pad_x, top_y), payload, font=f_h3, fill=H3_COLOR)
        elif kind == "seg":
            segs, font, lines = payload
            _draw_segments(draw, lines, pad_x, top_y, font, int(font.size * 1.5))

    return RenderedImage(content=finalize_rendered_image(img), media_type="image/png")
