# -*- coding: utf-8 -*-
"""jh_update.py - 새 버전 알림 (GUI 의존성 없음). 자동 다운로드/교체는 하지 않고 최신 버전 번호만 확인한다.

* 확인: GitHub API 대신 releases/latest 에 HEAD 요청을 보내고 리다이렉트를 따라가지 않은 채 302 의 Location 에서 태그를 읽는다.
* 빈도: 앱 시작 시 1회. data\\update_state.json 의 마지막 확인이 12시간 안이면 요청 없이 저장된 최신 버전을 쓴다.
* 실패/비정상 응답: jh_net 의 지수 backoff(10분→30분→2h→6h, 영속)와 하루 시도 상한을 따른다.
* 수동 확인: 12시간 규칙 무시, 10분에 1회, backoff 기간당 1회 우회(수동 새로고침과 같은 규칙), 설정(check_updates)이 꺼져 있어도 동작.
시간(clock)과 네트워크(fetch)는 주입할 수 있어 테스트에서 실제 접속을 하지 않는다.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple

import jh_net
import jh_paths

REPO_URL = "https://github.com/QooReche/starsavior-journey-lite"
LATEST_URL = REPO_URL + "/releases/latest"
STATE_FILE = "update_state.json"
CHECK_INTERVAL_SEC = 12 * 3600
MANUAL_COOLDOWN_SEC = 10 * 60
TIMEOUT_SEC = 6
NO_RELEASE = object()          # _request 가 '게시된 릴리즈 없음(정상 응답)' 을 알릴 때 쓰는 표시
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.(\d+)$")
_VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")

# status: disabled | cached | updated | backoff | capped | cooldown | error
UpdateResult = namedtuple("UpdateResult", "status latest message remain")


def parse_version(s):
    """'1.0.01.261017' 또는 'v1.0.01.261017' -> (1, 0, 1, 261017). 형식이 다르면 None (예외 없음)."""
    if not isinstance(s, str):
        return None
    m = _TAG_RE.match(s.strip()) or _VER_RE.match(s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def tag_to_version(tag):
    """'v1.0.01.261017' -> '1.0.01.261017' (태그 형식이 아니면 None)"""
    if isinstance(tag, str) and _TAG_RE.match(tag.strip()):
        return tag.strip()[1:]
    return None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def visible_update(latest, current, dismissed="", ignore_dismissed=False):
    """알림으로 보여 줄 최신 버전 문자열(없으면 None). dismissed 이하 버전은 숨긴다(더 새 버전이면 다시 보임)."""
    if not is_newer(latest, current):
        return None
    if not ignore_dismissed and parse_version(dismissed) is not None and not is_newer(latest, dismissed):
        return None
    return latest


def release_url(version):
    v = version if parse_version(version) else None
    return f"{REPO_URL}/releases/tag/v{v}" if v else LATEST_URL


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def default_fetch(url, user_agent, timeout=TIMEOUT_SEC):
    """HEAD 요청 -> (status, location). 리다이렉트를 따라가지 않는다. 네트워크 오류는 예외."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": user_agent})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers.get("Location")
    except urllib.error.HTTPError as e:         # 302 는 리다이렉트 비활성 때문에 HTTPError 로 올라온다
        return e.code, e.headers.get("Location") if e.headers else None


def _num(v, default=0):
    return jh_net.clean_num(v, default)


class UpdateChecker:
    def __init__(self, user_agent="JourneyHelperLite", fetch=None, clock=time.time, state_path=None):
        self.user_agent, self.clock = user_agent, clock
        self._fetch = fetch or default_fetch
        self._state_path = state_path
        self._lock = threading.Lock()
        self.state = self._load()

    # --- 상태 파일 ---
    def path(self):
        return self._state_path or os.path.join(jh_paths.data_dir(), STATE_FILE)

    def _load(self):
        st = {"last_check": 0, "latest": None, "consecutive_fail": 0, "next_retry": 0, "last_manual": 0,
              "manual_bypass": False, "last_status": ""}
        try:
            with open(self.path(), "r", encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k in ("last_check", "next_retry", "last_manual"):
                    st[k] = _num(d.get(k))
                cf = d.get("consecutive_fail")
                st["consecutive_fail"] = cf if isinstance(cf, int) and not isinstance(cf, bool) and cf >= 0 else 0
                st["manual_bypass"] = d.get("manual_bypass") is True
                if parse_version(d.get("latest")) is not None:
                    st["latest"] = d["latest"]
                if isinstance(d.get("last_status"), str):
                    st["last_status"] = d["last_status"][:40]
        except Exception:
            pass
        return st

    def _save(self):
        try:
            p = self.path()
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception:
            pass

    @property
    def latest(self):
        return self.state.get("latest")

    def seconds_until_manual(self):
        return max(0, int(self.state["last_manual"] + MANUAL_COOLDOWN_SEC - self.clock()))

    def summary(self, now=None):
        """진단용 한 줄"""
        st = self.state
        lc = st["last_check"]
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lc)) if lc else "-"
        return (f"업데이트 확인: 최신 {st['latest'] or '미확인'}, 마지막 확인 {last}, "
                f"상태 {st['last_status'] or '-'} (연속 실패 {st['consecutive_fail']})")

    # --- 확인 ---
    def _request(self):
        """-> 최신 버전 문자열 또는 None(비정상 응답). 네트워크 예외는 호출자가 잡는다."""
        status, loc = self._fetch(LATEST_URL, self.user_agent)
        if status != 302 or not isinstance(loc, str):
            return None
        tag = loc.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if tag == "releases":                       # 게시된 릴리즈가 없으면 GitHub 가 .../releases 로 보낸다 = 정상 응답, 새 버전 없음
            return NO_RELEASE
        return tag_to_version(tag)

    def check(self, enabled=True, manual=False):
        """예외를 밖으로 내지 않는다. -> UpdateResult"""
        with self._lock:
            try:
                return self._check(enabled, manual)
            except Exception as e:                                  # 방어: 어떤 경우에도 앱에 영향 없음
                return UpdateResult("error", self.latest, f"{type(e).__name__}", 0)

    def _check(self, enabled, manual):
        st, now = self.state, self.clock()
        retry = st["next_retry"]
        if manual:
            remain = self.seconds_until_manual()
            if remain > 0:
                return UpdateResult("cooldown", self.latest, "수동 확인 쿨다운", remain)
            if now < retry:
                if st["manual_bypass"]:
                    return UpdateResult("backoff", self.latest, f"{jh_net.minutes_left(retry, now)}분 뒤 재시도", 0)
                st["manual_bypass"] = True
            st["last_manual"] = now
        else:
            if not enabled:
                return UpdateResult("disabled", self.latest, "확인 꺼짐", 0)
            if (st["latest"] or st["last_status"] == "정상") and now - st["last_check"] < CHECK_INTERVAL_SEC and st["last_check"] <= now:
                return UpdateResult("cached", self.latest, "최근에 확인함 (12시간 이내)", 0)
            if now < retry:
                return UpdateResult("backoff", self.latest, f"{jh_net.minutes_left(retry, now)}분 뒤 재시도", 0)
            if not jh_net.get_limiter("update", jh_net.DB_ATTEMPTS_PER_DAY, 86400).allow(now):
                return UpdateResult("capped", self.latest, "하루 시도 상한 도달", 0)
        try:
            ver = self._request()
        except Exception as e:
            ver, err = None, f"{type(e).__name__}"
        else:
            err = "비정상 응답"
        if ver is None:
            n = st["consecutive_fail"] + 1
            if not manual:
                st["manual_bypass"] = False             # 새 backoff 기간이면 수동 우회권 복구
            st["consecutive_fail"] = n
            st["next_retry"] = now + jh_net.backoff_sec(n)
            st["last_status"] = "실패"
            self._save()
            return UpdateResult("error", self.latest, f"{err} ({jh_net.backoff_sec(n) // 60}분 뒤 재시도)", 0)
        if ver is NO_RELEASE:
            ver = ""                                # 릴리즈 없음: 성공으로 기록(실패 단계 올리지 않음), 최신 버전 값은 비움
        st.update(last_check=now, latest=ver, consecutive_fail=0, next_retry=0, manual_bypass=False, last_status="정상")
        self._save()
        return UpdateResult("updated", ver, "확인함", 0)
