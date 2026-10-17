# -*- coding: utf-8 -*-
"""네트워크 부하 제한: 지수 backoff(영속), 서킷브레이커, 시도 상한, 진단 문구. 실제 네트워크 없음(가짜 시계/세션)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_cards  # noqa: E402
import jh_db  # noqa: E402
import jh_net  # noqa: E402
import jh_v2  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class Resp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class Sess:
    """ok=False 이면 연결 오류. 성공 시 픽스처를 돌려준다."""

    def __init__(self):
        self.ok, self.calls = False, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if not self.ok:
            raise ConnectionError("down")
        rel = url.rsplit("/", 1)[1]
        if "pages.dev" in url:
            p = os.path.join(FIX, "v2", rel)
        elif rel == "arcanas.json":
            p = os.path.join(FIX, "arcanas", "arcanas.json")
        else:
            p = os.path.join(FIX, "journey", "ko", "journey.json")
        with open(p, "rb") as f:
            return Resp(200, f.read(), {"ETag": '"x"'})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(tmp, sess=None, clk=None):
    return jh_db.DbStore(str(tmp), session=sess or Sess(), clock=clk or Clock(), seed_dir=None)


def test_backoff_table():
    assert [jh_net.backoff_sec(n) for n in (0, 1, 2, 3, 4, 5, 99)] == [0, 600, 1800, 7200, 21600, 21600, 21600]


def test_db_exponential_backoff_reset_and_persistence(tmp_path):
    clk, s = Clock(), Sess()
    st = mk(tmp_path, s, clk)
    for n, wait in ((1, 600), (2, 1800), (3, 7200), (4, 21600), (5, 21600)):
        r = st.update()
        assert r.status == "offline", (n, r)
        assert st.meta["consecutive_fail"] == n
        assert st.meta["next_retry"] == pytest.approx(clk.t + wait)
        calls = len(s.calls)
        clk.t += wait - 60
        r = st.update()                                      # 대기 중: 요청 없이 안내
        assert r.status == "backoff" and "사이트 응답 없음" in r.message and len(s.calls) == calls
        clk.t += 61
    # 재시작 후에도 유지
    st.update()
    st2 = mk(tmp_path, s, clk)
    assert st2.meta["consecutive_fail"] == 6 and st2.update().status == "backoff"
    # 성공하면 리셋
    clk.t += 21600 + 1
    s.ok = True
    assert st2.update().status == "updated"
    assert st2.meta["consecutive_fail"] == 0 and st2.meta["next_retry"] == 0
    assert mk(tmp_path, s, clk).meta["consecutive_fail"] == 0


def test_damaged_meta_is_safe(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"consecutive_fail": "x", "next_retry": "y", "last_fail": None}), encoding="utf-8")
    st = mk(tmp_path)
    assert st.meta["consecutive_fail"] == 0 and st.meta["next_retry"] == 0
    (tmp_path / "meta.json").write_text("{broken", encoding="utf-8")
    assert mk(tmp_path).meta["consecutive_fail"] == 0


def test_manual_bypasses_backoff_once_per_period(tmp_path):
    clk, s = Clock(), Sess()
    st = mk(tmp_path, s, clk)
    st.update()                                              # 실패 1회 -> 10분 대기
    clk.t += 11 * 60                                         # 대기 끝 + 수동 쿨다운 없음
    st.update()                                              # 자동 재시도 실패 2회 -> 30분 대기
    clk.t += 11 * 60                                         # backoff 중(19분 남음)
    n = len(s.calls)
    r = st.update(manual=True)
    assert r.status == "offline" and len(s.calls) > n        # 1회 우회
    clk.t += 11 * 60                                         # 쿨다운은 지났지만 같은 backoff 기간
    n = len(s.calls)
    r = st.update(manual=True)
    assert r.status == "backoff" and "사이트 응답 없음" in r.message and len(s.calls) == n
    clk.t += 5 * 60                                          # 쿨다운 내
    assert mk(tmp_path, s, clk).update(manual=True).status in ("backoff", "cooldown")


def test_db_daily_cap(tmp_path):
    clk, s = Clock(), Sess()
    lim = jh_net.get_limiter("db", jh_net.DB_ATTEMPTS_PER_DAY, 86400)
    for _ in range(jh_net.DB_ATTEMPTS_PER_DAY):
        assert lim.allow(clk.t)
    st = mk(tmp_path, s, clk)
    r = st.update()
    assert r.status == "capped" and not s.calls
    clk.t += 86401                                           # 하루 지나면 다시 가능
    assert st.update().status == "offline"


def test_v2_backoff_and_persistence(tmp_path):
    clk, s = Clock(), Sess()
    v = jh_v2.V2Store(str(tmp_path), session=s, clock=clk, seed_dir=None)
    r = v.update()
    assert r.status == "offline" and len(s.calls) == 1       # 첫 네트워크 오류에서 나머지 파일 시도 중단
    assert v.meta["consecutive_fail"] == 1
    clk.t += 120
    v2 = jh_v2.V2Store(str(tmp_path), session=s, clock=clk, seed_dir=None)
    r = v2.update()
    assert r.status == "backoff" and "사이트 응답 없음" in r.message and len(s.calls) == 1
    clk.t += 600
    assert v2.update().status == "offline" and v2.meta["next_retry"] == pytest.approx(clk.t + 1800)
    clk.t += 1801
    s.ok = True
    assert v2.update().status == "updated" and v2.meta["consecutive_fail"] == 0


def test_network_summary(tmp_path):
    clk, s = Clock(), Sess()
    st = mk(tmp_path, s, clk)
    st.update(), st.update_v2()
    clk.t += 700
    st.update()
    txt = st.network_summary()
    assert "네트워크:" in txt and "DB 연속 실패 2회/다음 재시도 30분 후" in txt
    assert "V2 연속 실패 1회" in txt and "이미지 서킷 닫힘" in txt


# ---------------------------------------------------------------- 카드 이미지
def card_store(tmp_path, clk, **kw):
    st = jh_cards.CardStore(str(tmp_path), sleep=0, clock=clk, **kw)
    st.calls = 0
    st.ok = False

    def fetch(aid):
        st.calls += 1
        if not st.ok:
            raise OSError("down")
        return object()
    st._fetch = fetch
    return st


def test_circuit_breaker_opens_after_3_and_resumes(tmp_path):
    clk = Clock(0.0)
    events = []
    st = card_store(tmp_path, clk, on_event=events.append)
    for aid in (1, 2, 3):
        assert st.get_image(aid) is None
    assert st.calls == 3 and len(events) == 1 and "5분" in events[0]
    assert st.get_image(4) is None and st.calls == 3         # 열림: 요청 없음
    clk.t += 301
    assert st.get_image(5) is None and st.calls == 4         # 재개 후 첫 시도 실패 -> 더 길게(15분) 다시 열림
    assert st.breaker.state(clk.t)["minutes"] == 15
    clk.t += 15 * 60 + 1
    st.ok = True
    assert st.get_image(6) is not None
    assert st.breaker.state(clk.t) == {"open": False, "fails": 0, "trips": 0, "minutes": 0}


def test_image_hourly_limit(tmp_path):
    clk = Clock(0.0)
    events = []
    st = card_store(tmp_path, clk, limiter=jh_net.RateLimiter(2, 3600), on_event=events.append)
    st.ok = True
    for aid in (1, 2, 3, 4):
        st.get_image(aid)
    assert st.calls == 2 and len(events) == 1                # 로그는 한 줄
    clk.t += 3601
    st.get_image(5)
    assert st.calls == 3


def test_thumb_thread_stops_after_3_failures(tmp_path, monkeypatch):
    import journey_helper as jhm
    clk = Clock(0.0)
    st = card_store(tmp_path, clk)
    monkeypatch.setattr(jh_cards, "default_store", lambda: st)
    monkeypatch.setattr(jh_cards, "cached_path", lambda aid, store=None: None)
    t = jhm.ThumbThread(list(range(100, 140)))
    t.run()
    assert st.calls == 3


def test_card_thread_path_follows_breaker(tmp_path):
    """식별용 get_descriptor 도 같은 store 를 쓰므로 서킷이 열려 있으면 요청하지 않는다"""
    clk = Clock(0.0)
    st = card_store(tmp_path, clk)
    for aid in (1, 2, 3):
        st.get_descriptor(aid)
    n = st.calls
    assert st.get_descriptor(9) is None and st.calls == n
