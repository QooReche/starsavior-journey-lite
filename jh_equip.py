# -*- coding: utf-8 -*-
"""구원자 '장비' 화면: PVE 추천 장비 세팅 툴팁 (GUI 의존성 없음).

구원자 화면(머리글 '구원자')에서 오른쪽 패널 제목이 '장비' 이고 아래 버튼('전체 해제'/'자동 착장')이 보일 때만 장비 화면으로 본다.
같은 화면의 부제·이름 줄로 구원자를 식별한다(좌표는 '여정 시작' 화면보다 위쪽). 데이터는 가이드 사이트 구원자 상세의 PVE 장비
(savior_reco.json 의 equipment) 이며, 없는 구원자는 아무것도 띄우지 않는다. 같은 이름이 여럿이라 하나로 못 정해도 띄우지 않는다.
"""
import html as _html
import re
import time

import jh_common
import jh_savior
import jh_seticons

# 클라이언트 영역 비율 (스크린샷 2장으로 보정: tests/test_equip.py). 머리글은 jh_savior 의 savior_header 영역을 그대로 쓴다.
EQUIP_REGIONS = {
    "equip_title": {"top": 0.125, "left": 0.745, "width": 0.060, "height": 0.040},         # 우측 패널 제목 '장비'
    "equip_btn_clear": {"top": 0.925, "left": 0.765, "width": 0.065, "height": 0.050},     # '전체 해제'
    "equip_btn_auto": {"top": 0.925, "left": 0.880, "width": 0.065, "height": 0.050},      # '자동 착장'
    "equip_subtitle": {"top": 0.153, "left": 0.097, "width": 0.170, "height": 0.033},      # 등급 줄 아래 부제/소속
    "equip_name": {"top": 0.187, "left": 0.097, "width": 0.170, "height": 0.040},          # 구원자 이름
}
HEADER_PHRASE = "구원자"
BUTTON_PHRASE = {"clear": "전체해제", "auto": ("자동착장", "자동장착")}     # OCR 이 '착장'을 '장착'으로 읽기도 한다
TITLE_SCALES = (1.5, 2.0, 2.5, 3.0)   # 이름 OCR 배율: 이 화면의 글꼴은 '웬' 같은 글자가 배율에 따라 오인식돼 넓게 모은다
CONFIRM = 2                      # 연속 판독 횟수(화면/구원자 확정)
LOST = 3                         # 연속으로 못 읽으면 해제
NOREAD_LOST = 4
SCAN_SEC = 1.0
TOOLTIP_TOP_FRAC = 0.20
WIDE_TOP_FRAC = 0.34            # 디펜더(퓨어탱커/공격탱커 좌우): 레벨·전투력 숫자(~0.32) 아래
WIDE_X_FRAC = 0.02
WIDE_W_FRAC = 0.50
ICON_H = 15                    # 세트 아이콘 높이(px, 배율 전)
TOOLTIP_X_FRAC = 0.30            # 좌우 화살표(0.26 / 0.59) 사이, 우측 장비 패널(0.74~) 왼쪽
TOOLTIP_MAX_W_FRAC = 0.28
BASE_W = 430

COL_LABEL, COL_VALUE, COL_NOTE, COL_HEAD = jh_common.COL_CYAN, jh_common.COL_WHITE, jh_common.COL_GRAY, jh_common.COL_GOLD
LABEL_W = 68                    # 항목 이름 칸 폭(px): '추천 세트' 가 두 줄로 꺾이지 않는 정도(글꼴이 달라도 넉넉하게)


# ---------------------------------------------------------------- 화면 판별
def _ko(text):
    return re.sub(r"[^가-힣]", "", text or "")


def match_header(text):
    """OCR 문자열이 구원자 화면 머리글 '구원자' 인가 (뒤에 붙는 '?' 아이콘 잡음은 허용)."""
    ko = _ko(text)
    return 3 <= len(ko) <= 5 and HEADER_PHRASE in ko


def match_title(text):
    """우측 패널 제목 '장비' ('I장비' 같은 앞 잡음 허용)."""
    ko = _ko(text)
    return 2 <= len(ko) <= 3 and "장비" in ko


def match_button(text, kind):
    ko = _ko(text)
    phrases = BUTTON_PHRASE[kind]
    phrases = (phrases,) if isinstance(phrases, str) else phrases
    return len(ko) >= 3 and any(p in ko or jh_savior._sim(ko[:4], p) >= 0.75 for p in phrases)


def screen_hit(title, buttons):
    """장비 화면 확정 조건: 제목 '장비' + 버튼 하나 이상."""
    return bool(title) and bool(buttons)


# ---------------------------------------------------------------- 상태기계
class EquipTracker:
    """장비 화면 확정/해제와 구원자 식별을 1초 간격 판독으로 이어 가는 상태기계(OCR/화면 접근은 주입).

    io: gray(name) -> 영역 그레이 이미지 / lines(gray, scales) -> [(text, conf)] / titles(gray) -> [후보 문자열]
    step(header) -> None 또는 ('savior', matches|None). matches 가 한 명일 때만 툴팁을 띄운다(호출 측 판단)."""

    def __init__(self, saviors, io, clock=time.monotonic):
        self.saviors, self.io, self.clock = saviors, io, clock
        self._next = 0.0
        self.reset()

    def reset(self):
        self.on = False
        self.cur = None                 # 확정한 구원자 키
        self.cand, self.cand_n = None, 0
        self.hit_n = self.miss_n = self.noread_n = 0
        self.quick = False
        self.gray = None
        self.last = None

    def recheck(self):
        """클릭 직후: 구원자 확정을 풀고 다음 판독 한 번으로 다시 확정한다(좌우 화살표로 구원자가 바뀔 수 있다)."""
        self.cur = None
        self.last = self.gray = None
        self.cand, self.cand_n = None, 0
        self.quick = True                  # 다음 식별은 한 번 읽히면 확정(화살표를 누른 직후라 구원자가 바뀐 것이 확실)
        self._next = 0.0

    def due(self):
        now = self.clock()
        if now < self._next:
            return False
        self._next = now + SCAN_SEC
        return True

    def _off(self):
        was = self.cur is not None
        self.reset()
        return ("savior", None) if was else None

    def step(self, header):
        text, conf = header
        if not (conf >= 0.4 and match_header(text)):
            self.miss_n += 1
            return self._off() if self.miss_n >= LOST else None
        gi = self.io.gray
        title = any(c >= 0.5 and match_title(t) for t, c in self.io.lines(gi("equip_title"), (2.0, 3.0)))
        buttons = {k for k, n in (("clear", "equip_btn_clear"), ("auto", "equip_btn_auto"))
                   if any(c >= 0.4 and match_button(t, k) for t, c in self.io.lines(gi(n), (2.0, 3.0)))}
        if not screen_hit(title, buttons):
            self.hit_n = 0
            self.miss_n += 1
            return self._off() if self.miss_n >= LOST else None
        self.miss_n = 0
        self.hit_n += 1
        if self.hit_n < CONFIRM and not self.on:
            return None
        self.on = True
        sub_g, name_g = gi("equip_subtitle"), gi("equip_name")
        last = self.gray
        same = (last is not None and last[0].shape == sub_g.shape and last[1].shape == name_g.shape
                and abs(_mean_diff(sub_g, last[0])) < 1.0 and abs(_mean_diff(name_g, last[1])) < 1.0)
        if not same or self.last is None:
            self.gray = (sub_g, name_g)
            self.last = jh_savior.identify(self.saviors, self.io.titles(sub_g), self.io.titles(name_g))
        matches, _how = self.last
        if not matches:
            self.noread_n += 1
            self.cand, self.cand_n = None, 0
            return self._off() if self.noread_n >= NOREAD_LOST else None
        self.noread_n = 0
        key = jh_savior.savior_key(matches)
        self.cand_n = self.cand_n + 1 if key == self.cand else 1
        self.cand = key
        if self.cand_n >= (1 if self.quick else CONFIRM) and key != self.cur:
            self.cur, self.quick = key, False
            return ("savior", list(matches))
        return None


def _mean_diff(a, b):
    import cv2
    return float(cv2.absdiff(a, b).mean())


# ---------------------------------------------------------------- 툴팁
def usable(matches):
    """툴팁을 띄울 수 있는 식별 결과인가: 구원자 한 명, 신규(데이터 없음) 아님, 장비 데이터 있음."""
    if not matches or len(matches) != 1:
        return False
    s = matches[0] or {}
    return bool(s.get("equipment")) and not s.get("unknown")


def _tank_of(note):
    """항목 메모로 운영 방식을 가른다: 'attack' 공격탱커 / 'pure' 퓨어탱커 / 'ex' EX 전용 / None 알 수 없음."""
    n = note or ""
    if "공격탱커" in n:
        return "attack"
    if "퓨어탱커" in n:
        return "pure"
    return "ex" if "EX" in n else None


def split_by_tank(equipment):
    """디펜더 장비 -> {'necklace'|'ring'|'sets': (퓨어 항목들, 공격 항목들, EX 전용 항목들)}. 가를 수 없는 항목이 있으면 None(한 줄 목록으로 표시)."""
    out = {}
    for key in ("necklace", "ring", "sets"):
        groups = {"pure": [], "attack": [], "ex": []}
        for o in (equipment or {}).get(key) or []:
            kind = _tank_of(o.get("note"))
            if kind is None:
                return None
            groups[kind].append(o)
        if not (groups["pure"] or groups["attack"]):
            return None
        out[key] = (groups["pure"], groups["attack"], groups["ex"])
    return out


def two_columns(savior):
    """퓨어탱커/공격탱커를 좌우로 보여 줄 수 있는 디펜더인가."""
    return bool((savior or {}).get("sets")) and split_by_tank((savior or {}).get("equipment")) is not None


def wide(matches):
    return usable(matches) and two_columns(matches[0])


def geometry(rect, scale, is_wide=False):
    """-> (유효 배율, 툴팁 가로 px, 안쪽 폭 px). 좌우 2열은 창 너비의 절반."""
    eff = scale * jh_savior.window_k(rect["height"])
    if is_wide:
        w = round(rect["width"] * WIDE_W_FRAC)
    else:
        w = max(220, min(round(BASE_W * eff), int(TOOLTIP_MAX_W_FRAC * rect["width"])))
    return eff, w, w - 36


def _set_html(value, px, icon_path, th):
    """'공격(4) + 투지(2)' -> 세트 아이콘 + 글자 (아이콘이 없으면 글자만)"""
    esc = _html.escape
    parts = jh_seticons.split_set(value)
    if not parts:
        return jh_common.span(esc(value), COL_VALUE, px, bold=True)
    out = []
    for i, (name, count, suffix) in enumerate(parts):
        if i:
            out.append(jh_common.span("&nbsp;+&nbsp;", COL_NOTE, px))
        icon = jh_common.img_tag(icon_path(name), th) if (icon_path and name in jh_seticons.SET_NAMES) else ""
        text = f"{name}({count})" + (f" {suffix}" if suffix else "")
        out.append(icon + ("&nbsp;" if icon else "") + jh_common.span(esc(text), COL_VALUE, px, bold=True))
    return "".join(out)


def _item_html(item, px, small, is_set, icon_path, th):
    value = _set_html(item["value"], px, icon_path, th) if is_set else jh_common.span(_html.escape(item["value"]), COL_VALUE, px, bold=True)
    note = item.get("note")
    return value + (("<br>" + jh_common.span("&nbsp;&nbsp;" + _html.escape(note), COL_NOTE, small)) if note else "")


def _potential_html(equipment, px, small):
    lines = [x.strip() for x in (equipment.get("potential") or "").split("\n") if x.strip()]
    if not lines:
        return ""
    return "<br>".join(jh_common.span(_html.escape(x), COL_NOTE if x.startswith("※") else COL_VALUE, small if x.startswith("※") else px,
                                      bold=not x.startswith("※")) for x in lines)


def format_html(savior, scale=1.0, detail=0, icon_path=None):
    """장비 추천 툴팁 HTML (장비 데이터가 없으면 ''). detail 0 보통 / 1 한 단계 작게. icon_path: 세트 이름 -> 캐시된 PNG 경로 함수."""
    eq = (savior or {}).get("equipment")
    if not eq:
        return ""
    esc = _html.escape
    px = max(10, round((14 if detail == 0 else 13) * scale))
    small = max(9, round((12 if detail == 0 else 11) * scale))
    th = max(10, round((ICON_H if detail == 0 else ICON_H - 2) * scale)) if icon_path else 0
    head = (jh_common.span(f"🛡️ <b>{esc(savior.get('name') or '')}</b> PVE 추천 장비", COL_HEAD, px + 1) + "<br>"
            + jh_common.span(esc(jh_common.savior_meta(savior, "작전·회랑·코스모 게이트 기준")), COL_NOTE, small))
    label = lambda t: jh_common.span(esc(t), COL_LABEL, small, bold=True)
    cat = (("목걸이", "necklace", False), ("반지", "ring", False), ("추천 세트", "sets", True))
    heads, rows = None, []
    groups = split_by_tank(eq) if savior.get("sets") else None
    if groups:                                                       # 디펜더: 퓨어탱커 | 공격력 탱커 좌우, EX 전용은 아래 줄
        heads = [jh_common.span("▌퓨어탱커", jh_common.COL_GOLD, px, bold=True), jh_common.span("▌공격력 탱커", jh_common.COL_GOLD, px, bold=True)]
        for title, key, is_set in cat:
            pure, attack, ex = groups[key]
            cell = lambda items: "<br>".join(_item_html(o, px, small, is_set, icon_path, th) for o in items) or jh_common.span("-", COL_NOTE, px)
            rows.append((label(title), [cell(pure), cell(attack)]))
            if ex:
                rows.append(("", [jh_common.span("EX&nbsp;&nbsp;", COL_LABEL, small, bold=True) + cell(ex)]))
        if eq.get("setNotes"):
            rows.append(("", ["<br>".join(jh_common.span(esc(n), COL_NOTE, small) for n in eq["setNotes"])]))
    else:
        for title, key, is_set in cat:
            items = eq.get(key) or []
            if items:
                body = "<br>".join(_item_html(o, px, small, is_set, icon_path, th) for o in items)
                if key == "sets" and eq.get("setNotes"):
                    body += "".join("<br>" + jh_common.span(esc(n), COL_NOTE, small) for n in eq["setNotes"])
                rows.append((label(title), [body]))
    if eq.get("potential"):
        rows.append((label("잠재력"), [_potential_html(eq, px, small)]))
    return head + jh_common.spacer(6) + jh_common.grid_table(rows, LABEL_W, heads, pad=3) + jh_common.spacer(6) + jh_common.source_html(small)


def format_matches_html(matches, scale=1.0, detail=0, icon_path=None):
    return format_html(matches[0], scale, detail, icon_path) if usable(matches) else ""
