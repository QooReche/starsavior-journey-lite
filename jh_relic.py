# -*- coding: utf-8 -*-
"""'훈련의 방향성' 이벤트: 선택지별로 나중에 얻는 특수 잠재력(AX, CX ...) 안내 + 구원자별 추천 표시 (GUI/OCR 의존성 없음)

선택지 -> 고대 구원자의 유물(AX~XX) 연결은 DB 에 없다. 아카라이브 정리글(2026-09-26 '베로롱과 함께하는 AX~XX / 1호~8호 간단정리')의
표와 V2 DB 의 '고대 구원자의 유물' 이벤트 보상 후보(5370001~12)가 일치해서 직접 적었다. 선택지 이름이 정확히 같을 때만 표시한다.
구원자 추천 문구는 가이드 사이트 구원자 상세의 PVE 장비 '잠재력' 칸(savior_reco.json 의 potential)을 그대로 쓴다.
"""
import html as _html
import re

try:
    from jh_savior import COL_DIVIDER as _COL_DIVIDER      # 구분선 색은 구원자 툴팁과 한 곳(공용 스타일)
except Exception:
    _COL_DIVIDER = "#26323a"
CODE_W = 34                                                 # 코드 칸 폭(px): XX1 이 한 줄에 들어가는 정도

# 선택지 이름 -> [(코드, 효과 요약)]. 번호가 붙는 AX1~8 등은 '의뢰토벌 증거품 n호'로 정해지므로 여기서는 알파벳만 쓴다.
ROUTES = {
    "공격에 도움이 되는 응원": [
        ("AX", "턴 시작 시 공격력 8% 증가(최대 5중첩), 궁극기 사용 후 초기화"),
        ("EX", "치명타 확률 10% 증가, 기본기 치명타 시 25% 확률로 무작위 아군 기본기 발동"),
        ("FX", "치명타 피해 10% 증가, 치명타 발생 시 다음 타격 피해량 25% 증가")],
    "생존에 도움이 되는 응원": [
        ("BX", "최대 생명력 8% 증가, 피격 후 25% 확률로 최대 생명력 비례 회복"),
        ("CX", "방어력 12% 증가, 피격 시 방어력 10% 증가(최대 5중첩), 타격 후 초기화"),
        ("DX", "속도 8 증가, 기본기 사용 후 25% 확률로 행동 게이지 10% 증가(1턴 1회)")],
    "변수 대처에 도움이 되는 응원": [
        ("GX", "효과 적중 16% 증가, 기본기 타격 후 50% 확률로 2연 출현"),
        ("HX", "효과 저항 16% 증가, 단일 공격 피격 시 50% 확률로 타격자에게 생명력 비례 피해")],
    "전술 운용에 도움이 되는 응원": [
        ("XX1", "반격 확률 20% 증가"),
        ("XX2", "생명력 흡수율 15% 증가"),
        ("XX3", "강인도 최대치 3 증가")],
}

TIP = "💡 이후 의뢰토벌의 증거품 n호 선택지에서 원하는 1개만 챙기고 나머지는 버리면 그 호수가 확정됩니다"
HO_LEGEND = "1호 공격력4% · 2호 생명력4% · 3호 방어력6% · 4호 속도4 · 5호 치명확률5% · 6호 치명피해5% · 7호 효과적중8% · 8호 효과저항8%"

_CODE_RE = re.compile(r"(?<![A-Za-z])(XX\d?|[A-I]X)\d?(?![A-Za-z])")
_savior = None          # 직전에 식별한 구원자 한 명(동명이인이라 모호하면 None)


def set_savior(matches):
    """구원자 식별 결과(리스트) 기억. 한 명으로 정해질 때만 추천에 쓴다. 빈 값이면 유지(편성 화면을 벗어나도 그 여정의 구원자)."""
    global _savior
    if matches:
        _savior = matches[0] if len(matches) == 1 else None


def clear_savior():
    """아르카나 편성 화면에 (다시) 들어갈 때 호출: 이전 여정의 구원자를 비운다. 이번에 식별이 안 되면 추천 줄을 생략한다(틀린 추천보다 낫다)."""
    global _savior
    _savior = None


def route_for(option):
    return ROUTES.get(re.sub(r"[\.\s]+$", "", (option or {}).get("choice", "")))


def _key(token):
    """AX1 -> AX (뒤 번호는 증거품 호수), XX2 는 그대로(세 개가 서로 다른 효과), 번호 없는 XX 는 XX 전체"""
    return token if token.startswith("XX") and len(token) == 3 else token[:2]


def recommend_lines(route, savior):
    """구원자 추천 문구 중 이 선택지의 코드가 나오는 줄 (없으면 [])"""
    text = (savior or {}).get("potential") or ""
    mine = {_key(c) for c, _ in route}
    out = []
    for line in [x.strip() for x in text.split("\n") if x.strip()]:
        keys = {_key(m.group(0)) for m in _CODE_RE.finditer(line)}
        if keys & mine or ("XX" in keys and any(k.startswith("XX") for k in mine)):
            out.append(line)
    return out


def _table(rows, small, col_head, col_body):
    """[(코드, 설명)] -> 코드 칸 + 설명 칸 표. 설명이 길어 줄바꿈돼도 코드 칸 아래로 말려 들어가지 않고, 행 사이에 구분선(구원자 툴팁과 같은 색)."""
    esc = _html.escape
    div = (f'<tr><td colspan="2" style="background-color:{_COL_DIVIDER}; font-size:1px;">&nbsp;</td></tr>')
    trs = []
    for i, (c, d) in enumerate(rows):
        head = f'<span style="color:{col_head}; font-size:{small}px;"><b>{esc(c)}</b></span>' if c else ""
        trs.append(f'<tr><td valign="top" width="{CODE_W if c else 1}">{head}</td>'
                   f'<td valign="top"><span style="color:{col_body}; font-size:{small}px;">{esc(d)}</span></td></tr>')
        if i < len(rows) - 1:
            trs.append(div)
    return '<table cellspacing="0" cellpadding="2" width="100%">' + "".join(trs) + "</table>"


def section(option, scale=1.0, detail=0, savior=None):
    """선택지 툴팁 끝에 붙일 HTML (해당 선택지가 아니면 빈 문자열)"""
    route = route_for(option)
    if not route:
        return ""
    esc = _html.escape
    small = max(9, round(12 * scale))
    hr = '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">'
    codes = " · ".join(c for c, _ in route)
    out = hr + (f'<span style="color:#ffd27f; font-size:{small}px;"><b>얻을 수 있는 특수 잠재력</b></span>'
                f' <span style="color:#ffffff; font-size:{max(10, round(14 * scale))}px;"><b>{esc(codes)}</b></span>')
    if detail >= 1:
        return out
    out += _table([(c, d) for c, d in route], small, "#9fe8ff", "#cfd8dc")
    sv = savior if savior is not None else _savior
    rec = recommend_lines(route, sv) if sv else []
    if rec:
        who = f'{sv.get("name", "")}({sv.get("subtitle", "")})' if sv.get("subtitle") else sv.get("name", "")
        out += (hr + f'<span style="color:#a5d6a7; font-size:{small}px;"><b>⭐ {esc(who)} 추천</b></span>'
                + _table([("", x) for x in rec], small, "#a5d6a7", "#e8f5e9")
                + f'<br><span style="color:#90a4ae; font-size:{max(9, small - 1)}px;">{esc(HO_LEGEND)}</span>')
    out += hr + f'<span style="color:#ffd27f; font-size:{small}px;">{esc(TIP)}</span>'
    return out
