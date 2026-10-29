# -*- coding: utf-8 -*-
"""
jh_db.py - 여정 내비게이터 데이터 계층 (GUI/OCR 의존성 없음)

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

import jh_common
import jh_net
import jh_paths
from jh_version import APP_VERSION

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
           "more_n": "외 {}건", "from_head": "📥 획득처", "or": "또는", "custom": "사용자 안내",
           "notfound": "📭 DB에 없는 이벤트: {}",
           "ambig": "※ 제목이 비슷해 확정하지 못했습니다 - 화면의 제목과 맞는 것을 보세요"},
    "en": {"buff": "Journey Buff", "poten": "Potential", "auto": "Automatic", "ok": "Success", "fail": "Failure",
           "noinfo": "(no info)", "dup": "* Several arcanas have an event with this title", "more": "+{} more",
           "stamina": "Stamina", "cond": "Condition", "pp": "Potential Point", "turn": "{} Turn",
           "use_head": "📦 Later uses of this item", "use_none": "No later use found / unknown",
           "more_n": "+{} more", "from_head": "📥 Obtained from", "or": "OR", "custom": "Custom note",
           "notfound": "📭 Event not in DB: {}",
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
MAX_JSON_BYTES = 20 * 1024 * 1024     # DB/V2 JSON 응답 크기 상한(실제는 수 MB 이하)
DEFAULT_USER_AGENT = f"JourneyNavigator/{APP_VERSION} (+https://github.com/QooReche/starsavior-journey-lite; personal game helper)"

UpdateResult = namedtuple("UpdateResult", "status changed message")

_DEFAULT = object()
# 시드 파일 이름 (assets/seed 아래, 캐시의 상대 경로에서 언어 폴더만 뺀 것)
SEED_NAMES = {"journey": "journey.json", "arcanas": "arcanas.json"}
_GUARD_LIST_KEY = {"journey": "events", "arcanas": "arcanas"}


def validate_db_file(kind, data):
    """journey / arcanas 파일 1개의 최소 구조 검증(앱·make_seed 공용). 맞지 않으면 ValueError."""
    key = _GUARD_LIST_KEY.get(kind)
    ok = isinstance(data, dict) and key is not None and isinstance(data.get(key), list) and bool(data.get(key))
    if not ok:
        raise ValueError(f"{kind}: unexpected structure")


# ----------------------------------------------------------------------------
# 캐시 / 조건부 갱신
# ----------------------------------------------------------------------------
def default_cache_dir():
    return jh_paths.cache_dir()


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
                 clock=time.time, user_agent=DEFAULT_USER_AGENT, lang="ko", seed_dir=_DEFAULT):
        self.lang = lang if lang in SUPPORTED_LANGS else "ko"
        # 번들 시드: 사용자 캐시가 없을 때만 읽는 폴백(한국어만). seed_dir=None 이면 사용 안 함
        self.seed_dir = (jh_paths.seed_dir() if seed_dir is _DEFAULT else seed_dir) if self.lang == "ko" else None
        self.seed_used = False          # 마지막 load() 가 시드 파일을 하나라도 썼는지
        self.v2_seed_used = False       # 마지막 load_v2() 가 시드를 썼는지
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
        meta = {"last_check": 0, "last_fail": 0, "last_manual": 0, "files": {},
                "consecutive_fail": 0, "next_retry": 0, "manual_bypass": False}
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                meta.update({k: saved[k] for k in meta if k in saved})
        except Exception:
            pass
        for k in ("last_check", "last_fail", "last_manual", "next_retry"):      # 값이 손상돼도 update() 가 예외를 내지 않게
            v = meta.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                meta[k] = 0
        cf = meta.get("consecutive_fail")
        meta["consecutive_fail"] = cf if isinstance(cf, int) and not isinstance(cf, bool) and cf > 0 else 0
        meta["manual_bypass"] = meta.get("manual_bypass") is True
        files = meta.get("files")
        meta["files"] = {k: v for k, v in files.items() if isinstance(v, dict)} if isinstance(files, dict) else {}
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
    @staticmethod
    def _read_valid(path, kind):
        """JSON 을 읽고 구조까지 맞으면 데이터, 아니면 None"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            validate_db_file(kind, data)
            return data
        except Exception:
            return None

    def _kind_of(self, key):
        return key        # self.files 의 키가 곧 종류('journey'/'arcanas')

    def has_cache(self):
        """사용자 캐시(시드 아님)가 모두 있고 구조가 유효한가"""
        return all(self._read_valid(self._file_path(rel), key) is not None for key, rel in self.files.items())

    def _seed_data(self, key):
        if not self.seed_dir:
            return None
        return self._read_valid(os.path.join(self.seed_dir, SEED_NAMES[key]), key)

    def load(self):
        """(journey, arcanas) 반환. 캐시 우선, 캐시가 없거나 깨졌을 때만 번들 시드(ko). 없으면 None.
        시드는 메모리로만 읽는다(캐시/meta 를 만들지 않음)."""
        out, self.seed_used = {}, False
        for key, rel in self.files.items():
            data = self._read_valid(self._file_path(rel), key)
            if data is None:
                seed = self._seed_data(key)
                if seed is not None:
                    data, self.seed_used = seed, True
                else:             # 시드도 없으면 예전 동작: 구조 검증 없이 JSON 만 읽을 수 있으면 사용
                    try:
                        with open(self._file_path(rel), "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = None
            out[key] = data
        return out["journey"], out["arcanas"]

    @property
    def using_seed(self):
        return bool(self.seed_used or self.v2_seed_used)

    def seconds_until_manual(self):
        return max(0, int(self.meta.get("last_manual", 0) + MANUAL_COOLDOWN_SEC - self.clock()))

    # --- 갱신 ---
    def retry_at(self):
        """다음 시도 가능 시각. 구 meta(last_fail 만 있는 경우)도 10분 대기를 유지한다."""
        nr = self.meta.get("next_retry", 0)
        lf = self.meta.get("last_fail", 0)
        return max(nr, lf + FAIL_BACKOFF_SEC if lf else 0)

    def update(self, manual=False):
        now = self.clock()
        have = self.has_cache()
        retry = self.retry_at()
        if manual:
            remain = self.seconds_until_manual()
            if remain > 0:
                return UpdateResult("cooldown", False, f"수동 새로고침은 {remain // 60 + 1}분 뒤에 다시 가능합니다")
            if now < retry:
                # 수동은 사용자가 직접 누른 것이므로 backoff 한 번(기간당 1회)만 건너뛸 수 있다. 쿨다운(10분)은 그대로 적용.
                if self.meta.get("manual_bypass"):
                    return UpdateResult("backoff", False, f"사이트 응답 없음, {jh_net.minutes_left(retry, now)}분 뒤 재시도")
                self.meta["manual_bypass"] = True
            self.meta["last_manual"] = now
        else:
            if have and now - self.meta.get("last_check", 0) < CHECK_INTERVAL_SEC:
                return UpdateResult("skipped_recent", False, "최근에 확인함 (12시간 이내)")
            if now < retry:
                return UpdateResult("backoff", False, f"사이트 응답 없음, {jh_net.minutes_left(retry, now)}분 뒤 재시도")
            if not jh_net.get_limiter("db", jh_net.DB_ATTEMPTS_PER_DAY, 86400).allow(now):
                return UpdateResult("capped", False, f"오늘 자동 갱신 시도 상한({jh_net.DB_ATTEMPTS_PER_DAY}회) 도달, 중단")

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
            n = self.meta["consecutive_fail"] + 1
            if now >= retry:
                self.meta["manual_bypass"] = False      # 새 backoff 기간(자동 시도 실패)이면 수동 우회권 복구
            self.meta["consecutive_fail"] = n
            self.meta["last_fail"] = now
            self.meta["next_retry"] = now + jh_net.backoff_sec(n)
            self._save_meta()
            return UpdateResult("offline", changed, f"갱신 실패, 캐시 사용 (연속 실패 {n}회, {jh_net.backoff_sec(n) // 60}분 뒤 재시도): "
                                f"{type(failed).__name__} {str(failed)[:120]}".rstrip())
        self.meta["last_check"] = now
        self.meta["last_fail"] = 0
        self.meta["consecutive_fail"], self.meta["next_retry"], self.meta["manual_bypass"] = 0, 0, False
        self._save_meta()
        if changed:
            return UpdateResult("updated", True, "DB 갱신됨")
        return UpdateResult("not_modified", False, "변경 없음 (304)")

    def network_summary(self, now=None):
        """진단용 한 줄: DB / V2 연속 실패와 다음 재시도, 이미지 서킷 상태"""
        now = self.clock() if now is None else now

        def part(name, meta, retry):
            n = meta.get("consecutive_fail", 0)
            left = jh_net.minutes_left(retry, now)
            return f"{name} 연속 실패 {n}회" + (f"/다음 재시도 {left}분 후" if n and left else "")
        out = [part("DB", self.meta, self.retry_at())]
        try:
            import jh_v2
            v2 = jh_v2.V2Store(self.cache_dir, clock=self.clock)
            out.append(part("V2", v2.meta, v2.retry_at()))
        except Exception:
            out.append("V2 ?")
        try:
            st = jh_net.get_breaker().state(time.monotonic())
            lim = jh_net.get_limiter("card_dl", jh_net.IMAGES_PER_HOUR, 3600).used(time.monotonic())
            out.append(("이미지 서킷 열림(%d분 후 재개)" % st["minutes"] if st["open"] else "이미지 서킷 닫힘")
                       + f", 실패 {st['fails']}회, 시간당 다운로드 {lim}/{jh_net.IMAGES_PER_HOUR}")
        except Exception:
            out.append("이미지 ?")
        return "네트워크: " + ", ".join(out)

    # --- V2 보충 데이터 (jh_v2): 실패해도 가이드 DB 동작에 영향 없음 ---
    def load_v2(self):
        try:
            import jh_v2
            v2s = jh_v2.V2Store(self.cache_dir, session=self._session, user_agent=self.user_agent,
                                seed_dir=os.path.join(self.seed_dir, "v2") if self.seed_dir else None)
            out = v2s.load()
            self.v2_seed_used = bool(v2s.seed_used)
            return out
        except Exception:
            return None

    def update_v2(self, manual=False):
        """V2 파일 갱신. 예외를 내지 않고 UpdateResult 를 돌려준다."""
        try:
            import jh_v2
            return jh_v2.V2Store(self.cache_dir, session=self._session, clock=self.clock,
                                 user_agent=self.user_agent,
                                 seed_dir=os.path.join(self.seed_dir, "v2") if self.seed_dir else None).update(manual=manual)
        except Exception as e:
            return UpdateResult("offline", False, f"V2 갱신 예외: {type(e).__name__} {str(e)[:100]}")

    def update_reco(self, manual=False):
        """구원자 추천 데이터(savior_reco.json) 원격 갱신. 예외를 내지 않고 UpdateResult 를 돌려준다. 새 데이터는 다음 load_reco 부터."""
        try:
            import jh_reco
            return jh_reco.RecoStore(self.cache_dir, session=self._session, clock=self.clock,
                                     user_agent=self.user_agent).update(manual=manual)
        except Exception as e:
            return UpdateResult("offline", False, f"추천 데이터 갱신 예외: {type(e).__name__} {str(e)[:100]}")

    # 새 DB 사이트의 구조가 바뀌거나 내용이 크게 줄어든 응답이 정상 캐시를 덮어쓰지 않게 하는 안전장치
    _GUARD_KEYS = {"journey": ("events", "arcana"), "arcanas": ("arcanas", "potentials")}
    GUARD_MIN_RATIO = 0.5

    def _check_not_shrunk(self, rel, data):
        kind = "journey" if rel.startswith("journey/") else "arcanas" if rel.startswith("arcanas/") else None
        if kind is None:
            return
        # 기준: 유효한 사용자 캐시. 없으면 번들 시드를 '기존'으로 삼아 비교(시드 상태에서 장애로 빈약한 응답이 캐시가 되는 것을 막음)
        old = self._read_valid(self._file_path(rel), kind) or self._seed_data(kind)
        for key in self._GUARD_KEYS[kind]:
            new_v, old_v = data.get(key), (old or {}).get(key)
            if old_v is not None and isinstance(old_v, list) and len(old_v) >= 4:
                if not isinstance(new_v, list) or len(new_v) < len(old_v) * self.GUARD_MIN_RATIO:
                    raise ValueError(f"{rel}: '{key}' 항목이 {len(old_v)} -> {len(new_v) if isinstance(new_v, list) else new_v!r} 로 비정상 감소/변경, 캐시 유지")

    def _fetch_one(self, rel):
        url = self.base_url.rstrip("/") + "/" + rel
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        finfo = self.meta["files"].get(rel, {})
        kind = "journey" if rel.startswith("journey/") else "arcanas"
        if self._read_valid(self._file_path(rel), kind) is not None:       # 없거나 깨진 캐시/시드 상태: 조건부 요청 금지(304 오판 방지)
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
        if len(content) > MAX_JSON_BYTES:                 # 비정상적으로 큰 응답은 받지 않는다(메모리/디스크 보호)
            raise ValueError(f"response too large: {len(content)} bytes")
        data = json.loads(content.decode("utf-8"))      # 깨진 응답이 캐시를 덮어쓰지 않도록 먼저 검증
        if not isinstance(data, dict) or not data:
            raise ValueError("unexpected JSON")
        self._check_not_shrunk(rel, data)
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

    def __init__(self, arcanas=None, lang="ko", v2=None):
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
        if v2:                                  # V2 journey_buffs: 가이드에 없는 버프를 내장본보다 먼저 보충
            try:
                import jh_v2
                for name, desc in jh_v2.buff_desc_map(v2, self.lang).items():
                    self.by_name.setdefault(name, {"name": name, "desc": desc, "kind": self.s["buff"]})
            except Exception:
                pass
        try:                                    # 새 DB 에 없는 버프(예: 힘의 자양분)는 옛 DB 백업에서 추출한 설명으로 보충
            from jh_buffs import BUFF_DESC
            for name, desc in (BUFF_DESC.get(self.lang) or BUFF_DESC.get("ko") or {}).items():
                if name and desc:
                    self.by_name.setdefault(name, {"name": name, "desc": strip_tags(desc), "kind": self.s["buff"]})
        except Exception:
            pass
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
        "has_condition": any(is_condition_line(jh_common.TAG_PREFIX.sub("", l)) for l in lines),
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
_OR_SPLIT = jh_common.OR_SPLIT


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


_DIFF_RE_EARLY = jh_common.DIFF_WORD


_BUFF_INLINE_RE = re.compile(r"^(여정 버프 획득|Journey Buff (?:Gained|Obtained|Acquired))\s+(.+)$", re.I)
_BUFF_HEAD_RE = re.compile(r"^(여정 버프 획득|Journey Buff (?:Gained|Obtained|Acquired)?)$", re.I)


def _compact_result(lines):
    """옵션의 보상 줄들 -> 줄바꿈 구분 요약 (최대 6줄). '여정 버프 획득' 다음 줄은 '여정 버프 획득 (이름 +3턴)' 으로 합친다."""
    parts = []
    for l in lines:
        l = _TAG_PREFIX_EARLY.sub("", (l or "").strip())
        if not l or l.lower() == "or" or is_condition_line(l) or _CONSUME_RE.match(l) or l in ("없음", "None"):
            continue
        l = re.sub(r"\s+(?=[+-]\d)", "", l)
        if parts and _BUFF_HEAD_RE.match(parts[-1]):
            parts[-1] = f"{parts[-1]} ({l})"
            continue
        m = _BUFF_INLINE_RE.match(l)
        if m:                                   # '여정 버프 획득 힘의 자양분+3턴' -> '여정 버프 획득 (힘의 자양분+3턴)'
            parts.append(f"{m.group(1)} ({m.group(2)})")
            continue
        parts.append(l if len(l) <= 40 else l[:39] + "…")
    if len(parts) > 6:
        parts = parts[:6] + ["…"]
    return "\n".join(parts)


_TAG_PREFIX_EARLY = jh_common.TAG_PREFIX



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
        tones = cur.setdefault("tones", [])
        if text.lower() == "or":
            cur["lines"].append(("", "or"))
            tones.append("")
            continue
        lines = cur["lines"]
        if lines and lines[-1][0] == region and region and lines[-1][1].rstrip().endswith(" or"):
            lines[-1] = (region, lines[-1][1].rstrip() + " " + text)    # 셀 줄바꿈으로 갈라진 'A or B or' 이어붙임
        else:
            lines.append((region, text))
            # 난이도 머리 줄(이지/노말/하드 ...)·조건 줄(필요/사용/소모)의 톤은 난이도 색일 뿐 결과(성공/실패)가 아니다
            tones.append("" if (_DIFF_RE_EARLY.match(text) or is_condition_line(text)) else tone)

    out = []
    for o in options:
        texts = [t for _, t in o["lines"]]
        tones = o.get("tones") or []
        if len(tones) == len(texts) and "success" in tones and "failure" in tones:
            # 한 선택지에 성공/실패 톤이 함께 있으면 둘 중 하나가 나오는 결과다 -> [SUCCESS]/[FAIL] 표시로 블록을 나눈다
            texts = [(f"[SUCCESS] {t}" if tn == "success" else f"[FAIL] {t}" if tn == "failure" else t)
                     for t, tn in zip(texts, tones)]
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
            v = variants.setdefault(key, {"sources": [], "choice": o["choice"], "reward": o["reward"], "info": o["info"],
                                          "items": o.get("items") or []})
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


def build_event_cache(journey, arcanas=None, lang="ko", v2=None):
    """journey.json (+ arcanas.json) -> db_event_cache 리스트. v2(선택): jh_v2.V2Store.load() 결과로,
    가이드에 없는 제목 보충·버프 설명·아이템 설명에 쓴다(실패해도 가이드 결과는 그대로)."""
    idx = InfoIndex(arcanas, lang, v2=v2)
    groups = OrderedDict()      # cleanTitle -> {'raw','arcana','cands':[(src, opts)]}

    def add(title, is_arcana, src, opts, aid=None):
        ct = clean_title(title)
        if not ct:
            return
        g = groups.setdefault(ct, {"raw": title, "arcana": is_arcana, "cands": [], "ids": {}})
        g["cands"].append((src, opts))
        if aid is not None and src:
            g["ids"][src] = aid

    journey = journey or {}
    jr_ids = set()
    for a in journey.get("arcana", []) or []:
        jr_ids.add(a.get("arcanaId"))
    for aid, title, aname, opts in _arcana_events_from_journey(journey, idx):
        add(title, True, aname, opts, aid)
    for aid, title, aname, opts in _arcana_events_from_raw(arcanas, idx, jr_ids):
        add(title, True, aname, opts, aid)
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
            "arcanaIds": dict(g["ids"]),        # 출처(아르카나 이름) -> arcanaId (카드 식별용)
            "timing": "" if g["arcana"] else next((s for s, _ in g["cands"] if s), ""),
        })
    if v2:
        try:
            import jh_v2
            jh_v2.supplement_cache(cache, v2, lang)
        except Exception:
            pass
    link_items(cache)
    if v2:
        try:
            import jh_v2
            jh_v2.attach_item_desc(cache, v2, lang)
        except Exception:
            pass
    return cache


# ----------------------------------------------------------------------------
# 사용자 정의 이벤트 (custom_events.json): DB 에 없는 이벤트에 직접 안내문을 붙인다
# ----------------------------------------------------------------------------
CUSTOM_EVENTS_FILE = "custom_events.json"
DEFAULT_CUSTOM_EVENTS = {
    "운명이 이어 준 첫 번째 인연": ("초반에 가져온 인자 2명의 효과가 적용되는 타이밍입니다. "
                           "보상은 선택한 인자에 따라 달라집니다 (관찰 예: 힘 +85)."),
}


def load_custom_events(path, create_default=True):
    """path 의 JSON 을 읽어 {제목: 안내문 | {"note": 안내문, "arcana": bool}} 로 돌려준다.
    파일이 없으면 기본 항목으로 새로 만들고(create_default), 깨진 파일은 무시한다(기본 항목만 반환, 덮어쓰지 않음)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(DEFAULT_CUSTOM_EVENTS)
    except FileNotFoundError:
        if create_default:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(DEFAULT_CUSTOM_EVENTS, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return dict(DEFAULT_CUSTOM_EVENTS)
    except Exception:
        return dict(DEFAULT_CUSTOM_EVENTS)


def merge_custom_events(cache, customs):
    """DB 에 같은 제목이 이미 있으면 DB 를 우선하고, 없는 것만 캐시 뒤에 붙인다. -> 새 리스트"""
    out = list(cache or [])
    have = {e.get("cleanTitle") for e in out}
    for title, val in (customs or {}).items():
        note = val.get("note", "") if isinstance(val, dict) else val
        ct = clean_title(str(title))
        if not ct or ct in have or not isinstance(note, str) or not note.strip():
            continue
        arcana = bool(val.get("arcana")) if isinstance(val, dict) else False
        out.append({"rawTitle": str(title), "cleanTitle": ct, "isArcana": arcana, "custom": True,
                    "options": [{"choice": "", "reward": "", "has_condition": False, "recommended": False,
                                 "info": [], "items": [], "result": "", "note": note.strip()}],
                    "sources": [], "timing": ""})
        have.add(ct)
    return out


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
                           "info": o.get("info") or [], "kind": it["role"], "key": ev.get("cleanTitle", "")}
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
_DIFF_RE = jh_common.DIFF_LINE
_TAG_PREFIX = jh_common.TAG_PREFIX
_OUTCOME_STYLE = {"SUCCESS": ("rgba(61,220,132,0.16)", "#3ddc84"), "FAIL": ("rgba(255,92,92,0.16)", "#ff5c5c")}
MAX_INFO_ITEMS = 6


def _split_blocks(reward):
    """보상 문자열 -> [{'type': SUCCESS|FAIL|NORMAL, 'lines': [...]}] ('or' 와 성공/실패 전환 기준)"""
    blocks, cur, cur_type, grp = [], [], None, 0
    for raw in [l.strip() for l in (reward or "").split("\n") if l.strip()]:
        m = _TAG_PREFIX.match(raw)
        ltype = m.group(1) if m else "NORMAL"
        line = _TAG_PREFIX.sub("", raw).strip()
        if line.lower() == "or" or (cur_type and ltype != "NORMAL" and ltype != cur_type):
            if cur:
                blocks.append({"type": cur_type or "NORMAL", "lines": cur, "grp": grp})
            cur, cur_type = [], None
        if line.lower() == "or":
            grp += 1                                # 'or' 로 갈리는 랜덤 결과 묶음 번호 (성공/실패 쌍은 같은 묶음)
            continue
        cur.append(line)
        if ltype != "NORMAL":
            cur_type = ltype
    if cur:
        blocks.append({"type": cur_type or "NORMAL", "lines": cur, "grp": grp})
    return blocks


def _render_block(block, esc, scale, tint, s, inline=False):
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
    if inline and len(bullets) > 1:             # 압축 모드: 보상을 한 줄로 이어 붙여 높이를 줄인다
        parts.append(f'<span style="color:#ffffff; font-size:{px}px;">{esc(" · ".join(bullets))}</span>')
    else:
        for b in bullets:
            parts.append(f'<span style="color:#ffffff; font-size:{px}px;">• {esc(b)}</span>')
    inner = "<br>".join(parts) or f'<span style="color:#888888; font-size:{small}px;">{esc(s["noinfo"])}</span>'
    if tint and block["type"] in _OUTCOME_STYLE:
        bg, border = _OUTCOME_STYLE[block["type"]]
        label = s["ok"] if block["type"] == "SUCCESS" else s["fail"]
        inner = (f'<div style="background-color:{bg}; border-left:3px solid {border}; padding:4px 8px;">'
                 f'{jh_common.span(label, border, small)}<br>{inner}</div>')
    return inner


DIFF_KEYS = {"이지": "easy", "노말": "normal", "하드": "hard", "Easy": "easy", "Normal": "normal", "Hard": "hard"}
DIFFICULTIES = ("all", "easy", "normal", "hard")
MAX_BLOCKS_COMPACT = 6      # detail 2 에서 변형 하나당 보여 줄 최대 블록 수 (보상은 한 줄로 합쳐 높이를 줄인다)
MAX_VARIANTS_COMPACT = 2


def block_difficulty(block):
    """블록 첫 줄의 난이도 머리말('이지/노말/하드') -> 'easy'/'normal'/'hard' (없으면 None)"""
    m = _DIFF_RE.match(block["lines"][0]) if block.get("lines") else None
    return DIFF_KEYS.get(m.group(1)) if m else None


def hidden_set(difficulty):
    """difficulty 인자 -> 숨길 난이도 집합.
    문자열('all'|'easy'|'normal'|'hard')이면 '그 난이도만 표시', 리스트/집합이면 원작 1.1.3 의
    hidden_difficulties 와 같은 '숨길 난이도 목록'으로 해석한다(알 수 없는 값은 무시)."""
    if isinstance(difficulty, str):
        if difficulty not in DIFFICULTIES or difficulty == "all":
            return set()
        return {d for d in DIFFICULTIES[1:] if d != difficulty}
    return {d for d in (difficulty or ()) if d in DIFFICULTIES[1:]}


def filter_blocks_by_difficulty(blocks, difficulty):
    """숨길 난이도가 명시된 블록을 걸러낸다. 난이도 표시가 없는 블록(랜덤 결과 등)은 항상 유지.
    걸러 낸 결과가 비면 원본을 돌려준다. difficulty: 표시할 난이도 문자열 또는 숨길 난이도 목록."""
    hidden = hidden_set(difficulty)
    if not hidden:
        return blocks
    kept = [b for b in blocks if block_difficulty(b) not in hidden]
    return kept if kept else blocks


def format_option_html(option, scale=1.0, show_info=True, lang="ko", difficulty="all", detail=0):
    """선택지 1개 -> 툴팁용 HTML 문자열. difficulty: 'all'|'easy'|'normal'|'hard' 또는 숨길 난이도 목록.
    detail: 0 전체, 1 설명/아이템 섹션 생략, 2 변형·블록 수도 제한(화면 아래가 잘릴 때 단계적으로 줄임)."""
    s = STR.get(lang, STR["ko"])
    esc = _html.escape
    name = re.sub(r"[\.\s]+$", "", option.get("choice", ""))
    star = "⭐ " if option.get("recommended") else ""
    title = f"💡 <b>{star}{esc(name)}</b>" if name else f"💡 <b>{esc(s['auto'])}</b>"
    if option.get("note"):                      # custom_events.json 의 안내문
        head = f"📝 <b>{esc(name or s['custom'])}</b>"
        body_txt = esc(option["note"]).replace(chr(10), "<br>")
        return (head + '<hr style="border:none;border-top:1px solid #444444;margin:4px 0;">'
                + jh_common.span(body_txt, "#ffffff", max(10, round(14 * scale))))
    variants = option.get("variants") or [{"sources": [], "reward": option.get("reward", ""), "info": option.get("info", [])}]
    body = []
    divider = '<hr style="border:none;border-top:1px solid #333333;margin:6px 0;">'
    hidden_variants = 0
    if detail >= 2 and len(variants) > MAX_VARIANTS_COMPACT:
        hidden_variants = len(variants) - MAX_VARIANTS_COMPACT
        variants = variants[:MAX_VARIANTS_COMPACT]
    multi = len(variants) + hidden_variants > 1
    small_c = max(9, round(12 * scale))
    for v in variants:
        blocks = filter_blocks_by_difficulty(_split_blocks(v.get("reward", "")), difficulty)
        tint = any(b["type"] == "FAIL" for b in blocks)
        hidden_blocks = 0
        if detail >= 2 and len(blocks) > MAX_BLOCKS_COMPACT:
            hidden_blocks = len(blocks) - MAX_BLOCKS_COMPACT
            blocks = blocks[:MAX_BLOCKS_COMPACT]
        pieces = []
        for i, b in enumerate(blocks):
            if i:
                # 난이도 표시도 성공/실패 구분도 없는 'or' 블록 = 같은 난이도에서 나올 수 있는 랜덤 결과 중 하나
                if (b.get("grp", 0) != blocks[i - 1].get("grp", 0)
                        and block_difficulty(b) is None and block_difficulty(blocks[i - 1]) is None):
                    pieces.append(f'<div style="color:#ffd27f; font-size:{small_c}px; text-align:center; margin:2px 0;">'
                                  f'── {esc(s["or"])} ──</div>')
                else:
                    pieces.append(divider)
            pieces.append(f'<div style="margin-bottom:2px;">{_render_block(b, esc, scale, tint, s, inline=detail >= 2)}</div>')
        if hidden_blocks:
            pieces.append(f'<span style="color:#888888; font-size:{small_c}px;">…{esc(s["more_n"].format(hidden_blocks))}</span>')
        rendered = "".join(pieces)
        if multi:
            src = v.get("sources") or []
            shown = ", ".join(src[:3]) + (" " + s["more"].format(len(src) - 3) if len(src) > 3 else "")
            rendered = f'<span style="color:#00e5ff; font-size:{max(9, round(12 * scale))}px;">🃏 {esc(shown)}</span><br>' + rendered
        body.append(rendered or "")
    if hidden_variants:
        body.append(f'<span style="color:#888888; font-size:{small_c}px;">…{esc(s["more_n"].format(hidden_variants))}</span>')
    html = title + '<hr style="border:none;border-top:1px solid #444444;margin:4px 0;">' + \
        '<hr style="border:none;border-top:1px dashed #555555;margin:6px 0;">'.join(body)
    if multi:
        html += ('<br><span style="color:#aaaaaa; font-size:%dpx;">%s</span>'
                 % (max(9, round(11 * scale)), esc(s["dup"])))
    infos = option.get("info") or []
    if show_info and infos and detail < 1:
        small = max(9, round(12 * scale))
        rows = []
        for inf in infos[:MAX_INFO_ITEMS]:
            rows.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{esc(inf["name"])}</b></span>'
                        f'<span style="color:#cfd8dc; font-size:{small}px;"> {esc(inf["desc"])}</span>')
        html += '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">' + "<br>".join(rows)
    if show_info and detail < 1:
        html += _render_item_sections(option, scale, s)
    if show_info and lang == "ko" and detail < 2:       # '훈련의 방향성': 선택지별 특수 잠재력(AX, CX ...) 안내 (한국어 전용)
        try:
            import jh_relic
            html += jh_relic.section(option, scale, detail)
        except Exception:
            pass
    return html


def format_not_found_html(title, scale=1.0, lang="ko"):
    """DB 에 없는 이벤트: 작은 중립 안내"""
    s = STR.get(lang, STR["ko"])
    return (f'<span style="color:#b0bec5; font-size:{max(9, round(12 * scale))}px;">'
            f'{_html.escape(s["notfound"].format(title))}</span>')


def format_ambiguous_html(items, scale=1.0, show_info=True, lang="ko", difficulty="all", detail=0):
    """제목이 모호할 때: [(이벤트 제목, 선택지)] -> 제목 머리글을 붙여 이어 붙인 툴팁 HTML"""
    s = STR.get(lang, STR["ko"])
    small = max(9, round(12 * scale))
    parts = []
    for title, opt in items:
        parts.append(f'<span style="color:#ffd27f; font-size:{small + 1}px;"><b>◆ {_html.escape(title)}</b></span><br>'
                     + format_option_html(opt, scale, show_info, lang, difficulty, detail))
    sep = '<hr style="border:none;border-top:2px solid #777777;margin:8px 0;">'
    return (sep.join(parts) + f'<br><span style="color:#aaaaaa; font-size:{max(9, round(11 * scale))}px;">{_html.escape(s["ambig"])}</span>')


MAX_USES = 3
HIDDEN_ITEM_PREFIX = "스탯 인자"
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


def _use_lines(r, small):
    """사용처 1건 -> 식별하기 좋게 줄을 나눈 HTML 줄 목록: '• 이벤트 · 시기' / '  ↳ 선택지' / '  → 보상' (보상은 한 줄씩)"""
    esc = _html.escape
    sp = '&nbsp;&nbsp;&nbsp;'
    span = lambda t: jh_common.span(t, "#cfd8dc", small)
    head = " · ".join(p for p in (r.get("event", ""), r.get("timing", "")) if p)
    out = [span("• " + esc(head))]
    if r.get("choice"):
        out.append(span(sp + "↳ " + esc(r["choice"])))
    for k, part in enumerate(x for x in (r.get("result") or "").split("\n") if x):
        out.append(f'<span style="color:#a5d6a7; font-size:{small}px;">{sp}{"→ " if k == 0 else "&nbsp;&nbsp;"}{esc(part)}</span>')
    for inf in (r.get("info") or [])[:3]:       # 보상에 나온 버프/잠재력의 효과 설명
        out.append(f'<span style="color:#9fe8ff; font-size:{small}px;">{sp}ℹ <b>{esc(inf["name"])}</b></span>'
                   f'<span style="color:#90a4ae; font-size:{small}px;"> {esc(inf["desc"])}</span>')
    return out


def _render_item_sections(option, scale, s):
    """획득 아이템의 이후 사용처 / 소모 아이템의 획득처 섹션 (없으면 빈 문자열)"""
    esc = _html.escape
    small = max(9, round(12 * scale))
    out = ""
    # 스탯 인자(II/III 등)는 인자 계승 단계에서 고르는 항목이라 숨기고, 이후 사용처가 없는 아이템도 표시하지 않는다
    groups = [g for g in (option.get("item_use") or [])
              if g.get("uses") and not g.get("item", "").startswith(HIDDEN_ITEM_PREFIX)]
    if groups:
        lines = [f'<span style="color:#ffd27f; font-size:{small}px;"><b>{esc(s["use_head"])}</b></span>']
        for g in groups[:MAX_ITEM_GROUPS]:
            lines.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{esc(g["item"])}</b></span>')
            uses = g.get("uses") or []
            if not uses:
                lines.append(f'<span style="color:#888888; font-size:{small}px;">{esc(s["use_none"])}</span>')
                continue
            for r in uses[:MAX_USES]:
                lines.extend(_use_lines(r, small))
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


MIN_LEN_RATIO = 0.6     # 질의와 제목의 길이 비율이 이보다 작으면 다른 제목


def score_match(q, c):
    """0~1 유사도 (1 = 동일). q: OCR 글자열, c: 이벤트 제목(둘 다 clean_title 처리된 상태)"""
    if not c or not q:
        return 0.0
    if min(len(q), len(c)) / max(len(q), len(c)) < MIN_LEN_RATIO:    # '운명' 이 긴 문장 안에 들어있다고 매칭되지 않도록
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


def filter_by_kind(events, kind):
    """kind: 'arcana'(아르카나 이벤트 라벨) -> 아르카나 이벤트만, 'general'(여정/훈련 라벨) -> 일반 이벤트만, 그 외 -> 전체"""
    if kind == "arcana":
        return [e for e in events if e.get("isArcana")]
    if kind == "general":
        return [e for e in events if not e.get("isArcana")]
    return events


def arcana_ids_of(event):
    """이벤트 제목을 가진 아르카나들의 id (중복 제거, 순서 유지). 정보가 없으면 빈 리스트."""
    return list(dict.fromkeys((event.get("arcanaIds") or {}).values()))


def filter_event_by_arcana(event, arcana_id):
    """같은 제목이 여러 아르카나에 있을 때 arcana_id 의 변형만 남긴 이벤트 사본. 걸러낼 수 없으면 원본 그대로."""
    names = {n for n, i in (event.get("arcanaIds") or {}).items() if i == arcana_id}
    if not names:
        return event
    options = []
    for o in event.get("options", []):
        vs = []
        for v in o.get("variants") or []:
            keep = [x for x in v.get("sources", []) if x in names]
            if keep:
                vs.append(dict(v, sources=keep))
        if not vs:
            options.append(o)
            continue
        # info/items/사용처는 변형을 합친 값이라, 남긴 변형 것만 다시 모은다(다른 아르카나 버전의 버프 설명이 남는 문제: 수프 이벤트 '자신감')
        info, items = [], []
        for v in vs:
            for inf in v.get("info") or []:
                if all(x["name"] != inf["name"] for x in info):
                    info.append(inf)
            for it in v.get("items") or []:
                if it not in items:
                    items.append(it)
        item_names = {it.get("name") for it in items}
        extra = {}
        for key in ("item_use", "item_from"):
            if key in o:
                extra[key] = [g for g in o[key] if g.get("item") in item_names]
        options.append(dict(o, variants=vs, reward=vs[0]["reward"], info=info, items=items, **extra))
    return dict(event, options=options, sources=[x for x in event.get("sources", []) if x in names])


IGNORED_TITLE_WORD = "평가전"   # cleanTitle(공백 제거) 기준


def match_title(queries, events, kind=None):
    """OCR 후보 -> (candidates, ambiguous). candidates=[(event, score)...] 상위 5개, ambiguous=1등과 구분이 안 되는 이벤트 리스트
    (길이 1이면 확신, 0이면 DB 에 없음 = NOT-FOUND). kind 로 라벨 종류에 맞는 이벤트만 후보로 삼는다."""
    # 평가전 계열은 DB 에 3개뿐이라 '원정/토벌 평가전' 같은 미등록 제목이 오매칭되므로 아예 다루지 않는다
    qs = [queries] if isinstance(queries, str) else list(queries)
    if any(IGNORED_TITLE_WORD in q for q in qs):
        return [], []
    pool = [e for e in filter_by_kind(events, kind) if IGNORED_TITLE_WORD not in e.get("cleanTitle", "")]
    cands = find_event_candidates(queries, pool)
    return cands, [ev for ev, _ in ambiguous_set(cands)]
