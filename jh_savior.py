# -*- coding: utf-8 -*-
"""구원자 추천 아르카나 (여정 시작 화면).

- 데이터: data/savior_reco.json (tools/extract_savior_reco.py 가 만든 슬림 JSON, 앱은 읽기 전용으로만 쓴다)
- 식별: 좌상단 [부제 줄][이름 줄] OCR 문자열 -> (부제, 이름) 로 구원자 1명 (동명이인은 부제로 구분)
- 툴팁: 슬롯 5개 + 슬롯별 대체 카드(썸네일 포함)
순수 함수 위주(OCR/Qt 와 분리)라 테스트하기 쉽다. 어떤 입력/데이터 오류에도 예외를 밖으로 내지 않는다.
"""
from __future__ import annotations

import difflib
import html as _html
import json
import os
import re
import struct

import jh_common

DATA_FILE = os.path.join("assets", "savior_reco.json")

# 클라이언트 영역 비율 (스크린샷 4장에서 검증: tests/test_savior.py)
SAVIOR_REGIONS = {
    "savior_header": {"top": 0.045, "left": 0.125, "width": 0.070, "height": 0.045},    # '여정 시작' (오른쪽 '?' 아이콘 제외)
    "savior_subtitle": {"top": 0.243, "left": 0.097, "width": 0.170, "height": 0.043},  # 등급 줄 바로 아래 부제/소속
    "savior_name": {"top": 0.284, "left": 0.097, "width": 0.170, "height": 0.045},      # 구원자 이름
}
# '아르카나 편성 화면'에만 있는 '변하지 않는' 고정 글자 (프리셋 이름/번호는 사용자가 바꿀 수 있어 쓰지 않는다):
# 우측 하단 버튼 '전체 해제' / '자동 편성', 그 아래 '특수재화 우선 사용', 맨 아래 '여정 시작' 버튼.
# (구원자 고르기·인자작 화면은 머리글이 같은 '여정 시작' 이라 이것으로 구분한다)
ARCANA_SCREEN_REGIONS = {
    "arcana_btn_clear": {"top": 0.800, "left": 0.782, "width": 0.062, "height": 0.070},
    "arcana_btn_auto": {"top": 0.800, "left": 0.888, "width": 0.062, "height": 0.070},
    "arcana_special": {"top": 0.865, "left": 0.765, "width": 0.135, "height": 0.045},
    "arcana_start": {"top": 0.915, "left": 0.800, "width": 0.065, "height": 0.045},
}
ARCANA_KINDS = {"arcana_btn_clear": "clear", "arcana_btn_auto": "auto", "arcana_special": "special", "arcana_start": "start"}
ARCANA_PHRASE = {"clear": "전체해제", "auto": "자동편성", "special": "특수재화우선사용", "start": "여정시작"}
# '아르카나 고르기 화면'(편성 화면에서 카드 슬롯을 눌러 들어감)에만 있는 글자 = 부정 신호: 하단 안내줄 '구원자와 동일한 타이틀을 지닌...'(왼쪽 일부),
# 우측 상단 보유 수 '78/81'. 툴팁이 가리지 않는 영역만 쓴다(툴팁은 가로 0.245~0.52).
PICKER_REGIONS = {
    "picker_guide": {"top": 0.935, "left": 0.05, "width": 0.20, "height": 0.045},
    "picker_count": {"top": 0.118, "left": 0.745, "width": 0.055, "height": 0.045},
}
# 마우스를 올리면 툴팁이 뜨는 범위(left, top, right, bottom): 등급·부제·이름 블록
SAVIOR_HOVER = (0.03, 0.19, 0.27, 0.34)
TOOLTIP_TOP_FRAC = 0.20        # 툴팁 시작 높이(게임 창 비율)
TOOLTIP_X_FRAC = 0.245         # 툴팁 시작 가로 위치: 좌측 스탯 패널(~0.235) 오른쪽, 우측 편성 카드(0.53~) 왼쪽 = 구원자 일러스트 위

# '인자 고르기' 화면(머리글은 같은 '여정 시작'): 좌측 스탯 아래 '여정 시작 정보' 버튼과 우측 아래 '자동 장착' / '확인' 버튼 (스크린샷 3장으로 보정).
# 구원자 추천 아르카나 툴팁이 뜨는 '편성 화면'과는 다른 화면이라 여정 스탯 우선순위는 여기에서만 띄운다.
INJA_REGIONS = {
    "inja_info": {"top": 0.675, "left": 0.06, "width": 0.26, "height": 0.045},
    "inja_auto": {"top": 0.835, "left": 0.80, "width": 0.12, "height": 0.055},
    "inja_ok": {"top": 0.915, "left": 0.85, "width": 0.10, "height": 0.055},
}
INJA_KINDS = {"inja_info": "info", "inja_auto": "auto", "inja_ok": "ok"}
INJA_PHRASE = {"info": ("여정시작정보",), "auto": ("자동장착", "자동착장"), "ok": ("확인",)}
STAT_TOOLTIP_TOP_FRAC = 0.20      # 스탯 우선순위 툴팁 위치: 이름 블록 오른쪽, 좌측 스탯 패널(~0.2) 오른쪽, 인자 슬롯(y 0.5~) 위
STAT_TOOLTIP_X_FRAC = 0.26       # 스탯 올린 뒤 힘 줄 옆에 붙는 '+10 +15' 표시(~0.27)를 가리지 않게
STAT_WIDE_W_FRAC = 0.42           # 좌우 2열(디펜더)일 때 폭: x 0.245 부터 0.665 까지(열마다 '힘 > 체력 > 인내 > 집중 = 보호' 한 줄이 들어가야 함), 인자 슬롯(y 0.5~) 위

HEADER_PHRASE = "여정시작"
ELEMENT_KO = jh_common.ELEMENT_KO
NAME_MIN = 0.66          # 이름 유사도 하한 (2~4글자라 한 글자 오인식도 허용)
SUB_MIN = 0.70
SUB_MARGIN = 0.08
SUB_MISMATCH = 0.40      # 이름이 하나로 정해져도 부제가 이보다 낮게 닮았으면 다른(신규) 구원자로 본다


def _norm(s):
    return re.sub(r"[^가-힣0-9a-zA-Z]", "", s or "").lower()


def _sim(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- 데이터
def default_data_path():
    import sys
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, DATA_FILE)


def load_reco(path=None):
    """슬림 JSON -> 구원자 리스트. 없거나 깨졌으면 [] (기능만 꺼진다)."""
    try:
        with open(path or default_data_path(), "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        out = [s for s in data.get("saviors", []) if isinstance(s, dict) and s.get("name")]
        for s in out:
            s["_n_name"], s["_n_sub"] = _norm(s.get("name")), _norm(s.get("subtitle"))
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- 화면 판별 / 식별
def match_inja_screen(text, kind):
    """OCR 문자열이 인자 고르기 화면의 고정 글자(kind: info/auto/ok)인가."""
    ko = re.sub(r"[^가-힣]", "", text or "")
    if len(ko) < 2:
        return False
    return any(p in ko or (len(p) >= 4 and len(ko) >= len(p) - 1 and _sim(ko[:len(p)], p) >= 0.75) for p in INJA_PHRASE[kind])      # 한 글자 오인식까지만 허용


def inja_screen_hit(found):
    """인자 고르기 화면 확정 조건: '여정 시작 정보' + 우측 아래 버튼('자동 장착'/'확인') 중 하나."""
    return "info" in found and bool(set(found) - {"info"})


def match_header(text):
    """OCR 문자열이 '여정 시작' 머리글인가 (뒤에 붙는 '?' 아이콘 잡음은 허용)."""
    ko = re.sub(r"[^가-힣]", "", text or "")
    if len(ko) < 3 or len(ko) > 7:
        return False
    return HEADER_PHRASE in ko or _sim(ko[:4], HEADER_PHRASE) >= 0.75


def identify(saviors, sub_cands, name_cands):
    """OCR 후보들로 구원자를 찾는다. -> (matches: [savior], how)  how: 'subtitle' | 'name' | 'ambiguous' | 'unknown' | 'none'
    - 이름이 하나로 정해지면 부제를 못 읽어도 그 구원자.
    - 같은 이름이 여럿이면 부제 유사도로 가른다. 부제를 못 읽었거나 애매하면 후보를 모두 돌려준다(how='ambiguous')."""
    try:
        names = [_norm(t) for t in (name_cands or []) if _norm(t)]
        subs = [_norm(t) for t in (sub_cands or []) if _norm(t)]
        if not saviors or not names:
            return [], "none"
        ns = [(max(_sim(n, s["_n_name"]) for n in names), s) for s in saviors]
        best = max(v for v, _ in ns)
        if best < NAME_MIN:
            if any(len(re.sub(r"[^가-힣]", "", t or "")) >= 2 for t in name_cands or []):
                return [unknown_savior(name_cands)], "unknown"     # 데이터에 없는 이름(신규 구원자 등)
            return [], "none"
        hits = [s for v, s in ns if v >= best - 1e-9]
        if len(hits) == 1:
            if subs and max(_sim(t, hits[0]["_n_sub"]) for t in subs) < SUB_MISMATCH:
                return [unknown_savior(name_cands, hits[0].get("name"))], "unknown"   # 같은 이름의 신규(이격 등) 구원자
            return hits, "name"
        if subs:
            ss = sorted(((max(_sim(t, s["_n_sub"]) for t in subs), i) for i, s in enumerate(hits)), reverse=True)
            if ss[0][0] >= SUB_MIN and (len(ss) == 1 or ss[0][0] - ss[1][0] >= SUB_MARGIN):
                return [hits[ss[0][1]]], "subtitle"
        return hits, "ambiguous"
    except Exception:
        return [], "none"


def unknown_savior(name_cands, name=None):
    """데이터에 없는 구원자용 표시 자리(slots 없음 -> '아직 데이터 없음')."""
    n = name or next((t.strip() for t in (name_cands or []) if t and t.strip()), "")
    return {"key": "__unknown__:" + _norm(n), "name": n, "subtitle": "", "slots": [], "alternatives": [], "unknown": True}


def match_arcana_screen(text, kind):
    """영역 OCR 문자열이 아르카나 편성 화면의 고정 글자인가. kind: 'clear' | 'auto' | 'special' | 'start'."""
    ko = re.sub(r"[^가-힣]", "", text or "")
    p = ARCANA_PHRASE.get(kind)
    if not p or len(ko) < 3:
        return False
    if p in ko:
        return True
    return any(_sim(ko[i:i + len(p)], p) >= 0.74 for i in range(0, max(1, len(ko) - len(p) + 2)))


def arcana_screen_hit(found):
    """found: 읽힌 고정 글자 종류의 집합 -> 편성 화면인가. 버튼 줄(전체 해제/자동 편성) 하나면 충분, 아니면 '특수재화 우선 사용' + '여정 시작' 둘 다."""
    f = set(found or ())
    return bool(f & {"clear", "auto"}) or {"special", "start"} <= f


def match_picker(text, kind):
    """아르카나 고르기 화면 글자인가. kind: 'guide'(하단 안내줄 '동일한 타이틀') | 'count'(보유 수 '78/81')."""
    if kind == "count":
        m = re.fullmatch(r"\D{0,2}(\d{1,3})\s*[/|]\s*(\d{1,3})\D{0,2}", (text or "").strip())
        return bool(m) and int(m.group(2)) >= 10 and int(m.group(1)) <= int(m.group(2))
    ko = re.sub(r"[^가-힣]", "", text or "")
    p = "동일한타이틀"
    if len(ko) < 5:
        return False
    return p in ko or any(_sim(ko[i:i + len(p)], p) >= 0.8 for i in range(0, max(1, len(ko) - len(p) + 2)))


def savior_key(matches):
    return tuple(sorted(s.get("key", "") for s in matches))


# ---------------------------------------------------------------- 툴팁
def thumb_ids(savior):
    """툴팁에 쓰는 arcanaId 들 (썸네일 지연 다운로드 대상)."""
    ids = []
    pool = list(savior.get("slots") or []) + list(savior.get("alternatives") or [])
    for st in savior.get("sets") or []:
        pool += list(st.get("slots") or []) + list(st.get("alternatives") or [])
    for sl in pool:
        for o in (sl or {}).get("options", []):
            if o.get("arcanaId") and o["arcanaId"] not in ids:
                ids.append(o["arcanaId"])
    return ids


png_size = jh_common.png_size


def _thumb(opt, thumb_path, h):
    """카드 썸네일 <img> (캐시에 있을 때만). 없으면 빈 문자열 -> 텍스트만 표시."""
    aid = opt.get("arcanaId")
    if not aid or not thumb_path or h <= 0:
        return ""
    try:
        p = thumb_path(aid)
    except Exception:
        return ""
    return jh_common.img_tag(p, h)


def replaced_target(note):
    """대체 안내 문구 '<X> 대체' -> X (형식이 다르면 None)."""
    m = re.match(r"^(.+?)\s*대체$", (note or "").strip())
    return m.group(1).strip() if m else None


# 글자 크기(px, scale 1.0 기준)와 썸네일 높이는 detail 단계마다 줄이되, 썸네일은 마지막 단계(3) 전까지 끝까지 유지한다.
#  d0 풀 레이아웃 -> d1 썸네일 소형·간격 축소 -> d2 글자 15->13px -> d3 썸네일 제거(마지막 수단)
# 툴팁 색/구분선 상수 (한 곳에 모아 1세트·2세트가 같은 값을 쓴다)
COL_ALT, COL_MUTED, COL_NAME, COL_UNRES = "#ffd27f", "#90a4ae", "#ffffff", "#ffcc80"     # 대체/소제목, 보조 글씨, 카드 이름, 해석 못 한 이름
COL_NUM = "#00e5ff"            # 슬롯 번호
COL_DIVIDER = jh_common.COL_DIVIDER        # 슬롯 사이 구분선 색
DIVIDER_PX = 1                 # 구분선 두께
COL_LOOSE_HEAD = "#9fe8ff"     # '그 밖의 대체' 소제목
COL_EMPTY = "#78909c"          # '(비움)'
COL_FOOT = "#78909c"           # 출처 문구
COL_HINT = "#888888"           # 데이터 없음/미정 안내
COL_WARN = "#ffcc00"           # 같은 이름 구원자 경고
COL_COMMON = "#cfd8dc"         # 공용 아르카나 안내
PROFILES = (
    # th: 슬롯 썸네일, th2: 대체 썸네일, px: 본문, small: 보조, alt: 대체 이름, pad: 셀 패딩, gap: 슬롯 사이 간격(px)
    {"th": 40, "th2": 30, "px": 15, "small": 12, "alt": 14, "pad": 2, "gap": 4},
    {"th": 36, "th2": 28, "px": 14, "small": 12, "alt": 13, "pad": 1, "gap": 2},      # 중간 단계: 썸네일 36px, 본문 14px
    {"th": 30, "th2": 24, "px": 14, "small": 12, "alt": 13, "pad": 1, "gap": 1},
    {"th": 26, "th2": 20, "px": 13, "small": 11, "alt": 13, "pad": 0, "gap": 1},
    {"th": 0, "th2": 0, "px": 13, "small": 11, "alt": 13, "pad": 0, "gap": 1},       # 마지막 수단: 썸네일 제거
)
MAX_DETAIL = len(PROFILES) - 1
NUM_W = 22              # 슬롯 번호 칸 폭
SEP_W = 16              # ' · ' 구분 폭(추정)
NARROW_CONTENT_W = 440 - 36            # 기본 툴팁 폭(BASE_W) - 좌우 패딩(18*2)
# 넓은 모드(2세트 구원자): 좌측 스탯 패널은 가려도 되므로 좌측으로 넓힌다. 이름 블록(y 0.19~0.34) 아래, 우측 편성 카드(x 0.53~) 왼쪽까지.
WIDE_X_FRAC = 0.02
WIDE_WIDTH_FRAC = 0.50
WIDE_TOP_FRAC = 0.345


REF_WINDOW_H = 935          # 기준 게임 창 높이(사용자 현재 창 약 1600x935)
NARROW_MAX_W_FRAC = 0.285   # 보통 모드 폭 상한(창 너비 비율): x 0.245 에서 시작해도 우측 편성 카드(0.53~)를 침범하지 않게
NARROW_MARGIN = 12


def window_k(height):
    """창 높이 기반 배율 k = clamp(height/935, 0.6, 1.4). 구원자 툴팁의 글자/썸네일/패딩/폭에만 곱한다(설정 배율과 곱)."""
    try:
        return min(1.4, max(0.6, float(height) / REF_WINDOW_H))
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


def tooltip_geometry(rect, scale, wide):
    """-> (유효 배율, 툴팁 가로 px, 안쪽 폭 px). 보통: min(440*scale*k, 0.285*W - 여백), 넓음: 0.50*W (글자는 k 로 줄어 줄바꿈 증가 방지)."""
    k = window_k(rect["height"])
    eff = scale * k
    if wide:
        w = round(rect["width"] * WIDE_WIDTH_FRAC)
    else:
        w = max(200, min(round(440 * eff), int(NARROW_MAX_W_FRAC * rect["width"] - NARROW_MARGIN)))
    return eff, w, w - 36


def wide_mode(matches):
    """추천이 2세트(퓨어탱커/공격탱커)인 구원자 한 명이면 넓은 모드."""
    return bool(matches) and len(matches) == 1 and bool((matches[0] or {}).get("sets"))


def _profile(detail, scale, has_thumbs, content_w=None):
    p = PROFILES[min(max(detail, 0), MAX_DETAIL)]
    out = dict(p)
    for k in ("th", "th2"):
        out[k] = round(p[k] * scale) if has_thumbs else 0
    for k in ("px", "small", "alt"):
        out[k] = max(10, round(p[k] * scale))
    out["content_w"] = content_w if content_w else NARROW_CONTENT_W * scale
    out["avail"] = (out["content_w"] - NUM_W - 14) * 0.94              # 번호 칸 오른쪽에서 한 줄에 쓸 수 있는 폭(추정, 여유 6%)
    return out


def _tw(text, px):
    """글자 폭 추정(굵은 글씨, 실측 보정): 한글 1.05em, 공백 0.4em, 그 밖 0.62em."""
    return sum((1.05 if ord(c) >= 0x1100 else 0.4 if c == " " else 0.62) * px for c in text or "")


def _items(options, thumb_path, th, px):
    """옵션들 -> [(옵션, <img> 또는 '', 추정 폭)]"""
    out = []
    for o in options or []:
        if isinstance(o, dict):
            img = _thumb(o, thumb_path, th) if th else ""
            out.append((o, img, _tw(o.get("name"), px) + (th * 0.62 + 10 if th else 0)))
    return out


def _img_td(img, th):
    return f'<td valign="middle" align="center" width="{round(th * 0.62) + 8}">{img}</td>' if th else ""


def _name_html(o, px, prefix=""):
    color = COL_NAME if o.get("arcanaId") or o.get("common") else COL_UNRES
    return f'{prefix}<span style="color:{color}; font-size:{px}px;"><b>{_html.escape(o.get("name") or "")}</b></span>'


def _or_prefix(small):
    return jh_common.span("또는 ", COL_MUTED, small)


def _tbl(rows, pad):
    return f'<table cellspacing="0" cellpadding="{pad}">{rows}</table>'


def _num_td(num_html, valign="middle"):
    return f'<td valign="{valign}" align="center" width="{NUM_W}">{num_html}</td>'


BLANK_TD = f'<td width="{NUM_W}"></td>'
INDENT_TD = '<td width="10"></td>'            # 슬롯 칸 안에서 대체 줄 들여쓰기


def _opts_table(items, P, avail, px, th, first="", blank="", lead="", lead_w=0):
    """옵션들(A or B or C)을 표 하나로: 한 줄에 들어가면 `[앞칸][머리말] [썸][이름] · [썸][이름]` 한 행(칸마다 세로 가운데 정렬),
    폭이 모자랄 때만 항목마다 한 행(`또는` 표시)으로 줄바꿈. first/blank = 첫 행/나머지 행 맨 앞 칸(번호 또는 빈칸)."""
    small, pad = P["small"], P["pad"]
    if not items:
        return _tbl(f'<tr>{first}<td valign="middle">{lead}</td></tr>', pad) if lead else ""
    total = lead_w + sum(w for _, _, w in items) + SEP_W * (len(items) - 1)
    if len(items) == 1 or total <= avail:
        cells = [first]
        if lead:
            cells.append(f'<td valign="middle">{lead}</td>')
        for k, (o, img, _) in enumerate(items):
            if k:
                cells.append(f'<td valign="middle" align="center">{jh_common.span("&nbsp;·&nbsp;", COL_MUTED, px)}</td>')
            cells.append(_img_td(img, th))
            cells.append(f'<td valign="middle">{_name_html(o, px)}</td>')
        return _tbl("<tr>" + "".join(cells) + "</tr>", pad)
    rows = []
    f = first
    head_tbl = ""
    if lead:                                    # 머리 줄은 별도 표로(colspan 이 썸네일 칸 폭을 늘려 항목이 오른쪽으로 밀리는 것 방지)
        head_tbl = _tbl(f'<tr>{first}<td valign="middle">{lead}</td></tr>', pad)
        f = blank
    for k, (o, img, _) in enumerate(items):
        rows.append(f'<tr>{f if k == 0 else blank}{_img_td(img, th)}<td valign="middle">'
                    f'{_name_html(o, px, "" if k == 0 else _or_prefix(small))}</td></tr>')
    return head_tbl + _tbl("".join(rows), pad)


def _alt_lead(a, small, prefix="", attached=False):
    """대체 머리말. 슬롯 바로 아래에 붙은 대체는 위치로 대상을 알 수 있으므로 `↳ 대체 카드 →`(1세트·2세트 공통).
    슬롯에 안 붙은 '그 밖의 대체'는 note 문구(예: 부족한 자리 대체)를 그대로, note 가 없으면 `↳ 대체 카드 →`."""
    note = (a.get("note") or "").strip()
    text = "↳ 대체 카드 →" if (attached or not note) else f"↳ {note} →"
    text = prefix + text
    return text, f'<span style="color:{COL_ALT}; font-size:{small}px; white-space:nowrap;"><b>{_html.escape(text)}</b></span>&nbsp;'


def _alt_block(a, P, thumb_path, avail, prefix="", attached=False, first=BLANK_TD, blank=BLANK_TD):
    """대체 한 줄: `↳ 대체 카드 → [썸]Y · [썸]Z` (안 들어갈 때만 머리 줄 + 항목 줄). 카드가 없으면 안내 문구만."""
    small = P["small"]
    if not a.get("options"):
        return _tbl(f'<tr>{first}<td valign="middle"><span style="color:{COL_ALT}; font-size:{small}px;">'
                    f'※ {_html.escape(prefix + (a.get("note") or ""))}</span></td></tr>', P["pad"])
    text, lead = _alt_lead(a, small, prefix, attached)
    items = _items(a["options"], thumb_path, P["th2"], P["alt"])
    return _opts_table(items, P, avail, P["alt"], P["th2"], first, blank, lead, _tw(text, small) + 8)


def _split_alts(alts, nslots):
    by_slot, loose = {}, []
    for a in alts or []:
        if isinstance(a, dict) and (a.get("options") or a.get("note")):
            if isinstance(a.get("slot"), int) and 0 <= a["slot"] < nslots:
                by_slot.setdefault(a["slot"], []).append(a)
            else:
                loose.append(a)
    return by_slot, loose


def _gap(P):
    return f'<div style="font-size:{P["gap"]}px;">&nbsp;</div>' if P["gap"] else ""


def _num_span(i, px):
    return jh_common.span(i + 1, COL_NUM, px, bold=True)


def _divider_row(ncols):
    """슬롯 사이 가는 구분선 (1세트/2세트 공통 한 곳)."""
    return f'<tr><td colspan="{1 + ncols}" style="background-color:{COL_DIVIDER}; font-size:{DIVIDER_PX}px;">&nbsp;</td></tr>'


def _slot_cell(sl, alt_list, thumb_path, P, Pc, col_avail):
    """한 슬롯의 한 열 칸: 옵션(`[썸][이름]`, 'A · B' 는 폭이 허용하는 만큼 한 줄, 아니면 줄바꿈 + `또는`), 메모, 그 슬롯의 대체.
    대체는 1세트·2세트 모두 `↳ 대체 카드 → [썸]Y`."""
    px, small, th = P["px"], P["small"], P["th"]
    opts = (sl or {}).get("options")
    its, tcell = _items(opts, thumb_path, th, px), th
    if len(its) > 1 and th and sum(w for _, _, w in its) + SEP_W * (len(its) - 1) > col_avail:
        small_th = round(th * 0.7)                               # 안 들어가면 소형 썸네일로 'A · B' 를 한 줄에 이어 붙여 본다
        its2 = _items(opts, thumb_path, small_th, px)
        if sum(w for _, _, w in its2) + SEP_W * (len(its2) - 1) <= col_avail:
            its, tcell = its2, small_th
    if its:
        parts = [_opts_table(its, Pc, col_avail, px, tcell)]
    else:
        parts = [jh_common.span("(비움)", COL_EMPTY, small)]
    if sl and sl.get("note"):
        parts.append(f'<span style="color:{COL_MUTED}; font-size:{small}px;">{_html.escape(sl["note"])}</span>')
    parts += [_alt_block(a, Pc, thumb_path, col_avail, attached=True, first=INDENT_TD, blank=INDENT_TD) for a in alt_list]
    return "".join(parts)


def _grid_table(columns, thumb_path, P):
    """슬롯 1~5 를 행으로, 세트(열)를 좌우로 놓는 하나의 격자. 1세트는 열이 1개인 격자로 같은 코드가 처리한다.
    - 번호 칸은 위쪽 정렬(첫 줄에 맞춤), 슬롯 사이에 같은 구분선, 같은 패딩/'또는' 표시
    - 열이 2개 이상일 때만 열 머리(▌퓨어탱커/▌공격탱커)
    - '그 밖의 대체'(대상 슬롯이 없는 대체)와 대체불가 안내는 아래에 압축
    columns: [{"label", "slots", "alternatives"}]"""
    px, small = P["px"], P["small"]
    ncols = len(columns)
    n = max(len(c.get("slots") or []) for c in columns)
    col_avail = (P["content_w"] - NUM_W - 14) / ncols - (14 if ncols > 1 else 0)
    if ncols == 1:
        col_avail = P["avail"]
    splits = [_split_alts(c.get("alternatives"), n) for c in columns]
    Pc = dict(P, pad=0)                                          # 칸 안 표는 촘촘하게(행 높이 절약)
    cw = f'{100 // ncols}%'
    rows = []
    if ncols > 1:
        head = "".join(f'<td width="{cw}" valign="middle"><span style="color:{COL_ALT}; font-size:{px + 1}px;"><b>▌{_html.escape(c.get("label") or "")}</b></span></td>'
                       for c in columns)
        rows.append(f'<tr>{BLANK_TD}{head}</tr>')
    for i in range(n):
        cells = ""
        for c, (by_slot, _) in zip(columns, splits):
            slots = c.get("slots") or []
            sl = slots[i] if i < len(slots) else None
            cells += f'<td width="{cw}" valign="top">{_slot_cell(sl, by_slot.get(i, []), thumb_path, P, Pc, col_avail)}</td>'
        rows.append(f'<tr>{_num_td(_num_span(i, px), "top")}{cells}</tr>')
        if i < n - 1:
            rows.append(_divider_row(ncols))
        elif P["gap"]:
            rows.append(f'<tr><td colspan="{1 + ncols}" style="font-size:{P["gap"]}px;">&nbsp;</td></tr>')
    out = f'<table cellspacing="0" cellpadding="{min(P["pad"], 1)}" width="100%">' + "".join(rows) + "</table>"
    extra = []
    for c, (_, loose) in zip(columns, splits):
        prefix = f"{c.get('label') or ''} · " if ncols > 1 else ""
        extra += [_alt_block(a, P, thumb_path, P["avail"], prefix=prefix, first=INDENT_TD, blank=INDENT_TD) for a in loose]
    if extra:
        out += jh_common.span("그 밖의 대체", COL_LOOSE_HEAD, small + 1, bold=True) + "".join(extra)
    return out


def stat_usable(matches):
    """스탯 우선순위 툴팁을 띄울 수 있는 식별 결과인가: 구원자 한 명, 신규(데이터 없음) 아님, 데이터 있음."""
    if not matches or len(matches) != 1:
        return False
    s = matches[0] or {}
    return bool(s.get("statPriority")) and not s.get("unknown")


def _stat_seq(groups, px):
    """[[스탯..]..] -> '힘 > 체력 = 인내 > ...' HTML (같은 묶음은 '=', 묶음 사이는 '>')"""
    seq = []
    for gi, group in enumerate(groups or []):
        if gi:
            seq.append(jh_common.span("&nbsp;&gt;&nbsp;", COL_MUTED, px))
        for si, stat in enumerate(group):
            if si:
                seq.append(jh_common.span("&nbsp;=&nbsp;", COL_MUTED, px))
            seq.append(jh_common.span(_html.escape(stat), jh_common.COL_WHITE, px, bold=True))
    return "".join(seq)


def stat_two_columns(savior):
    """퓨어탱커/공격력 탱커처럼 기준이 둘 이상이면 좌우로 나눠 보여 준다."""
    return len((savior or {}).get("statPriority") or []) > 1


def stat_geometry(rect, scale, two_columns):
    """-> (유효 배율, 툴팁 가로 px). 한 줄짜리는 기본 폭, 좌우 2열은 창 너비의 42%(이름 블록 오른쪽부터 인자 슬롯 위까지)."""
    eff, w, _ = tooltip_geometry(rect, scale, False)
    if two_columns:
        w = max(w, round(rect["width"] * STAT_WIDE_W_FRAC))
    return eff, w


def format_stat_html(savior, scale=1.0):
    """인자 고르기 화면용 여정 스탯 우선순위 툴팁 HTML (데이터가 없으면 ''). 기준이 둘(디펜더)이면 좌우로 나눈다."""
    rows = (savior or {}).get("statPriority") or []
    if not rows:
        return ""
    esc = _html.escape
    px, small = max(10, round(15 * scale)), max(9, round(12 * scale))
    head = (jh_common.span(f"📊 <b>{esc(savior.get('name') or '')}</b> 여정 스탯 우선순위", COL_ALT, px + 1) + "<br>"
            + jh_common.span(esc(jh_common.savior_meta(savior)), COL_MUTED, small))
    note = lambda r: ("<br>" + jh_common.span("└ " + esc(r["note"]), COL_HINT, small)) if r.get("note") else ""
    if len(rows) > 1:
        heads = [jh_common.span("▌" + esc(r.get("label") or ""), COL_ALT, px, bold=True) for r in rows]
        body = jh_common.grid_table([("", [_stat_seq(r.get("groups"), px) + note(r) for r in rows])], heads=heads)
    else:
        body = _stat_seq(rows[0].get("groups"), px) + note(rows[0])
    tail = (("<br>" + jh_common.span("※ " + esc(savior["statNote"]), COL_HINT, small)) if savior.get("statNote") else "")
    return head + jh_common.spacer(6) + body + tail + jh_common.spacer(6) + jh_common.source_html(small)


def format_reco_html(savior, scale=1.0, thumb_path=None, detail=0, content_w=None):
    """구원자 추천 아르카나 툴팁 HTML.
    thumb_path: arcanaId -> 캐시된 PNG 경로 함수(없으면 썸네일 없이 텍스트만).
    detail: 0 풀 레이아웃(썸네일 40px) / 1 썸네일 30px·간격 축소 / 2 글자 15->13px·썸네일 26px / 3 썸네일 제거(마지막 수단).
    content_w: 툴팁 안쪽 가로 폭(px). 넓은 모드(2세트)에서 호출 측이 넘긴다."""
    esc = _html.escape
    P = _profile(detail, scale, bool(thumb_path), content_w)
    px, small = P["px"], P["small"]
    name = esc(savior.get("name") or "")
    meta = jh_common.savior_meta(savior)
    if savior.get("unknown"):
        return (f'🃏 <b>{name}</b><br><span style="color:{COL_HINT}; font-size:{small}px;">'
                '아직 추천 아르카나 데이터가 없습니다 (신규 구원자이거나 데이터 갱신 전)</span>')
    one_line = detail >= 1 or bool(savior.get("sets"))          # 넓은 모드(2세트)는 머리 줄을 한 줄로
    head = (f'<span style="font-size:{px + 1}px;">🃏 <b>{name}</b> 추천 아르카나</span>' + (" " if one_line else "<br>")
            + jh_common.span(esc(meta), COL_MUTED, small))
    foot = jh_common.spacer(8) + jh_common.source_html(small)
    sets = savior.get("sets") or []
    if sets:
        return head + _grid_table(sets, thumb_path, P) + foot
    slots = savior.get("slots") or []
    if not any(slots):
        return head + f'<br>{jh_common.span("등록된 추천 아르카나가 없습니다 (미정)", COL_HINT, small)}'
    if all((not sl) or all(o.get("common") for o in sl.get("options") or []) for sl in slots):
        return head + (f'<br>{jh_common.span("특정 추천 카드 없음 - 공용 아르카나로 자유롭게 편성", COL_COMMON, px)}{foot}')
    return head + _grid_table([{"label": "", "slots": slots, "alternatives": savior.get("alternatives") or []}], thumb_path, P) + foot


def format_candidates_html(matches, scale=1.0, thumb_path=None, detail=0):
    """이름은 같은데 부제를 못 읽어 하나로 못 정한 경우: 후보를 모두 표시(속성·직업 병기)."""
    small = max(9, round(11 * scale))
    note = (f'<span style="color:{COL_WARN}; font-size:{small}px;">⚠️ 같은 이름 구원자가 {len(matches)}명입니다 '
            f'(부제를 읽지 못함) - 후보를 모두 표시합니다</span>')
    return "<hr>".join([note] + [format_reco_html(s, scale, thumb_path, min(detail + 1, MAX_DETAIL)) for s in matches])


def format_html(matches, scale=1.0, thumb_path=None, detail=0, content_w=None):
    if not matches:
        return ""
    try:
        if len(matches) == 1:
            return format_reco_html(matches[0], scale, thumb_path, detail, content_w)
        return format_candidates_html(matches, scale, thumb_path, detail)
    except Exception:
        return ""
