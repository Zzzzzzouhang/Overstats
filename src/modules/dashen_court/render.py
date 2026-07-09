"""电竞法庭（court）判决书 PIL 渲染。

视觉严格仿照原 astrbot court.py 的 HTML 模板（deploy/ow/court.py）：

  - 金色标题栏（#1c202d 底 + #f59e0b 金边 + 金色 h1 标题）；
  - 正文按「文档流」渲染整段 LLM 纯文本：
      * 行内 **粗体** → 金色（#e8d5b7），与原始 <b> 一致；
      * "- " / "* " 无序列表 → 带项目符号、悬挂缩进、自动换行；
      * emoji / 普通段落 → 正文色（#dce1eb）自动换行；
  - 页脚 muted（#788296）+ 顶部分隔线。

emoji / 标点处理**完全复用 shiqu 的助手**，保证两个模块在 LLM 文本上的排版一致。
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
    raise RuntimeError("dashen_court.render requires Pillow") from exc

try:
    from overstats.src.modules.font_resolver import load_font, resolve_resource_dir
    from overstats.src.modules.render_base import finalize_rendered_image
    # 复用 shiqu 的 emoji / 标点处理（逐字一致）
    from overstats.src.modules.dashen_shiqu.render import (
        _normalize_llm_text,
        _is_emoji,
        _emoji_font,
        _glyph_width,
        _char_font,
        _wrap_segments,
        _draw_segments,
        RenderedImage,
    )
except ModuleNotFoundError:  # pragma: no cover
    from src.modules.font_resolver import load_font, resolve_resource_dir
    from src.modules.render_base import finalize_rendered_image
    from src.modules.dashen_shiqu.render import (
        _normalize_llm_text,
        _is_emoji,
        _emoji_font,
        _glyph_width,
        _char_font,
        _wrap_segments,
        _draw_segments,
        RenderedImage,
    )


# ── 视觉常量（与 court.py 原 HTML 模板一致）──
BG = (18, 22, 30)              # #12161e 页面背景
TITLE_BAR_BG = (28, 32, 45)    # #1c202d 标题栏底色
GOLD = (245, 158, 11)          # #f59e0b 金色标题 / 标题栏底边
BOLD_GOLD = (232, 213, 183)    # #e8d5b7 正文内粗体（原 <b> 色）
BODY_COLOR = (220, 225, 235)   # #dce1eb 正文
MUTED_COLOR = (120, 130, 150)  # #788296 页脚 / 次要
SEP_COLOR = (42, 48, 64)       # #2a3040 分隔线

_INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LIST_RE = re.compile(r"^([-*+]\s+|\d+[.)]\s+)(.*)$")


def _parse_inline(text: str) -> List[Tuple[str, Tuple[int, int, int]]]:
    """将一行文本拆分为（文本, 颜色）片段：**粗体** → 金色，其余 → 正文色。"""
    segments: List[Tuple[str, Tuple[int, int, int]]] = []
    pos = 0
    for m in _INLINE_BOLD_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], BODY_COLOR))
        segments.append((m.group(1), BOLD_GOLD))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], BODY_COLOR))
    if not segments:
        segments.append((text, BODY_COLOR))
    return segments


def render_court_image(
    data: Dict[str, Any],
    generated_at: str = "",
) -> RenderedImage:
    """将 court 判决书纯文本渲染为 PNG（视觉对齐原 HTML 模板）。"""
    width = 760
    pad_x = 40
    max_w = width - pad_x * 2

    target_id = str(data.get("target_id") or "未知玩家")
    index = int(data.get("index", 0) or 0)
    raw_text = str(data.get("raw_text") or "")
    map_name = str(data.get("map_name") or "")
    game_mode = str(data.get("game_mode") or "")
    disclaimer = "* 功能仅限娱乐, 切勿因为ai瞎编影响心情"

    # 字体
    f_title = load_font(48, bold=True, prefer_cjk=True)
    f_sub = load_font(24, prefer_cjk=True)
    f_body = load_font(24, prefer_cjk=True)
    f_small = load_font(18, prefer_cjk=True)

    title = f"电竞法庭 · 第 {index + 1} 局判决"
    subtitle = f"{target_id}  ·  {map_name}（{game_mode}）" if (map_name or game_mode) else target_id

    # ── 排版块定义（两遍：先算高度，再绘制）──
    # kind:
    #   "titlebar"        金色标题栏（整行）
    #   "subtitle"        居中副标题（target · map(mode)）
    #   "gap"             纯间距（payload=像素）
    #   "para"            普通段落（整行文本，payload=(segments, font)）
    #   "list"            列表项（payload=(segments, font, indent)）
    #   "footer"          页脚（payload=(text, font)）
    title_bar_h = 92
    list_indent = 26

    raw_lines = raw_text.split("\n")

    blocks: List[Tuple[str, Any]] = [("titlebar", title), ("subtitle", subtitle)]

    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            blocks.append(("gap", 14))
            continue
        lm = _LIST_RE.match(stripped)
        if lm:
            content = _normalize_llm_text(lm.group(2))
            segs = _parse_inline(content)
            blocks.append(("list", (segs, f_body, list_indent)))
        else:
            content = _normalize_llm_text(stripped)
            segs = _parse_inline(content)
            blocks.append(("para", (segs, f_body)))

    blocks.append(("gap", 18))
    blocks.append(("footer", (disclaimer, f_small)))
    if generated_at:
        blocks.append(("footer", (f"生成时间: {generated_at}", f_small)))

    # 测宽用的临时 draw（_wrap_segments 需要 draw 实例）
    _meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    # ── 第一遍：计算总高度 ──
    y = 0
    layout: List[Tuple[str, Any, int]] = []  # (kind, payload, top_y)
    for kind, payload in blocks:
        if kind == "titlebar":
            layout.append((kind, payload, y))
            y += title_bar_h
        elif kind == "subtitle":
            y += 16
            layout.append((kind, payload, y))
            y += int(f_sub.size * 1.5)
        elif kind == "gap":
            y += payload
        elif kind == "para":
            segs, font = payload
            y += 6
            lines = _wrap_segments(segs, _meas, font, max_w, emoji_font=_emoji_font(font.size))
            layout.append((kind, (segs, font, lines), y))
            y += len(lines) * int(font.size * 1.55) + 6
        elif kind == "list":
            segs, font, indent = payload
            y += 6
            avail = max_w - indent
            lines = _wrap_segments(segs, _meas, font, avail, emoji_font=_emoji_font(font.size))
            layout.append((kind, (segs, font, indent, lines), y))
            y += len(lines) * int(font.size * 1.55) + 6
        elif kind == "footer":
            text, font = payload
            y += 6
            layout.append((kind, (text, font), y))
            y += int(font.size * 1.6)

    y += 24

    # ── 第二遍：绘制 ──
    img = Image.new("RGB", (width, y), BG)
    draw = ImageDraw.Draw(img)
    footer_sep_drawn = False

    for kind, payload, top_y in layout:
        if kind == "titlebar":
            draw.rectangle([0, 0, width, title_bar_h], fill=TITLE_BAR_BG)
            draw.line([0, title_bar_h - 1, width, title_bar_h - 1], fill=GOLD, width=3)
            draw.text((pad_x, top_y + (title_bar_h - f_title.size) // 2 - 4), payload, font=f_title, fill=GOLD)
        elif kind == "subtitle":
            draw.text((width // 2, top_y), payload, font=f_sub, fill=BODY_COLOR, anchor="ma")
        elif kind == "para":
            segs, font, lines = payload
            _draw_segments(draw, lines, pad_x, top_y, font, int(font.size * 1.55))
        elif kind == "list":
            segs, font, indent, lines = payload
            # 项目符号（•）对齐首行
            draw.text((pad_x, top_y), "•", font=font, fill=BOLD_GOLD)
            _draw_segments(draw, lines, pad_x + indent, top_y, font, int(font.size * 1.55))
        elif kind == "footer":
            text, font = payload
            # 页脚块整体只画一条顶部分隔线
            if not footer_sep_drawn:
                draw.line([pad_x, top_y - 6, width - pad_x, top_y - 6], fill=SEP_COLOR, width=1)
                footer_sep_drawn = True
            draw.text((pad_x, top_y), text, font=font, fill=MUTED_COLOR)

    return RenderedImage(content=finalize_rendered_image(img), media_type="image/png")


__all__ = ["RenderedImage", "render_court_image"]
