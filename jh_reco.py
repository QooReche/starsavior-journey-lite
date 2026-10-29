# -*- coding: utf-8 -*-
"""
jh_reco.py - 구원자 추천 데이터(assets/savior_reco.json) 원격 갱신 (GUI/OCR 의존성 없음)

앱 새 버전 없이 데이터만 바꾸기 위한 계층. jh_v2.V2Store 와 같은 방식(조건부 GET, jh_net backoff/상한, 검증 실패 시 기존 유지).
* 받은 응답은 검증을 통과해야만 캐시(data\\cache\\reco\\savior_reco.json + meta.json)가 된다.
* 읽기 우선순위(preferred_path): 유효한 캐시 > 번들 assets. 번들이 더 새것이면('generated') 번들.
* 앱 버전이 바뀌면(= 번들이 바뀌었을 수 있음) 캐시를 버리고 다시 받는다.
* 새 데이터는 다음 load_reco() 호출(다음 실행)부터 쓰인다. 어떤 실패도 예외를 밖으로 내지 않는다.
"""
import json
import os
import re
import time

import jh_db
import jh_net
import jh_paths
from jh_version import APP_VERSION

RECO_URL = "https://raw.githubusercontent.com/QooReche/starsavior-journey-lite/main/assets/savior_reco.json"
RECO_FILE = "savior_reco.json"
MAX_BYTES = 5 * 1024 * 1024
MIN_RATIO = 0.5
LIMITER_NAME = "reco"


def bundled_path():
    import sys
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", RECO_FILE)


def _read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def summarize(data):
    """(version, 구원자 수, generated 또는 '') - 구조가 이상하면 ValueError"""
    if not isinstance(data, dict) or not isinstance(data.get("saviors"), list):
        raise ValueError("unexpected structure")
    v = data.get("version")
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("no schema version")
    g = data.get("generated")
    return v, len(data["saviors"]), g if isinstance(g, str) else ""


SKIP_SEC = 24 * 3600
_sleep = time.sleep


class CacheWriteError(Exception):
    pass


def _write_cache(path, content):
    """os.replace 가 PermissionError(백신/동기화 잠금)면 0.2초 간격으로 2번 더 시도, 그래도 실패하면 CacheWriteError."""
    for i in range(3):
        try:
            jh_db._atomic_write(path, content)
            return
        except PermissionError:
            if i < 2:
                _sleep(0.2)
    raise CacheWriteError("PermissionError")


class SkipVersion(ValueError):
    """원격 스키마가 번들보다 높다 = 앱이 모르는 형식. 실패가 아니라 '건너뜀'으로 다룬다."""


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_options(opts):
    if not isinstance(opts, list):
        raise ValueError("options not list")
    for o in opts:
        if not isinstance(o, dict) or not isinstance(o.get("name", ""), str):
            raise ValueError("bad option")
        aid = o.get("arcanaId")
        if aid is not None and (not isinstance(aid, int) or isinstance(aid, bool)):
            raise ValueError("bad arcanaId")


def _check_slots(slots, alts):
    if not isinstance(slots, list) or not isinstance(alts, list):
        raise ValueError("slots/alternatives not list")
    for x in list(slots) + list(alts):
        if x is None and x in slots:
            continue
        if not isinstance(x, dict):
            raise ValueError("bad slot")
        _check_options(x.get("options", []))
        for k in ("name", "note"):
            if not isinstance(x.get(k, ""), str):
                raise ValueError("bad slot text")


def _check_structure(s):
    for k in ("name", "subtitle", "affiliation", "grade", "element", "className"):
        if not isinstance(s.get(k, ""), str):
            raise ValueError(f"{k} not str")
    _check_slots(s.get("slots", []), s.get("alternatives", []))
    sets = s.get("sets", [])
    if not isinstance(sets, list):
        raise ValueError("sets not list")
    for st in sets:
        if not isinstance(st, dict) or not isinstance(st.get("label", ""), str):
            raise ValueError("bad set")
        _check_slots(st.get("slots", []), st.get("alternatives", []))


def _trial_render(saviors):
    """앱이 쓰는 포맷터로 모든 구원자를 한 번씩 그려 본다(예외가 나면 이 데이터는 쓸 수 없다)."""
    import jh_equip
    import jh_savior
    for s in saviors:
        s = dict(s)
        s["_n_name"], s["_n_sub"] = jh_savior._norm(s.get("name")), jh_savior._norm(s.get("subtitle"))
        jh_savior.format_html([s])
        jh_savior.format_stat_html(s)
        jh_equip.format_html(s)
        jh_savior.thumb_ids(s)


def validate(data, bundle):
    """받은(또는 캐시) 데이터를 번들 요약 bundle=(version, 수, generated)과 비교해 검증. 맞지 않으면 ValueError(스키마가 높으면 SkipVersion)."""
    ver, n, gen = summarize(data)
    b_ver, b_n, b_gen = bundle
    if ver > b_ver:
        raise SkipVersion(f"schema version {ver} > {b_ver}")
    if ver != b_ver:
        raise ValueError(f"schema version {ver} < {b_ver}")           # 번들보다 낡음
    if not _DATE.match(gen or ""):
        raise ValueError("no generated date")
    if b_gen and gen < b_gen:
        raise ValueError("older than bundled data")
    if data.get("unportedPatches"):
        raise ValueError("unported patches")
    if n < b_n * MIN_RATIO:
        raise ValueError(f"saviors {b_n} -> {n}")
    for s in data["saviors"]:
        if not (isinstance(s, dict) and isinstance(s.get("key"), str) and s["key"]
                and isinstance(s.get("name"), str) and s["name"]):
            raise ValueError("savior without key/name")
        _check_structure(s)
    try:
        _trial_render(data["saviors"])
    except Exception as e:
        raise ValueError(f"trial render failed: {type(e).__name__}")


class RecoStore:
    def __init__(self, cache_dir=None, session=None, url=RECO_URL, clock=time.time, user_agent=jh_db.DEFAULT_USER_AGENT,
                 bundle=None, app_version=APP_VERSION):
        self.dir = os.path.join(cache_dir or jh_db.default_cache_dir(), "reco")
        self.path = os.path.join(self.dir, RECO_FILE)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.url, self.clock, self.user_agent, self.app_version = url, clock, user_agent, app_version
        self._session = session
        self._bundle = bundle                      # 번들 파일 경로(테스트용 주입), None 이면 앱 번들
        self.meta = self._load_meta()

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _bundle_info(self):
        try:
            return summarize(_read_json(self._bundle or bundled_path()))
        except Exception:
            return None

    def _load_meta(self):
        meta = {"app_version": "", "received": 0, "etag": None, "last_modified": None,
                "last_check": 0, "last_fail": 0, "consecutive_fail": 0, "next_retry": 0, "skip_until": 0}
        try:
            saved = _read_json(self.meta_path)
            if isinstance(saved, dict):
                for k in ("received", "last_check", "last_fail", "next_retry", "skip_until"):
                    meta[k] = jh_net.clean_num(saved.get(k))
                cf = saved.get("consecutive_fail")
                if isinstance(cf, int) and not isinstance(cf, bool) and cf > 0:
                    meta["consecutive_fail"] = cf
                for k in ("app_version", "etag", "last_modified"):
                    if isinstance(saved.get(k), str):
                        meta[k] = saved[k]
        except Exception:
            pass
        return meta

    def _save_meta(self):
        try:
            jh_db._atomic_write(self.meta_path, json.dumps(self.meta, ensure_ascii=False, indent=1).encode("utf-8"))
        except Exception:
            pass

    def cache_valid(self):
        """현재 앱 버전이 받은 유효한 캐시가 있는가 (번들보다 낡았으면 False)."""
        if self.meta.get("app_version") != self.app_version:
            return False
        b = self._bundle_info()
        if b is None:
            return False
        try:
            validate(_read_json(self.path), b)
            return True
        except Exception:
            return False

    def retry_at(self):
        lf = self.meta.get("last_fail", 0)
        return max(self.meta.get("next_retry", 0), lf + jh_db.FAIL_BACKOFF_SEC if lf else 0)

    def _drop_stale(self):
        """앱 버전이 바뀌었으면 캐시를 버린다(번들이 더 새로울 수 있다)."""
        mv = self.meta.get("app_version")
        if mv and mv != self.app_version:
            for p in (self.path, self.meta_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.meta = self._load_meta()

    def update(self, manual=False):
        """-> jh_db.UpdateResult. 예외를 밖으로 내지 않는다."""
        try:
            return self._update(manual)
        except Exception as e:
            return jh_db.UpdateResult("offline", False, f"추천 데이터 갱신 예외: {type(e).__name__} {str(e)[:100]}")

    def _update(self, manual):
        now = self.clock()
        self._drop_stale()
        have = self.cache_valid()
        retry = self.retry_at()
        if not manual:
            if have and now - self.meta.get("last_check", 0) < jh_db.CHECK_INTERVAL_SEC:
                return jh_db.UpdateResult("skipped_recent", False, "추천 데이터: 최근에 확인함")
            if now < retry:
                return jh_db.UpdateResult("backoff", False, f"추천 데이터: {jh_net.minutes_left(retry, now)}분 뒤 재시도")
            if not jh_net.get_limiter(LIMITER_NAME, jh_net.DB_ATTEMPTS_PER_DAY, 86400).allow(now):
                return jh_db.UpdateResult("capped", False, "추천 데이터: 오늘 자동 갱신 시도 상한 도달, 중단")
        elif now < retry:
            return jh_db.UpdateResult("backoff", False, f"추천 데이터: {jh_net.minutes_left(retry, now)}분 뒤 재시도")
        if not manual and now < self.meta.get("skip_until", 0):
            return jh_db.UpdateResult("skipped_recent", False, "추천 데이터: 새 스키마 건너뜀 중")
        bundle = self._bundle_info()
        if bundle is None:
            return jh_db.UpdateResult("offline", False, "추천 데이터: 번들 데이터를 읽지 못해 갱신하지 않음")
        try:
            status = self._fetch(have, bundle)
        except SkipVersion:                              # 앱이 모르는 새 스키마: 실패가 아니다(backoff/실패 횟수 없음), 24시간 뒤에야 다시 확인
            self.meta["skip_until"], self.meta["last_check"] = now + SKIP_SEC, now
            self._save_meta()
            return jh_db.UpdateResult("skipped_version", False, "추천 데이터: 이 앱보다 새 스키마라 건너뜀 (앱 업데이트 필요)")
        except CacheWriteError as e:                     # 파일 잠금 등: 실패지만 연속 실패 횟수/backoff 에는 넣지 않는다
            return jh_db.UpdateResult("offline", False, f"추천 데이터 저장 실패, 기존 데이터 사용: {e}")
        except Exception as e:
            n = self.meta["consecutive_fail"] + 1
            self.meta["consecutive_fail"], self.meta["last_fail"] = n, now
            self.meta["next_retry"] = now + jh_net.backoff_sec(n)
            self._save_meta()
            return jh_db.UpdateResult("offline", False, f"추천 데이터 갱신 실패, 기존 데이터 사용 (연속 실패 {n}회, "
                                      f"{jh_net.backoff_sec(n) // 60}분 뒤 재시도): {type(e).__name__} {str(e)[:100]}")
        self.meta["last_check"], self.meta["last_fail"] = now, 0
        self.meta["consecutive_fail"], self.meta["next_retry"], self.meta["skip_until"] = 0, 0, 0
        self._save_meta()
        return jh_db.UpdateResult(status, status == "updated", "추천 데이터 갱신됨" if status == "updated" else "추천 데이터 변경 없음")

    def _fetch(self, have, bundle):
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if have:
            if self.meta.get("etag"):
                headers["If-None-Match"] = self.meta["etag"]
            if self.meta.get("last_modified"):
                headers["If-Modified-Since"] = self.meta["last_modified"]
        r = self.session.get(self.url, headers=headers, timeout=jh_db.HTTP_TIMEOUT)
        if r.status_code == 304 and have:
            return "not_modified"
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        content = r.content
        if len(content) > MAX_BYTES:
            raise ValueError(f"response too large: {len(content)} bytes")
        data = json.loads(content.decode("utf-8-sig"))
        validate(data, bundle)
        same = False
        try:
            with open(self.path, "rb") as f:
                same = f.read() == content
        except OSError:
            pass
        if not same:
            _write_cache(self.path, content)
        h = r.headers or {}
        self.meta.update(app_version=self.app_version, received=self.clock(),
                         etag=h.get("ETag") or h.get("etag"), last_modified=h.get("Last-Modified") or h.get("last-modified"))
        return "not_modified" if same and have else "updated"


def preferred_path(cache_dir=None, bundle=None, app_version=APP_VERSION):
    """읽을 파일 경로 후보를 우선순위 순으로: [캐시(유효하고 번들보다 새것일 때), 번들]. 예외 없음."""
    b_path = bundle or bundled_path()
    out = []
    try:
        st = RecoStore(cache_dir, bundle=b_path, app_version=app_version)
        if os.path.isfile(st.path) and st.cache_valid():
            c_gen, b_gen = summarize(_read_json(st.path))[2], summarize(_read_json(b_path))[2]
            if not (b_gen and (not c_gen or b_gen > c_gen)):         # 번들에만 generated 가 있거나 더 새것이면 번들
                out.append(st.path)
    except Exception:
        pass
    out.append(b_path)
    return out
