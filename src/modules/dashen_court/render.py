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
import json
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
        _half_width_font,
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
        _half_width_font,
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
ERROR_COLOR = (229, 72, 77)     # #e5484d 渲染报错（格式错误）

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
    # 强制 JSON 输出：raw_response 现存放 JSON（含 verdict 字段）；
    # 兼容未迁移的旧版纯文本记录（不以 '{' 开头时按纯文本处理）。
    verdict, parse_err = _extract_court_verdict(raw_text)
    if parse_err:
        return _render_court_error(parse_err)
    map_name = str(data.get("map_name") or "")
    game_mode = str(data.get("game_mode") or "")
    disclaimer = "* 功能仅限娱乐, 切勿因为ai瞎编影响心情"

    # 字体
    f_title = load_font(40, bold=True, prefer_cjk=True)
    f_body = load_font(24, prefer_cjk=True)
    f_small = load_font(18, prefer_cjk=True)

    # ── 排版块定义（两遍：先算高度，再绘制）──
    # kind:
    #   "title"           居中大标题（⚖️ 开头行，payload=(segments, font)）
    #   "para"            普通段落（整行文本，payload=(segments, font)），含 📋/🗺️ 等行，左对齐接 emoji
    #   "gap"             纯间距（payload=像素）
    #   "list"            列表项（payload=(segments, font, indent)）
    #   "footer"          页脚（payload=(text, font)）
    list_indent = 26

    raw_lines = verdict.split("\n")

    blocks: List[Tuple[str, Any]] = []

    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            blocks.append(("gap", 14))
            continue
        # ⚖️ 开头的「电竞法庭判决书」作为居中大标题，其余正文保持左对齐
        if stripped.startswith("⚖️"):
            content = _normalize_llm_text(stripped)
            segs = _parse_inline(content)
            blocks.append(("title", (segs, f_title)))
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
        if kind == "gap":
            y += payload
        elif kind == "title":
            segs, font = payload
            y += 12
            lines = _wrap_segments(segs, _meas, font, max_w, emoji_font=_emoji_font(font.size))
            layout.append((kind, (segs, font, lines), y))
            y += len(lines) * int(font.size * 1.6) + 16
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
        if kind == "title":
            segs, font, lines = payload
            half = _half_width_font(font.size)
            ef = _emoji_font(font.size)
            line_h = int(font.size * 1.6)
            for i, (chars, _full) in enumerate(lines):
                lw = sum(_glyph_width(draw, c[0], font, ef, half) for c in chars)
                x = max(pad_x, (width - lw) // 2)
                cx = x
                for ch, color, fn in chars:
                    draw.text((cx, top_y + i * line_h), ch, font=fn, fill=color)
                    cx += _glyph_width(draw, ch, font, ef, half)
        elif kind == "para":
            segs, font, lines = payload
            _draw_segments(draw, lines, pad_x, top_y, font, int(font.size * 1.55), max_w)
        elif kind == "list":
            segs, font, indent, lines = payload
            # 项目符号（•）对齐首行
            draw.text((pad_x, top_y), "•", font=font, fill=BOLD_GOLD)
            _draw_segments(draw, lines, pad_x + indent, top_y, font, int(font.size * 1.55), max_w - indent)
        elif kind == "footer":
            text, font = payload
            # 页脚块整体只画一条顶部分隔线
            if not footer_sep_drawn:
                draw.line([pad_x, top_y - 6, width - pad_x, top_y - 6], fill=SEP_COLOR, width=1)
                footer_sep_drawn = True
            draw.text((pad_x, top_y), text, font=font, fill=MUTED_COLOR)

    return RenderedImage(content=finalize_rendered_image(img), media_type="image/png")


def _assemble_court_text(obj: dict) -> str:
    """将多字段结构化 JSON 组装为带 ⚖️/📋/🗺️ 标记的判决书正文（与旧版纯文本格式一致）。"""
    parts = []

    def _s(key: str, default: str = "?") -> str:
        return str(obj.get(key, default) or default)

    parts.append("⚖️ **电竞法庭判决书**")
    parts.append("")
    parts.append(f"📋 **案件编号**：第 {_s('case_no')} 局")
    parts.append(f"🗺️ **案发地点**：{_s('location')}")
    parts.append("")

    mvp = obj.get("mvp") or {}
    mvp_player = str(mvp.get("player", "?") or "?")
    mvp_reason = str(mvp.get("reason", "") or "")
    parts.append(f"🏆 **MVP（最佳表现者）**：{mvp_player}——{mvp_reason}")
    parts.append("")

    defendant = obj.get("defendant") or {}
    def_player = str(defendant.get("player", "?") or "?")
    def_charges = str(defendant.get("charges", "") or "")
    parts.append(f"👎 **最差表现者（被告）**：{def_player}")
    parts.append(f"**原罪清单**：{def_charges}")
    parts.append("")

    focus = obj.get("focus_verdict") or {}
    foc_player = str(focus.get("player", "?") or "?")
    foc_score = str(focus.get("score", "?") or "?")
    foc_reason = str(focus.get("reason", "") or "")
    parts.append(f"⚡ **焦点玩家判决**：{foc_player}—— 评分：{foc_score}")
    parts.append(foc_reason)
    parts.append("")

    parts.append("📊 **全队审判**：")
    for tv in (obj.get("team_verdicts") or []):
        if isinstance(tv, dict):
            p = str(tv.get("player", "?") or "?")
            v = str(tv.get("verdict", "") or "")
            parts.append(f"- {p}：{v}")
    parts.append("")

    lanes = obj.get("lane_analysis") or {}
    parts.append("⚔️ **三路对位分析**：")
    parts.append(f"- 坦克位：{str(lanes.get('tank', '?') or '?')}")
    parts.append(f"- 输出位：{str(lanes.get('dps', '?') or '?')}")
    parts.append(f"- 辅助位：{str(lanes.get('healer', '?') or '?')}")

    return "\n".join(parts)


def _extract_court_verdict(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """从 raw_response（JSON）中取出判决书正文。

    支持两种格式：
      1. 新版多字段结构化 JSON（含 case_no/location/mvp/…）
      2. 旧版 "verdict" 单字段 JSON（兼容未迁移的记录）
      3. 旧版纯文本（不以 { 开头）

    返回 ``(verdict_text, error_message)``；``error_message`` 非空表示格式错误。
    """
    s = (raw or "").strip()
    if not s:
        return None, "判决书内容为空（raw_response 为空）"
    # 旧版纯文本（未迁移记录）：直接作为 verdict 使用
    if not s.startswith("{"):
        return s, None
    try:
        obj = json.loads(s)
    except Exception as exc:
        return None, f"判决书 JSON 解析失败: {exc}"
    if not isinstance(obj, dict):
        return None, "判决书 JSON 根节点不是对象"
    # 新版多字段结构化 JSON
    required_multi = ["case_no", "location", "mvp", "defendant", "focus_verdict", "team_verdicts", "lane_analysis"]
    if all(k in obj for k in required_multi):
        return _assemble_court_text(obj), None
    # 旧版单字段 {"verdict": "..."}
    verdict = obj.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        return verdict, None
    return None, "判决书 JSON 缺少必要字段（check case_no/location/mvp/defendant/focus_verdict/team_verdicts/lane_analysis）"


def _render_court_error(message: str) -> RenderedImage:
    """格式错误时渲染一张报错图（红色顶边 + 标题 + 正文）。"""
    width = 760
    pad_x = 40
    max_w = width - pad_x * 2
    f_title = load_font(28, bold=True, prefer_cjk=True)
    f_body = load_font(20, prefer_cjk=True)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = _wrap_segments([(message, BODY_COLOR)], meas, f_body, max_w,
                           emoji_font=_emoji_font(f_body.size))
    title_h = int(f_title.size * 1.6)
    h = 24 + title_h + 16 + len(lines) * int(f_body.size * 1.55) + 24
    img = Image.new("RGB", (width, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 6], fill=ERROR_COLOR)
    d.text((pad_x, 24), "⚠ 电竞法庭判决书生成失败", font=f_title, fill=ERROR_COLOR)
    _draw_segments(d, lines, pad_x, 24 + title_h + 16, f_body, int(f_body.size * 1.55), max_w)
    return RenderedImage(content=finalize_rendered_image(img), media_type="image/png")


__all__ = ["RenderedImage", "render_court_image"]
