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
DB_FILES = OrderedDict([
    ("journey", "journey/ko/journey.json"),
    ("arcanas", "arcanas/arcanas.json"),
])
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
                 clock=time.time, user_agent=DEFAULT_USER_AGENT):
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
        return all(os.path.isfile(self._file_path(rel)) for rel in DB_FILES.values())

    def load(self):
        """(journey, arcanas) 반환. 없거나 깨졌으면 (None, None). arcanas는 없어도 동작한다."""
        out = {}
        for key, rel in DB_FILES.items():
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
        for key, rel in DB_FILES.items():
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
    return re.sub(r"[^가-힣0-9a-zA-Z]", "", s or "")


def _fmt_num(n):
    if isinstance(n, float) and n == int(n):
        n = int(n)
    return f"+{n}" if n >= 0 else str(n)


class InfoIndex:
    """잠재력/여정 버프 이름 -> 설명 (arcanas.json)"""

    def __init__(self, arcanas=None):
        self.potentials, self.buffs = {}, {}
        for p in (arcanas or {}).get("potentials", []) or []:
            self.potentials[p.get("id")] = p
        for b in (arcanas or {}).get("journeyBuffs", []) or []:
            self.buffs[b.get("id")] = b
        self.by_name = {}
        for kind, src in (("잠재력", self.potentials), ("여정 버프", self.buffs)):
            for obj in src.values():
                name = (obj.get("name") or {}).get("ko")
                desc = strip_tags((obj.get("description") or {}).get("ko", ""))
                if name and desc:
                    self.by_name.setdefault(name, {"name": name, "desc": desc, "kind": kind})
        self._names = sorted(self.by_name, key=len, reverse=True)

    def pname(self, pid):
        return ((self.potentials.get(pid) or {}).get("name") or {}).get("ko", "")

    def pdesc(self, pid):
        return strip_tags(((self.potentials.get(pid) or {}).get("description") or {}).get("ko", ""))

    def bname(self, bid):
        return ((self.buffs.get(bid) or {}).get("name") or {}).get("ko", "")

    def bdesc(self, bid):
        return strip_tags(((self.buffs.get(bid) or {}).get("description") or {}).get("ko", ""))

    def find_in_text(self, text):
        """표 형태 이벤트의 보상 문자열에서 알려진 잠재력/버프 이름을 찾아 설명을 돌려준다."""
        found, rest = [], text
        for name in self._names:
            if name in rest and (len(name) >= 3 or "버프" in text):
                found.append(self.by_name[name])
                rest = rest.replace(name, " ")
        return found


_COND_WORDS = ("사용", "소모", "필요")


def is_condition_line(line):
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
        return f"여정 버프 {name} {value}".strip(), ({"name": name, "desc": desc, "kind": "여정 버프"} if desc else None)
    if t == "RT_SE_POTEN":
        name = label or (idx.pname(x.get("sourceId")) if idx else "")
        if not desc and idx is not None:
            desc = idx.pdesc(x.get("sourceId"))
        return f"잠재력 {name}".strip(), ({"name": name, "desc": desc, "kind": "잠재력"} if desc else None)
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
        e["label"] = (r.get("statName") or {}).get("ko", "")
    elif t == "RT_STAMINA":
        e["label"] = "스태미나"
    elif t == "RT_CONDITION":
        e["label"] = "컨디션"
    elif t == "RT_POTEN_POINT":
        e["label"] = "잠재력 포인트"
    elif t == "RT_SE_POTEN":
        e["label"] = idx.pname(r.get("rewardId"))
        e["value"] = ""
    elif t == "RT_JOURNEY_BUFF":
        name = idx.bname(r.get("rewardId")) or (r.get("journeyBuffName") or {}).get("ko", "") \
            if isinstance(r.get("journeyBuffName"), dict) else (idx.bname(r.get("rewardId")) or r.get("journeyBuffName") or "")
        e["label"] = f"여정 버프 획득 · {name}"
        e["value"] = f"{mn}턴" if mn is not None else ""
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
        aname = (a.get("name") or {}).get("ko", "")
        for ev in a.get("events", []) or []:
            opts = []
            for ch in ev.get("choices", []) or []:
                conv = lambda groups: [[_raw_entry_to_journey_entry(r, idx) for r in grp] for grp in groups or []]
                opts.append(_choice_to_option((ch.get("name") or {}).get("ko", ""), conv(ch.get("successRewards")),
                                              conv(ch.get("failureRewards")), idx))
            out.append((a.get("id"), (ev.get("name") or {}).get("ko", ""), aname, opts))
    return out


_NUM_PREFIX = re.compile(r"^\s*\d+\s*[.)]\s*")


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
        vlist = list(variants.values())
        merged.append({
            "choice": first["choice"],
            "reward": vlist[0]["reward"],
            "has_condition": any(is_condition_line(re.sub(r"^\[(SUCCESS|FAIL)\]\s*", "", l))
                                 for v in vlist for l in v["reward"].split("\n")),
            "recommended": first.get("recommended", False),
            "info": info_union,
            "variants": vlist,
        })
    return merged


def build_event_cache(journey, arcanas=None):
    """journey.json (+ arcanas.json) -> db_event_cache 리스트."""
    idx = InfoIndex(arcanas)
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
        })
    return cache


# ----------------------------------------------------------------------------
# 툴팁 HTML
# ----------------------------------------------------------------------------
DIFF_BADGE_COLORS = {"이지": "#3ddc84", "노말": "#ffcc4d", "하드": "#ff6b6b"}
_DIFF_RE = re.compile(r"^(이지|노말|하드)(?:\s+(.*))?$")
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


def _render_block(block, esc, scale, tint):
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
    inner = "<br>".join(parts) or f'<span style="color:#888888; font-size:{small}px;">(정보 없음)</span>'
    if tint and block["type"] in _OUTCOME_STYLE:
        bg, border = _OUTCOME_STYLE[block["type"]]
        label = "성공" if block["type"] == "SUCCESS" else "실패"
        inner = (f'<div style="background-color:{bg}; border-left:3px solid {border}; padding:4px 8px;">'
                 f'<span style="color:{border}; font-size:{small}px;">{label}</span><br>{inner}</div>')
    return inner


def format_option_html(option, scale=1.0, show_info=True):
    """선택지 1개 -> 툴팁용 HTML 문자열."""
    esc = _html.escape
    name = re.sub(r"[\.\s]+$", "", option.get("choice", ""))
    star = "⭐ " if option.get("recommended") else ""
    title = f"💡 <b>{star}{esc(name)}</b>" if name else "💡 <b>자동 진행</b>"
    variants = option.get("variants") or [{"sources": [], "reward": option.get("reward", ""), "info": option.get("info", [])}]
    body = []
    divider = '<hr style="border:none;border-top:1px solid #333333;margin:6px 0;">'
    for v in variants:
        blocks = _split_blocks(v.get("reward", ""))
        tint = any(b["type"] == "FAIL" for b in blocks)
        rendered = divider.join(f'<div style="margin-bottom:2px;">{_render_block(b, esc, scale, tint)}</div>' for b in blocks)
        if len(variants) > 1:
            src = v.get("sources") or []
            shown = ", ".join(src[:3]) + (f" 외 {len(src) - 3}" if len(src) > 3 else "")
            rendered = f'<span style="color:#00e5ff; font-size:{max(9, round(12 * scale))}px;">🃏 {esc(shown)}</span><br>' + rendered
        body.append(rendered or "")
    html = title + '<hr style="border:none;border-top:1px solid #444444;margin:4px 0;">' + \
        '<hr style="border:none;border-top:1px dashed #555555;margin:6px 0;">'.join(body)
    if len(variants) > 1:
        html += ('<br><span style="color:#aaaaaa; font-size:%dpx;">※ 같은 제목의 이벤트가 여러 아르카나에 있습니다</span>'
                 % max(9, round(11 * scale)))
    infos = option.get("info") or []
    if show_info and infos:
        small = max(9, round(12 * scale))
        rows = []
        for inf in infos[:MAX_INFO_ITEMS]:
            rows.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{esc(inf["name"])}</b></span>'
                        f'<span style="color:#cfd8dc; font-size:{small}px;"> {esc(inf["desc"])}</span>')
        html += '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">' + "<br>".join(rows)
    return html


def find_best_event(clean_query, events, min_score=0.35):
    """OCR 제목 -> 가장 비슷한 캐시 이벤트 (원본 헬퍼의 점수식 유지)"""
    best, best_score = None, 0.0
    for ev in events:
        score = score_match(clean_query, ev.get("cleanTitle", ""))
        if score > best_score and score >= min_score:
            best, best_score = ev, score
    return best, best_score


def score_match(q, c):
    if not c or not q:
        return 0.0
    if c in q or q in c:
        return min(len(q), len(c)) / max(len(q), len(c)) + 0.35
    mc = sum(1 for ch in q if ch in c)
    if mc / len(q) < 0.5 or mc / len(c) < 0.3:
        return 0.0
    return (mc / len(q) + mc / len(c)) / 2.0
