# -*- coding: utf-8 -*-
"""
jh_v2.py - 새 DB V2(https://starsavior-journey-choice.pages.dev/json/) 보충 계층 (GUI/OCR 의존성 없음)

가이드 DB(jh_db)에 없는 이벤트 제목(평가전 준비/승리, 출발점, 합숙훈련 등)을 V2 의 journeys.json 에서 보충한다.
* V2 는 원시 ID 형식(reward_id/type/min/max)이라, journey_items / journey_buffs / potentials 로 이름을 풀어
  앱이 쓰는 이벤트 행(db_event_cache 항목) 형식으로 변환한다.
* 가이드에 이미 있는 제목은 덮어쓰지 않는다.
* 모든 경로는 '실패해도 앱이 깨지지 않게' 설계: 없는 파일 요청은 HTML 이 오므로 JSON/구조 검증을 하고,
  비정상 응답은 캐시를 유지한다. 변환 중 예외는 그 제목만 건너뛴다.
"""
import json
import os
import re
import time
from collections import OrderedDict

import jh_db
import jh_net
import jh_paths

V2_BASE_URL = "https://starsavior-journey-choice.pages.dev/json/"
V2_FILES = OrderedDict([("journeys", "journeys.json"), ("journey_items", "journey_items.json"),
                        ("journey_buffs", "journey_buffs.json"), ("potentials", "potentials.json")])
V2_LOCALE = {"ko": "ko-KR", "en": "en-US"}
GUARD_MIN_RATIO = 0.5

STAT_NAMES = {
    "ko": {"JST_POWER": "힘", "JST_HEALTH": "체력", "JST_ENDURANCE": "인내", "JST_FOCUS": "집중", "JST_PROTECT": "보호"},
    "en": {"JST_POWER": "Strength", "JST_HEALTH": "Vitality", "JST_ENDURANCE": "Endurance",
           "JST_FOCUS": "Focus", "JST_PROTECT": "Protection"},
}
V2_STR = {
    "ko": {"coin": "오래된 주화", "coin_use": "오래된 동전 사용 {}", "stamina_use": "스태미나 사용 {}",
           "pp_use": "잠재력 포인트 사용 {}", "stat_need": "{} {} 필요", "item_use": "{} 소모",
           "buff_get": "여정 버프 획득 {} +{}턴", "rm_neg": "해로운 여정 버프 제거", "rm_pos": "이로운 여정 버프 제거",
           "stat_poten": "스탯 인자", "event_n": "{} {}"},
    "en": {"coin": "Old Coin", "coin_use": "Use Old Coin {}", "stamina_use": "Use Stamina {}",
           "pp_use": "Use Potential Point {}", "stat_need": "{} {} Required", "item_use": "{} Consumed",
           "buff_get": "Journey Buff Gained {} +{} Turn", "rm_neg": "Remove Harmful Journey Buffs",
           "rm_pos": "Remove Beneficial Journey Buffs", "stat_poten": "Stat Factor", "event_n": "{} {}"},
}


def _l(d, lang):
    """{'ko-KR':..,'en-US':..} 형태 필드에서 언어에 맞는 값 (없으면 한국어)"""
    if not isinstance(d, dict):
        return d if isinstance(d, str) else ""
    return d.get(V2_LOCALE.get(lang, "ko-KR")) or d.get("ko-KR") or ""


# ----------------------------------------------------------------------------
# 검증 / 캐시 / 갱신
# ----------------------------------------------------------------------------
def validate(name, data):
    """V2 파일 1개의 구조 검증. 맞지 않으면 ValueError (없는 파일 요청의 HTML 대체 응답은 JSON 파싱 단계에서 이미 걸러진다)"""
    if name == "journeys":
        ok = isinstance(data, dict) and data and all(
            isinstance(v, list) and all(isinstance(e, dict) and isinstance(e.get("choices"), list) for e in v)
            for v in data.values())
    else:
        ok = isinstance(data, list) and data and all(isinstance(x, dict) and "id" in x for x in data)
    if not ok:
        raise ValueError(f"{name}: unexpected structure")


class V2Store:
    """V2 JSON 로컬 캐시 + 조건부 GET 갱신기 (jh_db.DbStore 와 같은 방식: 검증 실패/급감 시 캐시 유지)"""

    def __init__(self, cache_dir=None, session=None, base_url=V2_BASE_URL, clock=time.time,
                 user_agent=jh_db.DEFAULT_USER_AGENT, seed_dir=jh_db._DEFAULT):
        self.dir = os.path.join(cache_dir or jh_db.default_cache_dir(), "v2")
        # 번들 시드 폴더(v2 파일이 직접 들어 있는 폴더). None 이면 사용 안 함
        if seed_dir is jh_db._DEFAULT:
            sd = jh_paths.seed_dir()
            seed_dir = os.path.join(sd, "v2") if sd else None
        self.seed_dir = seed_dir
        self.seed_used = False
        self.base_url, self.clock, self.user_agent = base_url, clock, user_agent
        self._session = session
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.meta = self._load_meta()

    def _load_meta(self):
        meta = {"last_check": 0, "last_fail": 0, "files": {}, "consecutive_fail": 0, "next_retry": 0, "manual_bypass": False}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in ("last_check", "last_fail", "next_retry"):
                    v = saved.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        meta[k] = v
                cf = saved.get("consecutive_fail")
                if isinstance(cf, int) and not isinstance(cf, bool) and cf > 0:
                    meta["consecutive_fail"] = cf
                meta["manual_bypass"] = saved.get("manual_bypass") is True
                if isinstance(saved.get("files"), dict):
                    meta["files"] = {k: v for k, v in saved["files"].items() if isinstance(v, dict)}
        except Exception:
            pass
        return meta

    def _save_meta(self):
        try:
            jh_db._atomic_write(self.meta_path, json.dumps(self.meta, ensure_ascii=False, indent=1).encode("utf-8"))
        except Exception:
            pass

    def _path(self, name):
        return os.path.join(self.dir, V2_FILES[name])

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    @staticmethod
    def _read_valid(path, name):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            validate(name, data)
            return data
        except Exception:
            return None

    def _seed_data(self, name):
        if not self.seed_dir:
            return None
        return self._read_valid(os.path.join(self.seed_dir, V2_FILES[name]), name)

    def has_cache(self):
        return self._read_valid(self._path("journeys"), "journeys") is not None

    def load(self):
        """{이름: 데이터} 반환. 파일별로 캐시 우선, 없거나 깨졌을 때만 번들 시드. journeys 가 없으면 None.
        나머지 파일이 없으면 빈 리스트로 대체. 시드는 메모리로만 읽는다(캐시/meta 생성 없음)."""
        out, self.seed_used = {}, False
        for name in V2_FILES:
            data = self._read_valid(self._path(name), name)
            if data is None:
                data = self._seed_data(name)
                if data is not None:
                    self.seed_used = True
            out[name] = data if data is not None else ({} if name == "journeys" else [])
        return out if out["journeys"] else None

    def retry_at(self):
        lf = self.meta.get("last_fail", 0)
        return max(self.meta.get("next_retry", 0), lf + jh_db.FAIL_BACKOFF_SEC if lf else 0)

    def update(self, manual=False):
        """-> jh_db.UpdateResult. 예외를 밖으로 내지 않는다. 실패는 지수 backoff(jh_net) 로 meta 에 저장."""
        now = self.clock()
        have = self.has_cache()
        retry = self.retry_at()
        if not manual:
            if have and now - self.meta.get("last_check", 0) < jh_db.CHECK_INTERVAL_SEC:
                return jh_db.UpdateResult("skipped_recent", False, "V2: 최근에 확인함")
            if now < retry:
                return jh_db.UpdateResult("backoff", False, f"V2: 사이트 응답 없음, {jh_net.minutes_left(retry, now)}분 뒤 재시도")
            if not jh_net.get_limiter("v2", jh_net.DB_ATTEMPTS_PER_DAY, 86400).allow(now):
                return jh_db.UpdateResult("capped", False, f"V2: 오늘 자동 갱신 시도 상한({jh_net.DB_ATTEMPTS_PER_DAY}회) 도달, 중단")
        elif now < retry:
            if self.meta.get("manual_bypass"):
                return jh_db.UpdateResult("backoff", False, f"V2: 사이트 응답 없음, {jh_net.minutes_left(retry, now)}분 뒤 재시도")
            self.meta["manual_bypass"] = True
        changed, errors = False, []
        for name, rel in V2_FILES.items():
            try:
                if self._fetch_one(name, rel) == "updated":
                    changed = True
            except Exception as e:               # 구조 오류(ValueError)는 파일별로 독립, 네트워크/HTTP 오류는 나머지를 시도하지 않고 중단
                errors.append(f"{rel}: {type(e).__name__} {str(e)[:100]}")
                if not isinstance(e, ValueError):
                    break
        if errors:
            n = self.meta["consecutive_fail"] + 1
            if now >= retry:
                self.meta["manual_bypass"] = False
            self.meta["consecutive_fail"], self.meta["last_fail"] = n, now
            self.meta["next_retry"] = now + jh_net.backoff_sec(n)
            self._save_meta()
            return jh_db.UpdateResult("offline", changed, f"V2 갱신 일부 실패, 캐시 사용 (연속 실패 {n}회, "
                                      f"{jh_net.backoff_sec(n) // 60}분 뒤 재시도): " + "; ".join(errors))
        self.meta["last_check"], self.meta["last_fail"] = now, 0
        self.meta["consecutive_fail"], self.meta["next_retry"], self.meta["manual_bypass"] = 0, 0, False
        self._save_meta()
        return jh_db.UpdateResult("updated" if changed else "not_modified", changed, "V2 갱신됨" if changed else "V2 변경 없음")

    def _fetch_one(self, name, rel):
        path = self._path(name)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        finfo = self.meta["files"].get(rel, {})
        if self._read_valid(path, name) is not None:      # 없거나 깨진 캐시(시드 상태 포함)는 조건부 요청 금지
            if finfo.get("etag"):
                headers["If-None-Match"] = finfo["etag"]
            if finfo.get("last_modified"):
                headers["If-Modified-Since"] = finfo["last_modified"]
        r = self.session.get(self.base_url.rstrip("/") + "/" + rel, headers=headers, timeout=jh_db.HTTP_TIMEOUT)
        if r.status_code == 304:
            return "not_modified"
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        content = r.content
        data = json.loads(content.decode("utf-8"))        # HTML 대체 응답이면 여기서 예외
        validate(name, data)
        # 기준: 유효한 캐시, 없으면 번들 시드(시드 상태에서 빈약한 응답이 캐시가 되는 것을 막음)
        old = self._read_valid(path, name) or self._seed_data(name)
        if old and len(data) < len(old) * GUARD_MIN_RATIO and len(old) >= 4:
            raise ValueError(f"{rel}: {len(old)} -> {len(data)} 로 비정상 감소, 캐시 유지")
        same = False
        try:
            with open(path, "rb") as f:
                same = f.read() == content
        except OSError:
            pass
        if not same:
            jh_db._atomic_write(path, content)
        h = r.headers or {}
        self.meta["files"][rel] = {"etag": h.get("ETag") or h.get("etag"),
                                   "last_modified": h.get("Last-Modified") or h.get("last-modified")}
        return "not_modified" if same else "updated"


# ----------------------------------------------------------------------------
# 이름 해석 색인
# ----------------------------------------------------------------------------
class V2Index:
    def __init__(self, v2, lang="ko"):
        self.lang = lang if lang in V2_LOCALE else "ko"
        self.s = V2_STR[self.lang]
        self.gs = jh_db.STR[self.lang]
        self.stats = STAT_NAMES[self.lang]

        def by_id(key):
            return {x.get("id"): x for x in (v2.get(key) or []) if isinstance(x, dict)}
        self.items, self.buffs, self.potens = by_id("journey_items"), by_id("journey_buffs"), by_id("potentials")

    def item_name(self, iid):
        return _l((self.items.get(iid) or {}).get("name"), self.lang)

    def is_invisible(self, iid):
        """게임 안에서 사용자에게 보이지 않는 내부용 아이템(다음 이벤트로 이어 주는 표시값). 설명에 '인비저블' 이 들어 있다."""
        it = self.items.get(iid) or {}
        return "인비저블" in (_l(it.get("description"), "ko") or "")

    def buff(self, bid):
        b = self.buffs.get(bid) or {}
        return _l(b.get("name"), self.lang), jh_db.strip_tags(_l(b.get("desc"), self.lang))

    def poten(self, pid):
        p = self.potens.get(pid) or {}
        return _l(p.get("name"), self.lang), jh_db.strip_tags(_l(p.get("desc"), self.lang))


def buff_desc_map(v2, lang="ko"):
    """{버프 이름: 설명} (InfoIndex 가 내장본보다 먼저 사용)"""
    out = {}
    for b in (v2 or {}).get("journey_buffs") or []:
        try:
            name, desc = _l(b.get("name"), lang), jh_db.strip_tags(_l(b.get("desc"), lang))
            if name and desc:
                out.setdefault(name, desc)
        except Exception:
            continue
    return out


def item_desc_map(v2, lang="ko"):
    """{아이템 이름: 설명(description)} - 설명이 있는 것만"""
    out = {}
    for it in (v2 or {}).get("journey_items") or []:
        try:
            name, desc = _l(it.get("name"), lang), jh_db.strip_tags(_l(it.get("description"), lang))
            if name and desc:
                out.setdefault(name, desc)
        except Exception:
            continue
    return out


# ----------------------------------------------------------------------------
# 변환: V2 원시 보상 -> 이벤트 행 형식
# ----------------------------------------------------------------------------
def _range(mn, mx):
    if mn is None:
        return ""
    if mx is None or mx == mn:
        return jh_db._fmt_num(mn)
    return f"{jh_db._fmt_num(mn)}~{mx}"


def _entry(r, ix, out):
    """보상 항목 1개 -> 표시 문자열 (items/info 는 out 에 누적). 알 수 없는 타입은 None."""
    t, s = r.get("type"), ix.s
    mn, mx = r.get("min"), r.get("max")
    if t == "RT_STAT":
        return f"{ix.stats.get(r.get('reward_stat'), str(r.get('reward_stat')))} {_range(mn, mx)}".strip()
    if t == "RT_STAMINA":
        return f"{ix.gs['stamina']} {_range(mn, mx)}".strip()
    if t == "RT_CONDITION":
        return f"{ix.gs['cond']} {_range(mn, mx)}".strip()
    if t == "RT_POTEN_POINT":
        return f"{ix.gs['pp']} {_range(mn, mx)}".strip()
    if t == "RT_COIN":
        return f"{s['coin']} {_range(mn, mx)}".strip()
    if t == "RT_SE_POTEN":
        name, desc = ix.poten(r.get("reward_id"))
        if not name:
            return None
        if desc and all(i["name"] != name for i in out["info"]):
            out["info"].append({"name": name, "desc": desc, "kind": ix.gs["poten"]})
        return f"{ix.gs['poten']} {name}"
    if t == "RT_STAT_POTEN":
        return s["stat_poten"]
    if t == "RT_JOURNEY_BUFF":
        name, desc = ix.buff(r.get("reward_id"))
        if not name:
            return None
        if desc and all(i["name"] != name for i in out["info"]):
            out["info"].append({"name": name, "desc": desc, "kind": ix.gs["buff"]})
        return s["buff_get"].format(name, mn if mn is not None else 1)
    if t == "RT_JOURNEY_BUFF_REMOVE_NEG":
        return s["rm_neg"]
    if t == "RT_JOURNEY_BUFF_REMOVE_POS":
        return s["rm_pos"]
    if t in ("RT_JOURNEY_ITEM", "NT_CHARM"):
        ids = r.get("options") if isinstance(r.get("options"), list) and r.get("options") else [r.get("reward_id")]
        ids = [i for i in ids if not ix.is_invisible(i)]            # 내부용(인비저블) 아이템은 보상 줄에 보이지 않는다
        names = [n for n in (ix.item_name(i) for i in ids) if n]
        for n in names:
            it = {"name": n, "role": "grant"}
            if it not in out["items"]:
                out["items"].append(it)
        return " or ".join(names) if names else None
    return None


def _condition_line(cond, ix, out):
    if not isinstance(cond, dict):
        return None
    t, v, s = cond.get("type"), cond.get("value"), ix.s
    if t == "RR_STAMINA_USE":
        return s["stamina_use"].format(v)
    if t == "RR_COIN_USE":
        return s["coin_use"].format(v)
    if t == "RR_PP_USE":
        return s["pp_use"].format(v)
    if t == "RR_STAT":
        return s["stat_need"].format(ix.stats.get(cond.get("target"), str(cond.get("target"))), v)
    if t == "RR_ITEM_USE":
        try:
            name = ix.item_name(int(cond.get("target")))
        except (TypeError, ValueError):
            name = ""
        if not name:
            return None
        it = {"name": name, "role": "consume"}
        if it not in out["items"]:
            out["items"].append(it)
        return s["item_use"].format(name)
    return None


def _choice_option(ch, ix):
    out = {"items": [], "info": []}
    lines = []
    cl = _condition_line(ch.get("condition"), ix, out)
    if cl:
        lines.append(cl)
    succ, fail = ch.get("success_rewards") or [], ch.get("failure_rewards") or []

    def add(groups, tag):
        for grp in groups:
            parts = [p for p in (_entry(r, ix, out) for r in (grp or [])) if p]
            if parts:
                lines.append(tag + " or ".join(parts))
    add(succ, "[SUCCESS] " if fail else "")
    add(fail, "[FAIL] ")
    dedup = []                                          # 똑같은 줄이 연속되면 '×N' 으로 합친다 (예: 스탯 인자 5번)
    for ln in lines:
        if dedup and dedup[-1][0] == ln:
            dedup[-1][1] += 1
        else:
            dedup.append([ln, 1])
    lines = [l if n == 1 else f"{l} ×{n}" for l, n in dedup]
    return {
        "choice": jh_db.strip_tags(_l(ch.get("name"), ix.lang)),
        "reward": "\n".join(lines),
        "has_condition": any(jh_db.is_condition_line(jh_db._TAG_PREFIX_EARLY.sub("", l)) for l in lines),
        "recommended": False,
        "info": out["info"],
        "items": out["items"],
        "result": jh_db._compact_result(lines),
    }


def convert_title(title, events, ix):
    """한 제목의 V2 이벤트들 -> db_event_cache 항목 1개 (선택지가 없으면 None)"""
    cands = []
    multi = len(events) > 1
    for n, ev in enumerate(events, 1):
        opts = [_choice_option(ch, ix) for ch in ev.get("choices") or [] if isinstance(ch, dict)]
        if not opts:
            continue
        src = ix.s["event_n"].format(_l(ev.get("type"), ix.lang) or "V2", n) if multi else ""
        cands.append((src, opts))
    if not cands:
        return None
    ev0 = events[0]
    is_arcana = "아르카나" in (ev0.get("type", {}).get("ko-KR", "") if isinstance(ev0.get("type"), dict) else "")
    sources = [s for s, _ in cands if s]
    return {
        "rawTitle": title,
        "cleanTitle": jh_db.clean_title(title),
        "isArcana": bool(is_arcana),
        "options": jh_db._merge_candidates(cands),
        "sources": sources,
        "arcanaIds": {},
        "timing": "",
        "v2": True,
    }


REPLACED_TITLES = []        # 마지막 supplement_cache 에서 '선택지 구성이 달라' V2 로 바꾼 제목(로그용)


def _choice_names(entry):
    """선택지 이름 목록(공백 제거). 가이드와 V2 가 같은 이벤트의 같은 판인지 비교하는 데 쓴다."""
    return [re.sub(r"\s+", "", (o.get("choice") or "")) for o in entry.get("options") or []]


def supplement_cache(cache, v2, lang="ko"):
    """cache(list, 제자리 수정)에 V2 에만 있는 제목을 추가한다.
    가이드에 이미 있는 제목(cleanTitle)은 건드리지 않는다. 단 **같은 제목인데 선택지 구성(개수·이름)이 서로 다르면**
    게임이 업데이트로 이벤트를 바꾼 것이므로 게임 문구와 맞는 새 쪽(V2)으로 바꾼다(2026-10-18: '훈련의 방향성' 가이드 3개 vs 게임·V2 4개).
    제목 단위로 예외를 격리한다. -> 새로 추가한 제목 수(바꾼 것은 REPLACED_TITLES)"""
    REPLACED_TITLES.clear()
    journeys = (v2 or {}).get("journeys")
    if not isinstance(journeys, dict):
        return 0
    ix = V2Index(v2, lang)
    have = {e.get("cleanTitle"): i for i, e in enumerate(cache)}
    added = []
    for raw, evs in journeys.items():
        try:
            title = _l(evs[0].get("name"), lang) or raw if lang != "ko" else raw
            ct = jh_db.clean_title(title)
            if not ct or jh_db.IGNORED_TITLE_WORD in ct:   # 평가전은 고정 일정이라 다루지 않는다
                continue
            if ct in have:
                old = cache[have[ct]]
                if old.get("isArcana") or old.get("v2"):
                    continue
                ent = convert_title(title, evs, ix)
                if ent and ent.get("options") and _choice_names(ent) != _choice_names(old):
                    ent["timing"] = old.get("timing") or ent.get("timing", "")        # 시기/출처 표기는 가이드 것을 유지
                    ent["sources"] = old.get("sources") or ent.get("sources", [])
                    cache[have[ct]] = ent
                    REPLACED_TITLES.append(title)
                continue
            ent = convert_title(title, evs, ix)
            if ent:
                added.append(ent)
                have[ct] = len(cache) + len(added) - 1
        except Exception:
            continue
    cache.extend(added)
    return len(added)


def attach_item_desc(cache, v2, lang="ko"):
    """아이템 설명을 데이터로만 노출: option['items'][i]['desc'], option['item_use'|'item_from'][i]['desc'] (표시는 바꾸지 않는다)"""
    descs = item_desc_map(v2, lang)
    if not descs:
        return 0
    n = 0
    for ev in cache:
        for o in ev.get("options") or []:
            for it in o.get("items") or []:
                d = descs.get(it.get("name"))
                if d:
                    it["desc"] = d
                    n += 1
            for key in ("item_use", "item_from"):
                for g in o.get(key) or []:
                    d = descs.get(g.get("item"))
                    if d:
                        g["desc"] = d
    return n
