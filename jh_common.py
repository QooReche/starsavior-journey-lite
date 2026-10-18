"""jh_db / jh_schedule / jh_v2 / jh_relic / jh_savior / jh_equip 이 함께 쓰는 보상 줄 정규식과 툴팁 HTML 부품.

같은 규칙이 여러 파일에 복사돼 한쪽만 고치는 일을 막기 위해 한 곳에 둔다.
툴팁을 새로 만들 때는 아래 부품(span, img_tag, grid_table, source_html, savior_meta)을 먼저 쓰고, 없을 때만 새로 만든다.
"""
import html as _html
import os
import re
import struct

# ----------------------------------------------------------------------------
# 보상 줄 정규식
# ----------------------------------------------------------------------------
TAG_PREFIX = re.compile(r"^\[(SUCCESS|FAIL)\]\s*")                    # '[SUCCESS] ' / '[FAIL] ' 결과 태그
OR_SPLIT = re.compile(r"\s+or(?:\s+|$)")                              # 줄 중간/끝의 택1 구분자
DIFF_WORD = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)\b")        # 줄 머리의 난이도 단어
DIFF_LINE = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)(?:\s+(.*))?$")   # 난이도 단어 + 나머지
MAIN_STAT = re.compile(r"^(힘|체력|인내|집중|보호)\s*[+-]")             # 주요 스탯 증감 줄


def strip_tag(line):
    """'[SUCCESS] 힘 +3' -> '힘 +3' (앞뒤 공백도 정리)"""
    return TAG_PREFIX.sub("", (line or "").strip())


# ----------------------------------------------------------------------------
# 툴팁 HTML 조각 (Qt 리치 텍스트용: 색과 글자 크기를 인라인 스타일로)
# ----------------------------------------------------------------------------
COL_GOLD = "#ffd27f"       # 제목/강조
COL_CYAN = "#9fe8ff"       # 항목 이름
COL_GREEN = "#a5d6a7"      # 보상/추천
COL_WHITE = "#ffffff"      # 본문
COL_GRAY = "#90a4ae"       # 부가 설명
COL_DIM = "#888888"        # 없음/생략
COL_FOOT = "#78909c"       # 출처 문구
COL_DIVIDER = "#26323a"    # 표의 줄 사이 구분선
SOURCE_NOTE = "출처: 스타세이비어 가이드(참고용, 절대적인 정답 아님)"
ELEMENT_KO = {"sun": "태양", "moon": "달", "star": "별", "order": "질서", "chaos": "혼돈"}


def span(text, color, size, bold=False):
    """글자색·크기를 지정한 <span>. text 는 이미 이스케이프된 HTML 이어야 한다."""
    body = f"<b>{text}</b>" if bold else text
    return f'<span style="color:{color}; font-size:{size}px;">{body}</span>'


def spacer(px):
    """세로 여백"""
    return f'<div style="font-size:{px}px;">&nbsp;</div>'


def source_html(size):
    """툴팁 맨 아래 출처 줄"""
    return span(SOURCE_NOTE, COL_FOOT, size)


def savior_meta(savior, extra=""):
    """'부제 · 속성 · 직업[ · extra]' (이스케이프 전 글자)"""
    return " · ".join(x for x in (savior.get("subtitle"), ELEMENT_KO.get(savior.get("element"), savior.get("element")),
                                   savior.get("className"), extra) if x)


def png_size(path):
    """PNG 헤더에서 (폭, 높이). 못 읽으면 None."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w, h = struct.unpack(">II", head[16:24])
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def img_tag(path, h):
    """캐시된 PNG 를 높이 h(px)로 보이는 <img> (파일이 없거나 못 읽거나 h<=0 이면 ''). 한글 경로도 /로 바꿔 넣는다."""
    if not path or h <= 0 or not os.path.exists(path):
        return ""
    sz = png_size(path)
    if not sz:
        return ""
    w = max(1, round(h * sz[0] / sz[1]))
    return f'<img src="{_html.escape(path.replace(os.sep, "/"))}" width="{w}" height="{h}" style="vertical-align:middle;">'


def divider_row(ncols):
    return f'<tr><td colspan="{ncols}" style="background-color:{COL_DIVIDER}; font-size:1px;">&nbsp;</td></tr>'


def grid_table(rows, label_w=0, heads=None, pad=2):
    """줄 사이에 구분선이 있는 표.
    rows: [(라벨 HTML, [칸 HTML ...])] (라벨이 '' 면 라벨 칸을 거의 없앤다). 칸이 하나뿐인 줄은 가로 전체를 쓴다.
    heads: 칸마다 머리글 HTML 리스트(없으면 머리 줄 없음). label_w: 라벨 칸 폭(px)."""
    ncols = max((len(cells) for _, cells in rows), default=1)
    cw = f' width="{100 // ncols}%"' if ncols > 1 else ""
    trs = []
    if heads:
        trs.append("<tr><td></td>" + "".join(f'<td valign="middle"{cw}>{h}</td>' for h in heads) + "</tr>")
    for i, (label, cells) in enumerate(rows):
        lab = f'<td valign="top" width="{label_w if label else 1}" style="white-space:nowrap;">{label}</td>'
        if len(cells) == 1 and ncols > 1:
            body = f'<td valign="top" colspan="{ncols}">{cells[0]}</td>'
        else:
            body = "".join(f'<td valign="top"{cw}>{c}</td>' for c in cells)
        trs.append("<tr>" + lab + body + "</tr>")
        if i < len(rows) - 1:
            trs.append(divider_row(1 + ncols))
    return f'<table cellspacing="0" cellpadding="{pad}" width="100%">' + "".join(trs) + "</table>"
