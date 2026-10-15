# -*- coding: utf-8 -*-
"""
jh_db.py - JourneyHelper Lite 데이터 계층 (GUI/OCR 의존성 없음)

* 새 DB(https://starsavior-guide.github.io/data/)를 내려받아 로컬 캐시에 보관
* 하루 두 번 이하(기본 12시간)만 확인하고, ETag / Last-Modified 조건부 GET(304)을 사용
* 수동 새로고침은 10분 쿨다운, 오프라인/실패 시에는 캐시를 조용히 사용
* journey.json(+arcanas.json)을 정규화하여 기존 헬퍼가 쓰던 db_event_cache 구조로 변환
    [{rawTitle, cleanTitle, isArcana, options:[{choice, reward, has_condition, ...}]}]
* 아이템/버프/잠재력 설명(info)을 선택지에 붙이고, 툴팁용 HTML을 만든다.
"""
import html as _html
import json
import os
import re
import tempfile
import time
from collections import OrderedDict, namedtuple

BASE_URL = "https://starsavior-guide.github.io/data/"
SUPPORTED_LANGS = ("ko", "en")


def db_files(lang="ko"):
    """언어별로 내려받을 파일 목록 (arcanas.json 은 ko/en/ja 이름이 모두 들어 있어 공용)"""
    return OrderedDict([("journey", f"journey/{lang}/journey.json"), ("arcanas", "arcanas/arcanas.json")])


DB_FILES = db_files("ko")

# 화면 문구 (DB 가 아니라 이 모듈이 만드는 텍스트)
STR = {
    "ko": {"buff": "여정 버프", "poten": "잠재력", "auto": "자동 진행", "ok": "성공", "fail": "실패",
           "noinfo": "(정보 없음)", "dup": "※ 같은 제목의 이벤트가 여러 아르카나에 있습니다", "more": "외 {}",
           "stamina": "스태미나", "cond": "컨디션", "pp": "잠재력 포인트", "turn": "{}턴",
           "use_head": "📦 이 아이템의 이후 사용처", "use_none": "이후 사용처 없음 / 확인 안 됨",
           "more_n": "외 {}건", "from_head": "📥 획득처",
           "ambig": "※ 제목이 비슷해 확정하지 못했습니다 - 화면의 제목과 맞는 것을 보세요"},
    "en": {"buff": "Journey Buff", "poten": "Potential", "auto": "Automatic", "ok": "Success", "fail": "Failure",
           "noinfo": "(no info)", "dup": "* Several arcanas have an event with this title", "more": "+{} more",
           "stamina": "Stamina", "cond": "Condition", "pp": "Potential Point", "turn": "{} Turn",
           "use_head": "📦 Later uses of this item", "use_none": "No later use found / unknown",
           "more_n": "+{} more", "from_head": "📥 Obtained from",
           "ambig": "* Title match is uncertain - check which one matches the screen"},
}


def _loc(d, lang):
    """{'ko':..,'en':..,'ja':..} 형태 필드에서 언어에 맞는 값 (비어 있으면 한국어로 대체)"""
    if not isinstance(d, dict):
        return d or ""
    return d.get(lang) or d.get("ko") or ""
CHECK_INTERVAL_SEC = 12 * 60 * 60     # 자동 확인 최소 간격
MANUAL_COOLDOWN_SEC = 10 * 60         # 수동 새로고침 쿨다운
FAIL_BACKOFF_SEC = 10 * 60            # 실패 직후 자동 재시도 금지 시간
HTTP_TIMEOUT = (5, 20)                # (connect, read)
DEFAULT_USER_AGENT = "JourneyHelperLite/1.0 (+https://github.com/QooReche/journeyhelper; personal game helper)"

UpdateResult = namedtuple("UpdateResult", "status changed message")


# ----------------------------------------------------------------------------
# 캐시 / 조건부 갱신
# ----------------------------------------------------------------------------
def default_cache_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JourneyHelperLite", "cache")


def _atomic_write(path, data_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data_bytes)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class DbStore:
    """로컬 캐시 + 조건부 GET 갱신기. session은 requests.Session 호환(get 메서드)이면 된다."""

    def __init__(self, cache_dir=None, session=None, base_url=BASE_URL,
                 clock=time.time, user_agent=DEFAULT_USER_AGENT, lang="ko"):
        self.lang = lang if lang in SUPPORTED_LANGS else "ko"
        self.files = db_files(self.lang)
        self.cache_dir = cache_dir or default_cache_dir()
        self.base_url = base_url
        self.clock = clock
        self.user_agent = user_agent
        self._session = session
        self.meta_path = os.path.join(self.cache_dir, "meta.json")
        self.meta = self._load_meta()

    # --- meta ---
    def _load_meta(self):
        meta = {"last_check": 0, "last_fail": 0, "last_manual": 0, "files": {}}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                meta.update({k: saved[k] for k in meta if k in saved})
        except Exception:
            pass
        return meta

    def _save_meta(self):
        try:
            _atomic_write(self.meta_path, json.dumps(self.meta, ensure_ascii=False, indent=1).encode("utf-8"))
        except Exception:
            pass

    def _file_path(self, rel):
        return os.path.join(self.cache_dir, *rel.split("/"))

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    # --- 조회 ---
    def has_cache(self):
        return all(os.path.isfile(self._file_path(rel)) for rel in self.files.values())

    def load(self):
        """(journey, arcanas) 반환. 없거나 깨졌으면 (None, None). arcanas는 없어도 동작한다."""
        out = {}
        for key, rel in self.files.items():
            try:
                with open(self._file_path(rel), "r", encoding="utf-8") as f:
                    out[key] = json.load(f)
            except Exception:
                out[key] = None
        return out["journey"], out["arcanas"]

    def seconds_until_manual(self):
        return max(0, int(self.meta.get("last_manual", 0) + MANUAL_COOLDOWN_SEC - self.clock()))

    # --- 갱신 ---
    def update(self, manual=False):
        now = self.clock()
        have = self.has_cache()
        if manual:
            remain = self.seconds_until_manual()
            if remain > 0:
                return UpdateResult("cooldown", False, f"수동 새로고침은 {remain // 60 + 1}분 뒤에 다시 가능합니다")
            self.meta["last_manual"] = now
        else:
            if have and now - self.meta.get("last_check", 0) < CHECK_INTERVAL_SEC:
                return UpdateResult("skipped_recent", False, "최근에 확인함 (12시간 이내)")
            if now - self.meta.get("last_fail", 0) < FAIL_BACKOFF_SEC:
                return UpdateResult("backoff", False, "직전 실패 후 대기 중")

        changed, failed, not_modified = False, None, 0
        for key, rel in self.files.items():
            try:
                res = self._fetch_one(rel)
            except Exception as e:  # 네트워크 오류, 파싱 오류 등: 조용히 캐시 사용
                failed = e
                break
            if res == "updated":
                changed = True
            else:
                not_modified += 1

        if failed is not None:
            self.meta["last_fail"] = now
            self._save_meta()
            return UpdateResult("offline", changed, f"갱신 실패, 캐시 사용: {type(failed).__name__}")
        self.meta["last_check"] = now
        self.meta["last_fail"] = 0
        self._save_meta()
        if changed:
            return UpdateResult("updated", True, "DB 갱신됨")
        return UpdateResult("not_modified", False, "변경 없음 (304)")

    def _fetch_one(self, rel):
        url = self.base_url.rstrip("/") + "/" + rel
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        finfo = self.meta["files"].get(rel, {})
        if os.path.isfile(self._file_path(rel)):
            if finfo.get("etag"):
                headers["If-None-Match"] = finfo["etag"]
            if finfo.get("last_modified"):
                headers["If-Modified-Since"] = finfo["last_modified"]
        r = self.session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        code = r.status_code
        if code == 304:
            return "not_modified"
        if code != 200:
            raise RuntimeError(f"HTTP {code}")
        content = r.content
        data = json.loads(content.decode("utf-8"))      # 깨진 응답이 캐시를 덮어쓰지 않도록 먼저 검증
        if not isinstance(data, dict) or not data:
            raise ValueError("unexpected JSON")
        same = False
        try:                # GitHub Pages(CDN 노드별로 ETag 가 달라짐)는 내용이 같아도 200 을 줄 수 있다
            with open(self._file_path(rel), "rb") as f:
                same = f.read() == content
        except OSError:
            pass
        if not same:
            _atomic_write(self._file_path(rel), content)
        h = r.headers or {}
        self.meta["files"][rel] = {"etag": h.get("ETag") or h.get("etag"),
                                   "last_modified": h.get("Last-Modified") or h.get("last-modified")}
        return "not_modified" if same else "updated"


# ----------------------------------------------------------------------------
# 정규화
# ----------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]*>")


def strip_tags(s):
    return _html.unescape(_TAG_RE.sub("", s or "")).strip()


def clean_title(s):
    return re.sub(r"[^가-힣0-9a-zA-Z]", "", s or "").lower()


def _fmt_num(n):
    if isinstance(n, float) and n == int(n):
        n = int(n)
    return f"+{n}" if n >= 0 else str(n)


class InfoIndex:
    """잠재력/여정 버프 이름 -> 설명 (arcanas.json)"""

    def __init__(self, arcanas=None, lang="ko"):
        self.lang = lang if lang in SUPPORTED_LANGS else "ko"
        self.s = STR[self.lang]
        self.potentials, self.buffs = {}, {}
        for p in (arcanas or {}).get("potentials", []) or []:
            self.potentials[p.get("id")] = p
        for b in (arcanas or {}).get("journeyBuffs", []) or []:
            self.buffs[b.get("id")] = b
        self.by_name = {}
        for kind, src in ((self.s["poten"], self.potentials), (self.s["buff"], self.buffs)):
            for obj in src.values():
                name = _loc(obj.get("name"), self.lang)
                desc = strip_tags(_loc(obj.get("description"), self.lang))
                if name and desc:
                    self.by_name.setdefault(name, {"name": name, "desc": desc, "kind": kind})
        self._names = sorted(self.by_name, key=len, reverse=True)

    def pname(self, pid):
        return _loc((self.potentials.get(pid) or {}).get("name"), self.lang)

    def pdesc(self, pid):
        return strip_tags(_loc((self.potentials.get(pid) or {}).get("description"), self.lang))

    def bname(self, bid):
        return _loc((self.buffs.get(bid) or {}).get("name"), self.lang)

    def bdesc(self, bid):
        return strip_tags(_loc((self.buffs.get(bid) or {}).get("description"), self.lang))

    def find_in_text(self, text):
        """표 형태 이벤트의 보상 문자열에서 알려진 잠재력/버프 이름을 찾아 설명을 돌려준다."""
        found, rest = [], text
        for name in self._names:
            if name in rest and (len(name) >= 3 or "버프" in text or "Buff" in text):
                found.append(self.by_name[name])
                rest = rest.replace(name, " ")
        return found


_COND_WORDS = ("사용", "소모", "필요")
_COND_EN = re.compile(r"^(Use|Requires?)\b", re.I)


def is_condition_line(line):
    if _COND_EN.match(line.strip()):
        return True
    return any(w in line for w in _COND_WORDS) and "+" not in line


def _entry_to_line_and_info(x, idx):
    """journey.json 의 보상 항목 1개 -> (표시 문자열, info 또는 None)"""
    t = x.get("sourceType")
    label = x.get("label") or ""
    value = x.get("value") or ""
    desc = strip_tags(x.get("description"))
    if t == "RT_JOURNEY_BUFF":
        name = label.split("·", 1)[-1].strip() if "·" in label else label
        if not desc and idx is not None:
            desc = idx.bdesc(x.get("sourceId"))
        return (f"{idx.s['buff']} {name} {value}".strip(),
                ({"name": name, "desc": desc, "kind": idx.s["buff"]} if desc else None))
    if t == "RT_SE_POTEN":
        name = label or (idx.pname(x.get("sourceId")) if idx else "")
        if not desc and idx is not None:
            desc = idx.pdesc(x.get("sourceId"))
        return (f"{idx.s['poten']} {name}".strip(),
                ({"name": name, "desc": desc, "kind": idx.s["poten"]} if desc else None))
    return f"{label} {value}".strip(), None


def _raw_entry_to_journey_entry(r, idx):
    """arcanas.json(arcanas[].events) 의 원본 보상 항목 -> journey.json 형태"""
    t = r.get("type")
    mn, mx = r.get("min"), r.get("max")
    if mn is None:
        value = ""
    elif mx is None or mx == mn:
        value = _fmt_num(mn)
    else:
        value = f"{_fmt_num(mn)}~{_fmt_num(mx)}"
    e = {"sourceType": t, "sourceId": r.get("rewardId"), "label": "", "value": value, "description": ""}
    if t == "RT_STAT":
        e["label"] = _loc(r.get("statName"), idx.lang)
    elif t == "RT_STAMINA":
        e["label"] = idx.s["stamina"]
    elif t == "RT_CONDITION":
        e["label"] = idx.s["cond"]
    elif t == "RT_POTEN_POINT":
        e["label"] = idx.s["pp"]
    elif t == "RT_SE_POTEN":
        e["label"] = idx.pname(r.get("rewardId"))
        e["value"] = ""
    elif t == "RT_JOURNEY_BUFF":
        name = idx.bname(r.get("rewardId")) or _loc(r.get("journeyBuffName"), idx.lang)
        e["label"] = f"{idx.s['buff']} · {name}"
        e["value"] = idx.s["turn"].format(mn) if mn is not None else ""
    else:
        e["label"] = str(t)
    return e


def _choice_to_option(name, success_groups, failure_groups, idx, recommended=False):
    lines, info = [], []
    has_fail = bool(failure_groups)

    def add(groups, tag):
        for grp in groups or []:
            for x in grp:
                line, inf = _entry_to_line_and_info(x, idx)
                if not line:
                    continue
                lines.append(f"{tag}{line}")
                if inf and all(i["name"] != inf["name"] for i in info):
                    info.append(inf)

    add(success_groups, "[SUCCESS] " if has_fail else "")
    add(failure_groups, "[FAIL] ")
    return {
        "choice": (name or "").strip(),
        "reward": "\n".join(lines),
        "has_condition": any(is_condition_line(re.sub(r"^\[(SUCCESS|FAIL)\]\s*", "", l)) for l in lines),
        "recommended": bool(recommended),
        "info": info,
    }


def _arcana_events_from_journey(journey, idx):
    """[(arcanaId, arcanaName, [event_group...])] 에서 (제목, 출처, 옵션들) 추출"""
    out = []
    for a in journey.get("arcana", []) or []:
        aname = a.get("arcanaName") or ""
        for g in a.get("eventGroups", []) or []:
            opts = []
            for ch in g.get("choices", []) or []:
                opts.append(_choice_to_option(ch.get("name"), ch.get("successRewards"), ch.get("failureRewards"),
                                              idx, recommended=ch.get("recommended") and not ch.get("automatic")))
            out.append((a.get("arcanaId"), g.get("title") or "", aname, opts))
    return out


def _arcana_events_from_raw(arcanas, idx, skip_ids):
    out = []
    for a in (arcanas or {}).get("arcanas", []) or []:
        if a.get("id") in skip_ids:
            continue
        aname = _loc(a.get("name"), idx.lang)
        for ev in a.get("events", []) or []:
            opts = []
            for ch in ev.get("choices", []) or []:
                conv = lambda groups: [[_raw_entry_to_journey_entry(r, idx) for r in grp] for grp in groups or []]
                opts.append(_choice_to_option(_loc(ch.get("name"), idx.lang), conv(ch.get("successRewards")),
                                              conv(ch.get("failureRewards")), idx))
            out.append((a.get("id"), _loc(ev.get("name"), idx.lang), aname, opts))
    return out


_NUM_PREFIX = re.compile(r"^\s*\d+\s*[.)]\s*")

# ----------------------------------------------------------------------------
# 아이템 (표 형태 이벤트의 이미지 행)
#   획득: 이미지가 있는 보상 행 (success/failure/neutral 톤) 이고 텍스트 == 에셋 이름
#   소모/필요: 텍스트가 '<이름> 소모' (이미지 동반, tone=neutral) -> role 'consume'
#   '여정 버프 획득 ...', '... 10 % 할인', '... +N턴' 은 아이템이 아니므로 제외
# ----------------------------------------------------------------------------
_CONSUME_RE = re.compile(r"^(?P<n>.+?)\s*(?P<w>소모|필요|보유|Consumed?|Required?)\s*$", re.I)
_NOT_ITEM_RE = re.compile(r"^(여정 버프|Journey Buff)|할인|%|[+＋]\s*\d+\s*(턴|Turns?)\b", re.I)
_OR_SPLIT = re.compile(r"\s+or(?:\s+|$)")


def _row_images(r):
    out = []
    for seg in r.get("segments") or []:
        if isinstance(seg, dict) and seg.get("type") == "images":
            for im in seg.get("images") or []:
                a = im.get("asset") if isinstance(im, dict) else None
                if a:
                    out.append(re.sub(r"\.(webp|png|jpg)$", "", a, flags=re.I))
    return out


def row_items(r):
    """표 행 1개 -> [{'name', 'role': grant|consume|require}] (없으면 [])."""
    try:
        text = (r.get("text") or "").strip()
        if not text or _NOT_ITEM_RE.search(text):
            return []
        has_img = bool(_row_images(r))
        m = _CONSUME_RE.match(text)
        if m:
            name = m.group("n").strip()
            if not name or (not has_img and (re.search(r"\d", name) or _DIFF_RE_EARLY.match(name))):
                return []
            role = "consume" if m.group("w") in ("소모",) or m.group("w").lower().startswith("consum") else "require"
            return [{"name": name, "role": role}]
        if not has_img or r.get("tone") not in ("success", "failure", "neutral"):
            return []
        names = [n.strip() for n in _OR_SPLIT.split(text) if n.strip()]
        return [{"name": n, "role": "grant"} for n in names if n.lower() != "or"]
    except Exception:
        return []


_DIFF_RE_EARLY = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)\b")


def _compact_result(lines):
    """옵션의 보상 줄들 -> '힘+10 체력+10' 형태의 짧은 요약"""
    parts = []
    for l in lines:
        l = _TAG_PREFIX_EARLY.sub("", (l or "").strip())
        if not l or l.lower() == "or" or is_condition_line(l) or _CONSUME_RE.match(l) or l in ("없음", "None"):
            continue
        parts.append(re.sub(r"\s+(?=[+-]\d)", "", l))
    txt = " ".join(parts)
    return txt if len(txt) <= 44 else txt[:43] + "…"


_TAG_PREFIX_EARLY = re.compile(r"^\[(SUCCESS|FAIL)\]\s*")



def parse_table_event(ev, idx):
    """events[] (표 형태 행) -> (timing, options). 한계는 README 참고."""
    rows = ev.get("rows") or []
    timing = ""
    options = []
    cur = None          # {'choice': str, 'lines': [(region, text)]}

    def new_opt(name):
        o = {"choice": name, "lines": []}
        options.append(o)
        return o

    for i, r in enumerate(rows):
        text = (r.get("text") or "").strip()
        if not text:
            continue
        region, tone = r.get("region", ""), r.get("tone", "")
        if i == 0 and region == "" and tone == "plain":
            continue                                    # 제목 행
        if region and tone == "neutral" and r.get("fontSize", 0) <= 13 and not timing and cur is None and not options:
            timing = text                               # '3월 초순' 같은 시기 표기
            continue
        if region == "" and tone == "plain" and r.get("weight", 400) >= 700 and _NUM_PREFIX.match(text):
            cur = new_opt(_NUM_PREFIX.sub("", text).strip())
            continue
        if cur is None:
            cur = new_opt("")
        for it in row_items(r):
            cur.setdefault("items", [])
            if it not in cur["items"]:
                cur["items"].append(it)
        if text.lower() == "or":
            cur["lines"].append(("", "or"))
            continue
        lines = cur["lines"]
        if lines and lines[-1][0] == region and region and lines[-1][1].rstrip().endswith(" or"):
            lines[-1] = (region, lines[-1][1].rstrip() + " " + text)    # 셀 줄바꿈으로 갈라진 'A or B or' 이어붙임
        else:
            lines.append((region, text))

    out = []
    for o in options:
        texts = [t for _, t in o["lines"]]
        info = []
        for t in texts:
            for inf in idx.find_in_text(t):
                if all(i["name"] != inf["name"] for i in info):
                    info.append(inf)
        out.append({
            "choice": o["choice"],
            "reward": "\n".join(texts),
            "has_condition": any(is_condition_line(t) for t in texts),
            "recommended": False,
            "info": info,
            "items": o.get("items") or [],
            "result": _compact_result(texts),
        })
    return timing, out


def _merge_candidates(cands):
    """같은 제목의 후보들(각 후보 = (출처, 옵션리스트)) -> 합쳐진 옵션 리스트.
    선택지 번호(index)가 같은 것끼리 묶고, 보상이 같으면 출처만 합친다."""
    n = max(len(opts) for _, opts in cands)
    merged = []
    for i in range(n):
        variants = OrderedDict()
        first = None
        info_union = []
        items_union = []
        for src, opts in cands:
            if i >= len(opts):
                continue
            o = opts[i]
            first = first or o
            key = (o["choice"], o["reward"])
            v = variants.setdefault(key, {"sources": [], "choice": o["choice"], "reward": o["reward"], "info": o["info"]})
            if src and src not in v["sources"]:
                v["sources"].append(src)
            for inf in o["info"]:
                if all(x["name"] != inf["name"] for x in info_union):
                    info_union.append(inf)
            for it in o.get("items") or []:
                if it not in items_union:
                    items_union.append(it)
        vlist = list(variants.values())
        merged.append({
            "choice": first["choice"],
            "reward": vlist[0]["reward"],
            "has_condition": any(is_condition_line(re.sub(r"^\[(SUCCESS|FAIL)\]\s*", "", l))
                                 for v in vlist for l in v["reward"].split("\n")),
            "recommended": first.get("recommended", False),
            "info": info_union,
            "variants": vlist,
            "items": items_union,
            "result": first.get("result", ""),
        })
    return merged


def build_event_cache(journey, arcanas=None, lang="ko"):
    """journey.json (+ arcanas.json) -> db_event_cache 리스트."""
    idx = InfoIndex(arcanas, lang)
    groups = OrderedDict()      # cleanTitle -> {'raw','arcana','cands':[(src, opts)]}

    def add(title, is_arcana, src, opts):
        ct = clean_title(title)
        if not ct:
            return
        g = groups.setdefault(ct, {"raw": title, "arcana": is_arcana, "cands": []})
        g["cands"].append((src, opts))

    journey = journey or {}
    jr_ids = set()
    for a in journey.get("arcana", []) or []:
        jr_ids.add(a.get("arcanaId"))
    for _, title, aname, opts in _arcana_events_from_journey(journey, idx):
        add(title, True, aname, opts)
    for _, title, aname, opts in _arcana_events_from_raw(arcanas, idx, jr_ids):
        add(title, True, aname, opts)
    for ev in journey.get("events", []) or []:
        timing, opts = parse_table_event(ev, idx)
        if not opts:
            opts = [{"choice": "", "reward": "", "has_condition": False, "recommended": False, "info": []}]
        add(ev.get("title") or "", False, timing, opts)

    cache = []
    for ct, g in groups.items():
        options = _merge_candidates(g["cands"])
        cache.append({
            "rawTitle": g["raw"],
            "cleanTitle": ct,
            "isArcana": g["arcana"],
            "options": options,
            "sources": [s for s, _ in g["cands"] if s],
            "timing": "" if g["arcana"] else next((s for s, _ in g["cands"] if s), ""),
        })
    link_items(cache)
    return cache


# ----------------------------------------------------------------------------
# 아이템 색인: 어느 이벤트에서 얻고(grant) 어디에서 쓰는지(consume/require)
# ----------------------------------------------------------------------------
_TIMING_RE = re.compile(r"(\d+)\s*월\s*(초순|중순|하순)?")
_PHASE = {"초순": 1, "중순": 2, "하순": 3}


def _timing_key(t):
    m = _TIMING_RE.search(t or "")
    return (int(m.group(1)), _PHASE.get(m.group(2), 0)) if m else (99, 0)


def _clean_choice(c):
    return re.sub(r"[\.\s]+$", "", c or "")


def build_item_index(cache):
    """db_event_cache -> {item: {'grants': [ref...], 'uses': [ref...]}} (ref = event/timing/choice/result/kind)"""
    index = OrderedDict()
    for ev in cache or []:
        for oi, o in enumerate(ev.get("options") or []):
            for it in o.get("items") or []:
                try:
                    e = index.setdefault(it["name"], {"grants": [], "uses": []})
                    ref = {"event": ev.get("rawTitle", ""), "timing": ev.get("timing", ""),
                           "choice": _clean_choice(o.get("choice")), "result": o.get("result", ""),
                           "kind": it["role"], "key": ev.get("cleanTitle", "")}
                    e["grants" if it["role"] == "grant" else "uses"].append(ref)
                except (KeyError, TypeError):
                    continue
    for e in index.values():
        for k in ("grants", "uses"):
            e[k].sort(key=lambda r: _timing_key(r["timing"]))     # 안정 정렬: 시기 없으면 DB 순서
    return index


def link_items(cache):
    """각 선택지에 item_use(획득 아이템의 이후 사용처) / item_from(소모 아이템의 획득처)을 붙인다. 실패해도 조용히 무시."""
    try:
        index = build_item_index(cache)
    except Exception:
        return
    for ev in cache or []:
        for o in ev.get("options") or []:
            use, frm = [], []
            for it in o.get("items") or []:
                ent = index.get(it.get("name"))
                if not ent:
                    continue
                if it.get("role") == "grant":
                    uses = [r for r in ent["uses"] if r["key"] != ev.get("cleanTitle")]
                    use.append({"item": it["name"], "uses": uses})
                else:
                    srcs = [r for r in ent["grants"] if r["key"] != ev.get("cleanTitle")]
                    frm.append({"item": it["name"], "from": srcs})
            if use:
                o["item_use"] = use
            if frm:
                o["item_from"] = frm


# ----------------------------------------------------------------------------
# 툴팁 HTML
# ----------------------------------------------------------------------------
DIFF_BADGE_COLORS = {"이지": "#3ddc84", "노말": "#ffcc4d", "하드": "#ff6b6b", "Easy": "#3ddc84", "Normal": "#ffcc4d", "Hard": "#ff6b6b"}
_DIFF_RE = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)(?:\s+(.*))?$")
_TAG_PREFIX = re.compile(r"^\[(SUCCESS|FAIL)\]\s*")
_OUTCOME_STYLE = {"SUCCESS": ("rgba(61,220,132,0.16)", "#3ddc84"), "FAIL": ("rgba(255,92,92,0.16)", "#ff5c5c")}
MAX_INFO_ITEMS = 6


def _split_blocks(reward):
    """보상 문자열 -> [{'type': SUCCESS|FAIL|NORMAL, 'lines': [...]}] ('or' 와 성공/실패 전환 기준)"""
    blocks, cur, cur_type = [], [], None
    for raw in [l.strip() for l in (reward or "").split("\n") if l.strip()]:
        m = _TAG_PREFIX.match(raw)
        ltype = m.group(1) if m else "NORMAL"
        line = _TAG_PREFIX.sub("", raw).strip()
        if line.lower() == "or" or (cur_type and ltype != "NORMAL" and ltype != cur_type):
            if cur:
                blocks.append({"type": cur_type or "NORMAL", "lines": cur})
            cur, cur_type = [], None
        if line.lower() == "or":
            continue
        cur.append(line)
        if ltype != "NORMAL":
            cur_type = ltype
    if cur:
        blocks.append({"type": cur_type or "NORMAL", "lines": cur})
    return blocks


def _render_block(block, esc, scale, tint, s):
    px = max(10, round(14 * scale))
    small = max(9, round(12 * scale))
    parts, badge, conds, bullets = [], None, [], []
    for line in block["lines"]:
        m = _DIFF_RE.match(line)
        if m:
            badge = m.group(1)
            line = m.group(2) or ""
            if not line:
                continue
        if is_condition_line(line):
            conds.append(line)
        else:
            bullets.append(line)
    head = ""
    if badge:
        head += (f'<span style="background-color:{DIFF_BADGE_COLORS.get(badge, "#00e5ff")}; color:#111111; '
                 f'font-size:{small}px;">&nbsp;{esc(badge)}&nbsp;</span>')
    if conds:
        head += f'&nbsp;<span style="color:#ffb454; font-size:{small}px;">' + " &nbsp; ".join(
            ("💰 " if ("사용" in c or "소모" in c) else "📊 ") + esc(c) for c in conds) + "</span>"
    if head:
        parts.append(head)
    for b in bullets:
        parts.append(f'<span style="color:#ffffff; font-size:{px}px;">• {esc(b)}</span>')
    inner = "<br>".join(parts) or f'<span style="color:#888888; font-size:{small}px;">{esc(s["noinfo"])}</span>'
    if tint and block["type"] in _OUTCOME_STYLE:
        bg, border = _OUTCOME_STYLE[block["type"]]
        label = s["ok"] if block["type"] == "SUCCESS" else s["fail"]
        inner = (f'<div style="background-color:{bg}; border-left:3px solid {border}; padding:4px 8px;">'
                 f'<span style="color:{border}; font-size:{small}px;">{label}</span><br>{inner}</div>')
    return inner


def format_option_html(option, scale=1.0, show_info=True, lang="ko"):
    s = STR.get(lang, STR["ko"])
    """선택지 1개 -> 툴팁용 HTML 문자열."""
    esc = _html.escape
    name = re.sub(r"[\.\s]+$", "", option.get("choice", ""))
    star = "⭐ " if option.get("recommended") else ""
    title = f"💡 <b>{star}{esc(name)}</b>" if name else f"💡 <b>{esc(s['auto'])}</b>"
    variants = option.get("variants") or [{"sources": [], "reward": option.get("reward", ""), "info": option.get("info", [])}]
    body = []
    divider = '<hr style="border:none;border-top:1px solid #333333;margin:6px 0;">'
    for v in variants:
        blocks = _split_blocks(v.get("reward", ""))
        tint = any(b["type"] == "FAIL" for b in blocks)
        rendered = divider.join(f'<div style="margin-bottom:2px;">{_render_block(b, esc, scale, tint, s)}</div>' for b in blocks)
        if len(variants) > 1:
            src = v.get("sources") or []
            shown = ", ".join(src[:3]) + (" " + s["more"].format(len(src) - 3) if len(src) > 3 else "")
            rendered = f'<span style="color:#00e5ff; font-size:{max(9, round(12 * scale))}px;">🃏 {esc(shown)}</span><br>' + rendered
        body.append(rendered or "")
    html = title + '<hr style="border:none;border-top:1px solid #444444;margin:4px 0;">' + \
        '<hr style="border:none;border-top:1px dashed #555555;margin:6px 0;">'.join(body)
    if len(variants) > 1:
        html += ('<br><span style="color:#aaaaaa; font-size:%dpx;">%s</span>'
                 % (max(9, round(11 * scale)), esc(s["dup"])))
    infos = option.get("info") or []
    if show_info and infos:
        small = max(9, round(12 * scale))
        rows = []
        for inf in infos[:MAX_INFO_ITEMS]:
            rows.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{esc(inf["name"])}</b></span>'
                        f'<span style="color:#cfd8dc; font-size:{small}px;"> {esc(inf["desc"])}</span>')
        html += '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">' + "<br>".join(rows)
    if show_info:
        html += _render_item_sections(option, scale, s)
    return html


def format_ambiguous_html(items, scale=1.0, show_info=True, lang="ko"):
    """제목이 모호할 때: [(이벤트 제목, 선택지)] -> 제목 머리글을 붙여 이어 붙인 툴팁 HTML"""
    s = STR.get(lang, STR["ko"])
    small = max(9, round(12 * scale))
    parts = []
    for title, opt in items:
        parts.append(f'<span style="color:#ffd27f; font-size:{small + 1}px;"><b>◆ {_html.escape(title)}</b></span><br>'
                     + format_option_html(opt, scale, show_info, lang))
    sep = '<hr style="border:none;border-top:2px solid #777777;margin:8px 0;">'
    return (sep.join(parts) + f'<br><span style="color:#aaaaaa; font-size:{max(9, round(11 * scale))}px;">{_html.escape(s["ambig"])}</span>')


MAX_USES = 3
MAX_ITEM_GROUPS = 2


def _ref_line(r, with_result):
    parts = [r.get("event", "")]
    if r.get("timing"):
        parts.append(r["timing"])
    if r.get("choice"):
        parts.append(r["choice"])
    line = " · ".join(p for p in parts if p)
    if with_result and r.get("result"):
        line += " → " + r["result"]
    return line


def _render_item_sections(option, scale, s):
    """획득 아이템의 이후 사용처 / 소모 아이템의 획득처 섹션 (없으면 빈 문자열)"""
    esc = _html.escape
    small = max(9, round(12 * scale))
    out = ""
    groups = option.get("item_use") or []
    if groups:
        lines = [f'<span style="color:#ffd27f; font-size:{small}px;"><b>{esc(s["use_head"])}</b></span>']
        for g in groups[:MAX_ITEM_GROUPS]:
            lines.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{esc(g["item"])}</b></span>')
            uses = g.get("uses") or []
            if not uses:
                lines.append(f'<span style="color:#888888; font-size:{small}px;">{esc(s["use_none"])}</span>')
                continue
            for r in uses[:MAX_USES]:
                lines.append(f'<span style="color:#cfd8dc; font-size:{small}px;">• {esc(_ref_line(r, True))}</span>')
            if len(uses) > MAX_USES:
                lines.append(f'<span style="color:#888888; font-size:{small}px;">{esc(s["more_n"].format(len(uses) - MAX_USES))}</span>')
        if len(groups) > MAX_ITEM_GROUPS:
            lines.append(f'<span style="color:#888888; font-size:{small}px;">{esc(s["more_n"].format(len(groups) - MAX_ITEM_GROUPS))}</span>')
        out += '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">' + "<br>".join(lines)
    frm = [g for g in (option.get("item_from") or []) if g.get("from")]
    if frm:
        lines = []
        for g in frm[:MAX_ITEM_GROUPS]:
            srcs = g["from"]
            txt = " / ".join(_ref_line(r, False) for r in srcs[:2])
            if len(srcs) > 2:
                txt += " " + s["more_n"].format(len(srcs) - 2)
            lines.append(f'<span style="color:#a5d6a7; font-size:{small}px;">{esc(s["from_head"])}: '
                         f'<b>{esc(g["item"])}</b> ← {esc(txt)}</span>')
        out += '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">' + "<br>".join(lines)
    return out


# ----------------------------------------------------------------------------
# OCR 제목 -> 이벤트 매칭: 순서를 보는 편집거리 (한글은 자모 단위로 부분 점수)
# ----------------------------------------------------------------------------
_JUNK_COST = 0.4        # 질의 양끝의 군더더기 글자 (OCR 이 붙인 쓰레기) 비용
_MISS_COST = 0.8        # 이벤트 제목에는 있는데 OCR 에서 빠진 글자
_INS_COST = 1.0         # 질의 중간에 끼어든 글자


def _jamo(ch):
    o = ord(ch) - 0xAC00
    if 0 <= o < 11172:
        return (o // 588, (o % 588) // 28, o % 28)
    return None


def _char_cost(a, b):
    """글자 치환 비용 0(같음) ~ 1(전혀 다름). 한글은 초/중/종성이 일부 같으면 깎아준다 (순/손/소/스 등)"""
    if a == b:
        return 0.0
    ja, jb = _jamo(a), _jamo(b)
    if not ja or not jb:
        return 1.0
    e = [x == y for x, y in zip(ja, jb)]
    if e[0] and e[1]:
        return 0.3
    if e[0] and e[2] or e[1] and e[2]:
        return 0.45
    if e[0]:
        return 0.7
    if any(e):
        return 0.85
    return 1.0


def match_cost(q, c):
    n, m = len(q), len(c)
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = i * _JUNK_COST
    for j in range(1, m + 1):
        D[0][j] = j * _MISS_COST
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i][j] = min(D[i - 1][j - 1] + _char_cost(q[i - 1], c[j - 1]),
                          D[i - 1][j] + _INS_COST,
                          D[i][j - 1] + _MISS_COST)
    return min(D[i][m] + (n - i) * _JUNK_COST for i in range(n + 1))


def score_match(q, c):
    """0~1 유사도 (1 = 동일). q: OCR 글자열, c: 이벤트 제목(둘 다 clean_title 처리된 상태)"""
    if not c or not q:
        return 0.0
    return max(0.0, 1.0 - match_cost(q, c) / max(len(c), 2))


def _as_list(queries):
    if isinstance(queries, str):
        queries = [queries]
    seen, out = set(), []
    for q in queries or []:
        q = clean_title(q)
        if len(q) >= 2 and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def score_candidates(queries, c):
    """여러 OCR 후보 중 가장 잘 맞는 것 + 나머지 평균을 조금 반영"""
    sc = [score_match(q, c) for q in queries]
    if not sc:
        return 0.0
    best = max(sc)
    return best + 0.05 * (sum(sc) / len(sc))


def find_event_candidates(queries, events, min_score=0.5, top=5):
    """OCR 후보 문자열(들) -> [(event, score)] 점수 내림차순 (min_score 미만 제외)"""
    qs = _as_list(queries)
    out = []
    for ev in events or []:
        s = score_candidates(qs, ev.get("cleanTitle", ""))
        if s >= min_score:
            out.append((ev, s))
    out.sort(key=lambda x: -x[1])
    return out[:top]


AMBIG_MARGIN = 0.7      # 1등과의 차이가 이 글자 수(비용) 미만이면 모호
AMBIG_MAX = 3


def ambiguous_set(cands, margin=AMBIG_MARGIN, max_n=AMBIG_MAX):
    """1등과의 차이(점수차 x 제목 길이 = 글자 단위 비용)가 margin 미만인 후보들 (최대 max_n 개). 1개뿐이면 확신."""
    if not cands:
        return []
    top, n = cands[0][1], max(len(cands[0][0].get("cleanTitle", "")), 2)
    return [c for c in cands if (top - c[1]) * n < margin][:max_n]


def find_best_event(clean_query, events, min_score=0.5):
    """OCR 제목(문자열 또는 후보 리스트) -> (가장 비슷한 캐시 이벤트, 점수). 동점이면 먼저 나온 것"""
    c = find_event_candidates(clean_query, events, min_score, top=1)
    return c[0] if c else (None, 0.0)

def match_title(queries, events):
    """OCR 후보 -> (candidates, ambiguous). candidates=[(event, score)...] 상위 5개, ambiguous=1등과 구분이 안 되는 이벤트 리스트
    (길이 1이면 확신, 0이면 DB 에 없음)"""
    cands = find_event_candidates(queries, events)
    return cands, [ev for ev, _ in ambiguous_set(cands)]
