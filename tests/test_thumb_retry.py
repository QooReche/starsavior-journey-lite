# -*- coding: utf-8 -*-
import io
import os
import sys
import types
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_cards  # noqa: E402
import jh_net  # noqa: E402
import journey_helper as jhm  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def http_err(code):
    return urllib.error.HTTPError("https://x/1.webp", code, "msg", {}, io.BytesIO(b""))


def store(tmp_path, clk, events):
    st = jh_cards.CardStore(str(tmp_path), sleep=0, clock=clk, on_event=events.append)
    st.calls, st.script = 0, []

    def fetch(aid):
        st.calls += 1
        r = st.script.pop(0) if st.script else None
        if isinstance(r, Exception):
            raise r
        open(st._path(aid), "wb").write(b"x")
        return object()
    st._fetch = fetch
    return st


def test_classify_errors():
    c = jh_cards.classify_error
    assert c(http_err(404)) == "missing" and c(http_err(410)) == "missing" and c(http_err(403)) == "missing"
    for code in (500, 502, 503, 429):
        assert c(http_err(code)) == "network"
    assert c(urllib.error.URLError("dns")) == "network" and c(TimeoutError()) == "network"
    assert c(ConnectionResetError()) == "network" and c(OSError("x")) == "network"
    assert c(jh_cards.LocalImageError("decode")) == "missing" and c(ValueError("x")) == "missing"


def test_404_does_not_touch_breaker_and_is_negatively_cached(tmp_path):
    clk, ev = Clock(), []
    st = store(tmp_path, clk, ev)
    st.script = [http_err(404)] * 5
    for aid in (1, 2, 3, 4):
        assert st.get_image(aid) is None
    assert st.breaker.state(0.0) == {"open": False, "fails": 0, "trips": 0, "minutes": 0}
    assert st.calls == 4 and st.is_missing(1)
    assert st.get_image(1) is None and st.calls == 4            # 6시간 음성 캐시: 재요청 없음
    assert not any("중단" in e for e in ev)


def test_network_errors_open_breaker_then_auto_retry_succeeds(tmp_path):
    clk, ev = Clock(), []
    st = store(tmp_path, clk, ev)
    st.script = [urllib.error.URLError("boom"), TimeoutError("t"), http_err(503)]
    for aid in (1, 2, 3):
        assert st.get_image(aid) is None
    assert not st.breaker.allow(clk.t) and any("5분" in e for e in ev)
    clk.t += 301
    assert st.breaker.allow(clk.t)
    st._fail.clear()
    assert st.get_image(1) is not None                         # 서킷이 풀린 뒤 재시도 성공
    assert st.breaker.state(clk.t)["open"] is False


def test_failure_log_has_exception_type_once_per_card(tmp_path):
    clk, ev = Clock(), []
    st = store(tmp_path, clk, ev)
    st.script = [OSError(r"C:\Users\bob\x\1.png denied"), OSError("again")]
    st.get_image(7)
    st._fail.clear()
    st.get_image(7)
    logs = [e for e in ev if "카드 이미지 실패" in e]
    assert len(logs) == 1 and "id=7" in logs[0] and "OSError" in logs[0]
    assert "bob" not in logs[0] and "C:" not in logs[0]
    st2 = store(tmp_path, clk, ev)
    st2.script = [http_err(404)]
    st2.get_image(8)
    assert any("id=8" in e and "HTTPError HTTP 404" in e for e in ev)


# ---- 앱: 재시도 ----
class FakeThread:
    started = []

    def __init__(self, ids):
        self.ids = ids
        self._run = False

    def start(self):
        self._run = True
        FakeThread.started.append(self)

    def isRunning(self):
        return self._run


@pytest.fixture
def host(tmp_path, monkeypatch):
    FakeThread.started = []
    monkeypatch.setattr(jhm, "ThumbThread", FakeThread)
    clk = Clock()
    st = jh_cards.CardStore(str(tmp_path / "cards"), sleep=0, clock=clk)
    monkeypatch.setattr(jh_cards, "default_store", lambda: st)
    cached = set()
    monkeypatch.setattr(jh_cards, "cached_path", lambda aid, store=None: "p" if aid in cached else None)
    monkeypatch.setattr(jhm.jh_savior, "thumb_ids", lambda s: [11, 12])
    monkeypatch.setattr(jhm.jh_savior, "savior_key", lambda m: "K")
    h = types.SimpleNamespace(savior_matches=[{"name": "n"}], _thumb_threads=[], _thumb_done=set(), _thumb_last={})
    mono = Clock(1000.0)
    h._thumb_clock = mono
    h.THUMB_RETRY_SEC = jhm.MouseHoverManager.THUMB_RETRY_SEC
    h.retry = lambda: jhm.MouseHoverManager.retry_thumbs(h)
    h.st, h.clk, h.mono, h.cached = st, clk, mono, cached
    return h


def test_retry_after_failure_waits_30s_and_respects_breaker(host):
    host.retry()
    assert len(FakeThread.started) == 1
    host.retry()                                             # 실행 중이면 새로 만들지 않음
    assert len(FakeThread.started) == 1
    FakeThread.started[0]._run = False                       # 첫 시도 실패로 끝남 (캐시 없음)
    host.mono.t += 10
    host.retry()
    assert len(FakeThread.started) == 1                      # 30초 전: 쉼
    host.mono.t += 25
    for _ in range(3):                                       # 서킷 열림 -> 시도 안 함
        host.st.breaker.failure(host.clk.t)
    host.retry()
    assert len(FakeThread.started) == 1
    host.clk.t += 301                                        # 서킷 풀림 -> 자동 재시도
    host.retry()
    assert len(FakeThread.started) == 2


def test_success_marks_done_and_stops(host):
    host.retry()
    FakeThread.started[0]._run = False
    host.cached.update({11, 12})
    host.mono.t += 100
    host.retry()
    assert "K" in host._thumb_done and len(FakeThread.started) == 1
    host.cached.clear()
    host.mono.t += 100
    host.retry()
    assert len(FakeThread.started) == 1                      # 완료 기록 뒤에는 시도하지 않음


def test_all_missing_cards_not_retried(host):
    host.st._missing[11] = host.st._missing[12] = __import__("time").monotonic()
    host.retry()
    assert FakeThread.started == []


def test_thumb_thread_continues_past_404(tmp_path, monkeypatch):
    clk, ev = Clock(), []
    st = store(tmp_path, clk, ev)
    st.script = [http_err(404), http_err(404), http_err(404), None]
    monkeypatch.setattr(jh_cards, "default_store", lambda: st)
    monkeypatch.setattr(jh_cards, "cached_path", lambda aid, store=None: os.path.exists(st._path(aid)) and st._path(aid))
    jhm.ThumbThread([1, 2, 3, 4]).run()
    assert st.calls == 4                                     # 404 는 서킷에 안 들어가므로 일괄 다운로드가 계속됨


# ---- 한글 경로에 실제로 저장되는지 (cv2.imwrite 는 비 ASCII 경로에 못 쓴다: 2026-10-18 실사용에서 발견) ----
def test_card_image_is_saved_into_non_ascii_folder(tmp_path, monkeypatch):
    import io
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".png", np.full((60, 30, 3), 120, np.uint8))
    assert ok
    data = buf.tobytes()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(jh_cards.urllib.request, "urlopen", lambda req, timeout=None: Resp(data))
    folder = tmp_path / "바탕 화면" / "여정 도우미" / "data" / "cache" / "cards"      # 실제 배포 환경처럼 한글 + 공백 경로
    st = jh_cards.CardStore(str(folder), sleep=0)
    img = st._fetch(7105601)                                                            # 가짜 네트워크, 저장은 진짜
    assert img is not None
    saved = folder / "7105601.png"
    assert saved.exists() and saved.stat().st_size > 0
    again = cv2.imdecode(np.fromfile(str(saved), np.uint8), cv2.IMREAD_COLOR)
    assert again is not None and again.shape[1] == 176                                  # 폭 176 으로 줄여 저장됨
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp.png")]            # 임시 파일 정리됨
