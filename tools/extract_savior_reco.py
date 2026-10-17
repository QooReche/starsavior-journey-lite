# -*- coding: utf-8 -*-
"""구원자 추천 아르카나 슬림 JSON 추출기 (오프라인 재현 가능).

입력: 가이드 사이트 사본(app-core.js, app.js, arcanas.json) -> 출력: src/data/savior_reco.json
사용: python -X utf8 tools/extract_savior_reco.py <사이트사본폴더> [--out src/data/savior_reco.json]
      (사이트 재다운로드는 하지 않는다. 다시 받을 때는 docs/arcana_setting_research_2026-10-16.md 의 UA·요청 최소화 방식을 따른다.)

사이트가 구원자 상세 화면(createDetailMarkup)에서 실제로 보여 주는 '최종' PVE 5슬롯 + 대체 슬롯을 그대로 재현한다.
SAVIORS 하드코딩 값을 그대로 쓰면 틀리므로, 사이트의 아래 변환을 같은 순서로 포팅했다 (app-core.js 줄 번호는 2026-10-17 사본 기준):
  1. 로드 시 변환: gwen 상세 교체, 스트라이커 9명(STRIKER_ARCANA_TARGET_IDS)의 pve/alternatives 를 createStriker* 로 덮어쓰기,
     clarissa 덮어쓰기                                               (5857~5920)
  2. app.js 로드 후 패치: roberta, lydia-moon-caster, lydia 덮어쓰기 + buildAlternativeArcana 래핑(lydia 계열은 원본 대체 그대로)
  3. getBuild: 값이 없으면 미정(PENDING) 기본값                      (6004)
  4. createDetailMarkup (7875):
       - 슬롯에 '공용 아르카나' 가 하나라도 있으면 COMMON_ARCANA_SLOTS 로 통째 교체
       - STRIKER_ARCANA_SWAP_IDS(9명)는 슬롯 이름의 '완벽한 바니걸' -> '음독의 각오'
       - buildAlternativeArcana (7714): 대체 목록 재구성(전용 구원자/공통 자동 대체/스트라이커 교체)
       - addEternalBondAlternativeForWhiteMoon (7841): '영원 속박의 굴레' 대체 덧붙임
       - 디펜더(white-pearl-luna 제외)는 구원자별 값이 아니라 DEFENDER_PURE_TANK/ATTACK_TANK 프리셋 2세트를 보여 줌 (8418)
  5. 이름 -> 카드: ARCANA_LIBRARY(약칭 맵) + arcanas.json 하이드레이트(1031) + normalizeArcanaAlias/or 분할 (8221~8249)
이 포팅이 사이트와 같은지는 tools/site_logic_check.js (node 로 사이트 JS 를 실제 실행) 결과와 비교해 확인한다(tests/test_savior.py).
"""
import json
import os
import re
import sys

SCHEMA_VERSION = 2
COMMON = "공용 아르카나"
# 포팅한 app.js/app-core.js 패치 대상. 이 밖의 구원자를 사이트가 따로 패치하면 경고한다(포팅 누락 감지).
PORTED_PATCH_IDS = {"gwen", "clarissa", "roberta", "lydia", "lydia-moon-caster"}

# '노 페인' 처럼 note 가 비어 있을 때, 사이트의 공통 자동 대체 규칙(buildAlternativeArcana)이 쓰는 대체 대상 (표시용 추정)
DEFAULT_TARGETS = {"노 페인, 노 게인": "단점 보완 맞춤 훈련", "메이드 바이 페트라♡": "꽃들에게 죽음을", "별을 보며 꿈을": "꽃들에게 죽음을",
                   "어느 한 기사의 맹세": "하얀 달의 온기는 햇빛처럼", "완벽한 바니걸": "음독의 각오"}


# ---------------------------------------------------------------- JS 리터럴 도우미
def _balanced(text, start, open_ch="[", close_ch="]"):
    """text[start] == open_ch 에서 시작해 짝이 맞는 닫는 괄호 위치. 문자열 안의 괄호는 무시."""
    depth, i, n, in_str, esc = 0, start, len(text), None, False
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        elif c in "\"'`":
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("괄호 짝을 찾지 못함")


def js_to_json(text):
    """객체/배열 리터럴(키에 따옴표 없음, 후행 쉼표 허용) -> Python 값. 문자열 안은 건드리지 않는다."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word, k = text[i:j], j
            while k < n and text[k] in " \t\r\n":
                k += 1
            out.append(f'"{word}"' if k < n and text[k] == ":" else word)
            i = j
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[\]}])", r"\1", "".join(out)))


def _literal_after(core_js, decl_regex, open_ch, close_ch):
    m = re.search(decl_regex, core_js)
    if not m:
        raise ValueError(f"선언을 찾지 못함: {decl_regex}")
    s = core_js.index(open_ch, m.end() - 1) if core_js[m.end() - 1] != open_ch else m.end() - 1
    return js_to_json(core_js[s:_balanced(core_js, s, open_ch, close_ch) + 1])


def parse_savior_block(core_js):
    """app-core.js 문자열 -> SAVIORS 리스트 (PENDING_ARCANA() 치환)."""
    m = re.search(r"const\s+SAVIORS\s*=\s*\[", core_js)
    if not m:
        raise ValueError("SAVIORS 선언을 찾지 못함")
    start = m.end() - 1
    block = core_js[start:_balanced(core_js, start) + 1]
    pend = [{"name": "미정", "note": "미정"}] * 5
    return json.loads(re.sub(r"PENDING_ARCANA\(\)", json.dumps(pend, ensure_ascii=False), block))


def parse_site_constants(core_js):
    return {
        "common": _literal_after(core_js, r"const\s+COMMON_ARCANA_SLOTS\s*=\s*\[", "[", "]"),
        "defender_pure": _literal_after(core_js, r"const\s+DEFENDER_PURE_TANK_ARCANA\s*=\s*Object\.freeze\(\s*\{", "{", "}"),
        "defender_attack": _literal_after(core_js, r"const\s+DEFENDER_ATTACK_TANK_ARCANA\s*=\s*Object\.freeze\(\s*\{", "{", "}"),
        "swap_ids": set(_literal_after(core_js, r"const\s+STRIKER_ARCANA_SWAP_IDS\s*=\s*new\s+Set\(\s*\[", "[", "]")),
        "target_ids": set(_literal_after(core_js, r"const\s+STRIKER_ARCANA_TARGET_IDS\s*=\s*new\s+Set\(\s*\[", "[", "]")),
    }


# ---------------------------------------------------------------- 카드 라이브러리 (ARCANA_LIBRARY + 하이드레이트)
def parse_detail_ids(core_js):
    m = re.search(r"const\s+ARCANA_DETAIL_IDS\s*=\s*\{", core_js)
    if not m:
        return {}
    s = m.end() - 1
    return {k: int(v) for k, v in re.findall(r"\"([^\"]+)\"\s*:\s*(\d+)", core_js[s:_balanced(core_js, s, "{", "}") + 1])}


def parse_arcana_library(core_js):
    """사이트 로드 직후의 ARCANA_LIBRARY 를 {별칭: [카드 정식명...]} 로 재현한다 (이미지/ID 는 무시)."""
    m = re.search(r"const\s+ARCANA_LIBRARY\s*=\s*\{", core_js)
    if not m:
        return {}
    s = m.end() - 1
    e = _balanced(core_js, s, "{", "}")
    body = core_js[s:e + 1]
    lib = {}
    for km in re.finditer(r"^\s{2}\"([^\"]+)\"\s*:\s*\[", body, re.M):
        a = km.end() - 1
        lib[km.group(1)] = re.findall(r"name\s*:\s*\"([^\"]+)\"", body[a:_balanced(body, a) + 1])
    rest = core_js[e:]
    # 파일 순서대로: 별칭 대입 / 단일 카드 대입 / '카드명 자동 별칭' 루프
    events = []
    for am in re.finditer(r"^ARCANA_LIBRARY\[\"([^\"]+)\"\]\s*=\s*ARCANA_LIBRARY\[\"([^\"]+)\"\];", rest, re.M):
        events.append((am.start(), "alias", am.group(1), am.group(2)))
    for cm in re.finditer(r"^ARCANA_LIBRARY\[\"([^\"]+)\"\]\s*=\s*\[\{\s*name:\s*\"([^\"]+)\"", rest, re.M):
        events.append((cm.start(), "card", cm.group(1), cm.group(2)))
    lm = re.search(r"Object\.values\(ARCANA_LIBRARY\)\.flat\(\)\.forEach\(\(card\) => \{\s*if \(!ARCANA_LIBRARY\[card\.name\]\)", rest)
    if lm:
        events.append((lm.start(), "auto", "", ""))
    for _, kind, a, b in sorted(events):
        if kind == "alias":
            if b in lib:
                lib[a] = lib[b]
        elif kind == "card":
            lib[a] = [b]
        else:
            for names in list(lib.values()):
                for nm in names:
                    lib.setdefault(nm, [nm])
    for am in re.finditer(r"if \(!ARCANA_LIBRARY\[\"([^\"]+)\"\]\)\s*\{\s*ARCANA_LIBRARY\[\"[^\"]+\"\]\s*=\s*\[\s*\{\s*name:\s*\"([^\"]+)\"", rest):
        lib.setdefault(am.group(1), [am.group(2)])
    return lib


def _normalize_alias(v):
    v = re.sub(r"^\*+", "", v or "")
    v = re.sub(r"^통찰\(4\)\s*:\s*", "", v)
    v = re.sub(r"\s*택\s*1\s*", "", v)
    v = v.replace("(추천)", "")
    v = re.sub(r"\s*\(\s*", "(", v)
    v = re.sub(r"\s*\)\s*", ")", v)
    return v.strip()


class Resolver:
    """사이트 resolveArcanaChoices 의 포팅. 사이트는 해석 못 한 별칭을 조용히 버리지만, 여기서는 이름만이라도 남긴다(unresolved)."""

    def __init__(self, arcanas, detail_ids, library, common_slots):
        self.ids = dict(detail_ids)
        self.lib = {k: list(v) for k, v in library.items()}
        for a in arcanas:                                   # hydrateArcanaRecommendationLibrary
            ko = (a.get("name") or {}).get("ko") if isinstance(a.get("name"), dict) else a.get("name")
            if ko and a.get("id") is not None:
                self.ids[ko] = int(a["id"])
                if not self.lib.get(ko):
                    self.lib[ko] = [ko]
        self.common_slots = common_slots
        self.stats = {"exact": 0, "alias": 0, "unresolved": 0}
        self.unresolved = []

    def choices(self, raw):
        """사이트와 같은 카드 이름 리스트 (해석된 것만)."""
        n = _normalize_alias(raw)
        if not n:
            return []
        if COMMON in n:
            return [c for s in self.common_slots for c in self.lib.get(s["name"], [])]
        return [c for al in (_normalize_alias(x) for x in re.split(r"\s*or\s*", n, flags=re.I)) if al for c in self.lib.get(al, [])]

    def names(self, slots):
        return [c for s in slots or [] if s for c in self.choices(s.get("name"))]

    def options(self, raw, count=True):
        """슬롯/대체 이름 문자열 -> 옵션 리스트 [{name, arcanaId}] (미해결은 arcanaId None + unresolved)."""
        n = _normalize_alias(raw)
        if not n:
            return []
        if COMMON in n:
            return [self._opt(c) for c in self.choices(n)]
        out = []
        for al in (_normalize_alias(x) for x in re.split(r"\s*or\s*", n, flags=re.I)):
            if not al:
                continue
            cards = self.lib.get(al)
            if not cards:
                o = {"name": al, "arcanaId": None, "unresolved": True}
                if count:
                    self.stats["unresolved"] += 1
                    self.unresolved.append(al)
                out.append(o)
                continue
            if count:
                self.stats["exact" if cards == [al] else "alias"] += 1
            out.extend(self._opt(c, al if cards != [al] and len(cards) > 1 else None) for c in cards)
        return out

    def _opt(self, card, alias=None):
        o = {"name": card, "arcanaId": self.ids.get(card)}
        if alias:
            o["alias"] = alias
        return o


# ---------------------------------------------------------------- 사이트 변환 포팅
def _pend():
    return [{"name": "미정", "note": "미정"} for _ in range(5)]


def striker_recommended(sid=""):
    return [{"name": "단점 보완 맞춤 훈련", "note": ""}, {"name": "불굴의 역작", "note": ""}, {"name": "꽃들에게 죽음을", "note": ""},
            {"name": "음독의 각오", "note": ""},
            {"name": "자칭 베테랑 탐정의 수사법 or 허수의 개척자" if sid == "gwen" else "자칭 베테랑 탐정의 수사법", "note": ""}]


def striker_alternatives():
    return [{"name": "노 페인, 노 게인", "note": ""}, {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을", "note": ""},
            {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세", "note": "부족한 자리 대체"}, {"name": "완벽한 바니걸", "note": ""}, None]


def _pad5(lst):
    lst = lst or []
    return [(lst[i] if i < len(lst) and lst[i] else None) for i in range(5)]


def apply_load_time_patches(saviors, consts):
    """app-core.js 5857~5920 + app.js 로드 후 패치. saviors 를 제자리에서 바꾼다."""
    by_id = {s.get("id"): s for s in saviors}
    g = by_id.get("gwen")
    if g is not None:
        g["detail"] = {"arcana": {"pve": striker_recommended("gwen"), "pvp": _pend(), "alternatives": striker_alternatives()}}
    for s in saviors:
        if s.get("id") in consts["target_ids"] and s.get("detail"):
            arc = s["detail"].setdefault("arcana", {"pve": [], "pvp": _pend(), "alternatives": []})
            arc["pve"], arc["alternatives"] = striker_recommended(s["id"]), striker_alternatives()
    c = by_id.get("clarissa")
    if c is not None and (c.get("detail") or {}).get("arcana"):
        c["detail"]["arcana"]["pve"] = [{"name": n, "note": ""} for n in
                                        ("단점 보완 맞춤 훈련", "불굴의 역작", "꽃들에게 죽음을", "완벽한 바니걸", "탐닉자의 유흥거리")]
        c["detail"]["arcana"]["alternatives"] = [
            {"name": "노 페인, 노 게인", "note": "단점 보완 맞춤 훈련 대체"},
            {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을", "note": "꽃들에게 죽음을 대체"},
            {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세", "note": "탐닉자의 유흥거리 대체"},
            {"name": "영원 속박의 굴레", "note": "하얀 달의 온기는 햇빛처럼 대체"}, None]
    # --- app.js ---
    r = by_id.get("roberta")
    if r is not None and (r.get("detail") or {}).get("arcana"):
        r["detail"]["arcana"]["pve"] = [
            {"name": "단점 보완 맞춤 훈련", "note": ""}, {"name": "허수의 개척자 or 불굴의 역작", "note": ""},
            {"name": "꽃들에게 죽음을", "note": ""}, {"name": "서류 더미 위의 책임감", "note": ""},
            {"name": "음독의 각오 or 자칭 베테랑 탐정의 수사법", "note": ""}]
        r["detail"]["arcana"]["alternatives"] = [
            {"name": "본 투 비 와일드", "note": "음독의 각오/자칭 베테랑 탐정의 수사법 대체"},
            {"name": "노 페인, 노 게인", "note": "단점 보완 맞춤 훈련 대체"},
            {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을", "note": "꽃들에게 죽음을 대체"},
            {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세 or 완벽한 바니걸", "note": "부족한 자리 대체"}, None]
    lm = by_id.get("lydia-moon-caster")
    if lm is not None:
        lm["summary"] = "PVE 장비·아르카나 세팅을 반영했습니다."
        arc = dict((lm.get("detail") or {}).get("arcana") or {})
        arc["pve"] = [{"name": n, "note": ""} for n in ("단점 보완 맞춤 훈련", "죽음이 둘을 갈라놓을 때까지", "이게 옷이라고요?", "불굴의 역작", "꽃들에게 죽음을")]
        arc["alternatives"] = [
            {"name": "하늘의 심판", "note": "불굴의 역작 대체"},
            {"name": "노 페인, 노 게인", "note": "단점 보완 맞춤 훈련 대체"},
            {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을", "note": "꽃들에게 죽음을 대체"},
            {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세", "note": "부족한 자리 대체"}, None]
        lm["detail"] = {**(lm.get("detail") or {}), "arcana": arc}
    wl = by_id.get("lydia")
    if wl is not None and (wl.get("detail") or {}).get("arcana"):
        arc = wl["detail"]["arcana"]
        arc["pve"] = _pad5(arc.get("pve"))
        arc["pve"][4] = {"name": "이게 옷이라고요?", "note": ""}
        alts = _pad5(arc.get("alternatives"))
        alts[0] = {"name": "노 페인, 노 게인", "note": "단점 보완 맞춤 훈련 대체"}
        alts[1] = {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을 or 깊은 애도", "note": "꽃들에게 죽음을 대체"}
        alts[2] = {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세 or 영원 속박의 굴레", "note": "부족한 자리 대체"}
        alts[3] = None
        arc["alternatives"] = alts


def unported_patches(core_js, app_js):
    """사이트가 SAVIORS.find("id") 로 따로 만지는 구원자 중 이 포팅에 없는 것 (사이트 개편 감지용 경고)."""
    ids = set(re.findall(r"SAVIORS\.find\(\(\w+\)\s*=>\s*\w+\.id\s*===\s*\"([^\"]+)\"\)", (core_js or "") + (app_js or "")))
    ids |= set(re.findall(r"savior\??\.id\s*===\s*\"(lydia[^\"]*)\"", app_js or ""))
    return sorted(ids - PORTED_PATCH_IDS)


def build_alternative_arcana(savior, pve, existing, R, consts):
    """사이트 buildAlternativeArcana (app.js 가 lydia 계열만 감싼 것 포함). 항목은 {name, note} 또는 None 인 리스트."""
    sid = savior.get("id")
    if sid in ("lydia", "lydia-moon-caster") or sid in consts["target_ids"] or sid == "clarissa":
        return _pad5(existing)
    result, seen = [], set()

    def add(name, note=""):
        key = f"{name}::{note}"
        if not name or key in seen:
            return
        seen.add(key)
        result.append({"name": name, "note": note})

    swap = sid in consts["swap_ids"]
    for slot in existing or []:
        if not slot or not slot.get("name"):
            continue
        if swap and "음독의 각오" in str(slot["name"]):
            add(str(slot["name"]).replace("음독의 각오", "완벽한 바니걸"), "음독의 각오 대체")
            continue
        add(slot["name"], slot.get("note") or "")
    names = set(R.names(pve))
    if sid == "cristelle":
        return [{"name": "어느 한 기사의 맹세", "note": "하얀 달의 온기는 햇빛처럼/완벽한 바니걸 대체"},
                {"name": "노 페인, 노 게인", "note": "단점 맞춤 훈련 대체"},
                {"name": "누각 위, 유리달 맞이", "note": "영원 속박의 굴레 대체"}, None, None]
    if sid in ("white-pearl-luna", "luna", "professor-m", "waltz-asherah"):
        return result
    if sid == "smile":
        return [{"name": "노 페인, 노 게인", "note": "단점 보완 맞춤 훈련 대체"} if "단점 보완 맞춤 훈련" in names else None,
                {"name": "메이드 바이 페트라♡ or 별을 보며 꿈을", "note": "꽃들에게 죽음을 대체"} if "꽃들에게 죽음을" in names else None,
                {"name": "하얀 달의 온기는 햇빛처럼 or 어느 한 기사의 맹세 or 완벽한 바니걸", "note": "종말은 소녀의 얼굴을 하고 있다. 대체"},
                {"name": "불굴의 역작", "note": "허수의 개척자 대체"}, None]
    if sid == "roberta":
        if "단점 보완 맞춤 훈련" in names:
            add("노 페인, 노 게인", "단점 보완 맞춤 훈련 대체")
        if "꽃들에게 죽음을" in names:
            add("메이드 바이 페트라♡ or 별을 보며 꿈을", "꽃들에게 죽음을 대체")
        return result
    if "단점 보완 맞춤 훈련" in names:
        add("노 페인, 노 게인", "단점 보완 맞춤 훈련 대체")
    if "꽃들에게 죽음을" in names:
        add("메이드 바이 페트라♡ or 별을 보며 꿈을", "꽃들에게 죽음을 대체")
    if "하얀 달의 온기는 햇빛처럼" in names:
        add("어느 한 기사의 맹세", "하얀 달의 온기는 햇빛처럼 대체")
    if swap:
        add("완벽한 바니걸", "음독의 각오 대체")
    if savior.get("className") == "서포터":
        add("누구보다 프로페셔널", "공녀, 왕좌에 오르다 대체")
    if "본 투 비 와일드" in names:
        add("언더커버 캅", "본 투 비 와일드 대체")
    return result


def add_eternal_bond(savior, pve, alts, R):
    """사이트 addEternalBondAlternativeForWhiteMoon."""
    result = list(alts or [])
    if savior.get("id") in ("cristelle", "clarissa") or savior.get("className") in ("서포터", "디펜더"):
        return result
    if "하얀 달의 온기는 햇빛처럼" not in set(R.names(pve)):
        return result
    if "영원 속박의 굴레" in set(R.names(result)):
        return result
    entry = {"name": "영원 속박의 굴레", "note": "하얀 달의 온기는 햇빛처럼 대체"}
    last = -1
    for i, s in enumerate(result):
        if s and s.get("name"):
            last = i
    if last + 1 < len(result) and not result[last + 1]:
        result[last + 1] = entry
    else:
        result.append(entry)
    return result


def final_build(savior, R, consts):
    """사이트가 화면에 보여 주는 PVE 슬롯과 대체 슬롯 (원문 name/note). 디펜더는 ('defender', None, None)."""
    if savior.get("className") == "디펜더" and savior.get("id") != "white-pearl-luna":
        return "defender", None, None
    detail = savior.get("detail")
    arc = (detail or {}).get("arcana") or {}
    pve = arc.get("pve") if arc.get("pve") is not None else _pend()
    existing = arc.get("alternatives") if arc.get("alternatives") is not None else _pend()
    if any(COMMON in str((a or {}).get("name") or "") for a in pve):
        pve = consts["common"]
    if savior.get("id") in consts["swap_ids"]:
        pve = [{**s, "name": str(s["name"]).replace("완벽한 바니걸", "음독의 각오")} if s else None for s in pve]
    alts = build_alternative_arcana(savior, pve, existing, R, consts) if detail else existing
    return "normal", pve, add_eternal_bond(savior, pve, alts, R)


# ---------------------------------------------------------------- 슬림 변환
def _is_pending(name):
    return (name or "").strip() in ("미정", "")


def _slim_slot(entry, R):
    if not isinstance(entry, dict) or _is_pending(entry.get("name")):
        return None
    note = (entry.get("note") or "").strip()
    if note == "미정" or re.search(r"엑셀.*추천", note):
        note = ""
    return {"options": R.options(entry["name"]), "note": note}


def _target_slot(alt, slots):
    """대체 항목이 어느 PVE 슬롯(인덱스)을 대신하는지 -> (인덱스|None, 대상 이름|None, 추정 여부)."""
    note = (alt.get("note") or "").strip()
    m = re.match(r"^(.+?)\s*대체$", note)
    cands, inferred = ([x.strip() for x in m.group(1).split("/")] if m else []), False
    if not cands and not note and alt["options"]:
        d = DEFAULT_TARGETS.get(alt["options"][0]["name"])
        cands, inferred = ([d] if d else []), True
    for c in cands:
        for i, sl in enumerate(slots):
            if sl and any(o["name"] == c or o.get("alias") == c for o in sl["options"]):
                return i, c, inferred
    loose = cands[0] if cands and not inferred and m and not cands[0].endswith("자리") and "부족" not in cands[0] else None   # '부족한 자리 대체' 는 대상 없음
    return None, loose, False


def _slim_alts(alts, slots, R):
    out = []
    for pos, a in enumerate(alts or []):
        if not isinstance(a, dict):
            continue
        name, note = a.get("name") or "", (a.get("note") or "").strip()
        if _is_pending(name) and (not note or note == "미정"):
            continue
        item = {"options": [] if _is_pending(name) else R.options(name), "note": "" if note == "미정" else note, "pos": pos}
        item["slot"], item["target"], inferred = _target_slot(item, slots)
        if inferred:
            item["inferred"] = True
        if item["options"] or item["note"]:
            out.append(item)
    return out


def _slim_set(label, pve, alts, R):
    slots = [_slim_slot(x, R) for x in pve]
    return {"label": label, "slots": slots, "alternatives": _slim_alts(alts, slots, R)}


def build(core_js, app_js, arcanas_json):
    saviors = parse_savior_block(core_js)
    consts = parse_site_constants(core_js)
    warn = unported_patches(core_js, app_js)
    apply_load_time_patches(saviors, consts)
    R = Resolver(arcanas_json.get("arcanas", []), parse_detail_ids(core_js), parse_arcana_library(core_js), consts["common"])
    out = []
    for s in saviors:
        kind, pve, alts = final_build(s, R, consts)
        entry = {"key": s.get("id"), "name": s.get("name"), "subtitle": s.get("subtitle") or "", "affiliation": s.get("affiliation") or "",
                 "grade": s.get("grade") or "", "element": s.get("element") or "", "className": s.get("className") or "",
                 "slots": [], "alternatives": []}
        if kind == "defender":
            entry["sets"] = [_slim_set("퓨어탱커", consts["defender_pure"]["recommended"], consts["defender_pure"]["alternatives"], R),
                             _slim_set("공격탱커", consts["defender_attack"]["recommended"], consts["defender_attack"]["alternatives"], R)]
        else:
            if any(COMMON in str((a or {}).get("name") or "") for a in ((s.get("detail") or {}).get("arcana") or {}).get("pve") or []):
                entry["commonSet"] = True                       # 사이트가 '공용 아르카나' 5장으로 펼쳐 보여 준 구원자
            st = _slim_set("", pve, alts, R)
            if any(st["slots"]):
                entry["slots"], entry["alternatives"] = _pad_none(st["slots"]), st["alternatives"]
        out.append(entry)
    data = {"version": SCHEMA_VERSION, "unportedPatches": warn, "saviors": out}
    return data, R


def _pad_none(slots):
    return slots + [None] * max(0, 5 - len(slots))


def has_reco(s):
    return any(s.get("slots") or []) or bool(s.get("sets"))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    d = argv[1]
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "savior_reco.json")
    if "--out" in argv:
        out_path = argv[argv.index("--out") + 1]

    def rd(n):
        with open(os.path.join(d, n), "r", encoding="utf-8-sig") as f:
            return f.read()
    data, R = build(rd("app-core.js"), rd("app.js"), json.loads(rd("arcanas.json")))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    sv = data["saviors"]
    n = sum(R.stats.values())
    print(f"구원자 {len(sv)}명: 추천 보유 {sum(1 for s in sv if has_reco(s))} (디펜더 2세트 {sum(1 for s in sv if s.get('sets'))}, "
          f"공용 5장 펼침 {sum(1 for s in sv if s.get('commonSet'))}), 미정 {sum(1 for s in sv if not has_reco(s))}")
    print(f"이름 해석 {n}개: {R.stats}  미해결: {sorted(set(R.unresolved))}")
    if data["unportedPatches"]:
        print("경고: 사이트가 따로 패치하지만 포팅하지 않은 구원자:", data["unportedPatches"])
    print("저장:", os.path.abspath(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
