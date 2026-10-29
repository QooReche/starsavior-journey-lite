# -*- coding: utf-8 -*-
"""구원자 추천 데이터 원격 갱신(jh_reco): 네트워크 없이 가짜 응답으로 검증/거부/폴백/우선순위를 확인한다."""
import json
import os

import jh_net
import jh_reco
import jh_savior

APP_V = "9.9.9.000000"


def _sav(n):
    return [{"key": f"k{i}", "name": f"이름{i}", "subtitle": "", "slots": [], "alternatives": []} for i in range(n)]


def _doc(n=10, version=3, generated="2026-10-10", **extra):
    d = {"version": version}
    if generated:
        d["generated"] = generated
    d.update(extra)
    d["saviors"] = _sav(n)
    return d


def _bad(mut):
    d = _doc(10)
    mut(d["saviors"][2])
    return d


def _bytes(d):
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


class Resp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class Session:
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        r = self.resps.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _bundle(tmp_path, d=None):
    p = tmp_path / "bundle" / "savior_reco.json"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(_bytes(d or _doc(10)))
    return str(p)


def _store(tmp_path, session, clock=None, bundle=None, app_version=APP_V):
    return jh_reco.RecoStore(str(tmp_path / "cache"), session=session, clock=clock or Clock(),
                             bundle=bundle or _bundle(tmp_path), app_version=app_version)


def test_valid_response_replaces_cache_and_saves_meta(tmp_path):
    s = Session(Resp(200, _bytes(_doc(12)), {"ETag": "W/abc"}))
    st = _store(tmp_path, s)
    res = st.update()
    assert res.status == "updated" and res.changed
    assert s.calls[0][0] == jh_reco.RECO_URL and "If-None-Match" not in s.calls[0][1]
    assert st.cache_valid()
    meta = json.load(open(st.meta_path, encoding="utf-8"))
    assert meta["etag"] == "W/abc" and meta["app_version"] == APP_V
    blob = json.dumps(meta)                                                      # URL·경로 문자열이 meta 에 들어가지 않음
    assert "http" not in blob and "github" not in blob and str(tmp_path) not in blob and "\\\\" not in blob and "/" not in blob.replace("W/abc", "")


def test_conditional_request_and_304(tmp_path):
    clock = Clock()
    s = Session(Resp(200, _bytes(_doc(12)), {"ETag": '"a"'}), Resp(304))
    st = _store(tmp_path, s, clock)
    st.update()
    clock.t += jh_reco.jh_db.CHECK_INTERVAL_SEC + 1
    assert st.update().status == "not_modified"
    assert s.calls[1][1]["If-None-Match"] == '"a"'
    assert _store(tmp_path, Session(), clock).update().status == "skipped_recent"      # 12시간 안에는 요청하지 않음


def test_rejections_keep_existing_cache(tmp_path):
    good = _bytes(_doc(12, generated="2026-10-12"))
    big = _bytes(_doc(10, pad="x" * (jh_reco.MAX_BYTES + 10)))
    nokey = _doc(10)
    del nokey["saviors"][3]["name"]
    cases = [
        _bytes(_doc(10, version=2)),          # 번들보다 낮은(낡은) 스키마  (높은 스키마는 건너뜀: 아래 별도 테스트)
        _bytes(_doc(4)),                      # 번들(10명)의 절반 미만
        b"{broken json",                      # 깨진 JSON
        b"<html>404</html>",                  # HTML 대체 응답
        big,                                  # 크기 상한 초과
        _bytes(nokey),                        # key/name 없음
        _bytes({k: v for k, v in _doc(10).items() if k != "generated"}),          # 원격에 generated 없음
        _bytes(_doc(10, generated="2026-09-01")),                                 # 번들(2026-10-10)보다 이른 generated
        _bytes(_doc(10, unportedPatches=["x"])),                                  # 포팅 안 된 패치 있음
        _bytes(_bad(lambda s: s.update(slots="oops"))),                           # slots 가 list 아님
        _bytes(_bad(lambda s: s.update(alternatives={"a": 1}))),                  # alternatives 가 list 아님
        _bytes(_bad(lambda s: s.update(slots=[{"options": "x", "note": ""}]))),   # options 가 list 아님
        _bytes(_bad(lambda s: s.update(slots=[{"options": [{"name": "a", "arcanaId": "7"}], "note": ""}]))),   # arcanaId 가 int 아님
        _bytes(_bad(lambda s: s.update(slots=[{"options": [], "note": 5}]))),     # note 가 str 아님
        _bytes(_bad(lambda s: s.update(sets=[{"label": 1, "slots": [], "alternatives": []}]))),   # label 이 str 아님
        _bytes(_bad(lambda s: s.update(statPriority=[{"label": "a", "groups": 5}]))),   # 시험 렌더에서 예외
    ]
    for bad in cases:
        jh_net.reset_for_tests()                  # 사례마다 일일 상한 초기화
        clock = Clock()
        s = Session(Resp(200, good), Resp(200, bad))
        st = _store(tmp_path, s, clock)
        st.update()
        before = open(st.path, "rb").read()
        res = st.update(manual=True)
        assert res.status == "offline" and not res.changed
        assert open(st.path, "rb").read() == before
        os.remove(st.path)
        os.remove(st.meta_path)


def test_http_error_and_network_failure_backoff(tmp_path):
    clock = Clock()
    s = Session(RuntimeError("boom"), Resp(500))
    st = _store(tmp_path, s, clock)
    r = st.update()
    assert r.status == "offline" and not os.path.exists(st.path)
    assert st.meta["consecutive_fail"] == 1 and st.meta["next_retry"] == clock.t + jh_net.backoff_sec(1)
    assert st.update().status == "backoff" and len(s.calls) == 1             # 대기 중에는 요청하지 않음
    clock.t += jh_net.backoff_sec(1) + 1
    assert st.update().status == "offline" and st.meta["consecutive_fail"] == 2


def test_daily_limit_applies(tmp_path):
    clock = Clock()
    lim = jh_net.get_limiter(jh_reco.LIMITER_NAME, jh_net.DB_ATTEMPTS_PER_DAY, 86400)
    for _ in range(jh_net.DB_ATTEMPTS_PER_DAY):
        assert lim.allow(clock.t)
    s = Session()
    assert _store(tmp_path, s, clock).update().status == "capped" and not s.calls


def test_older_than_bundle_generated_rejected(tmp_path):
    b = _bundle(tmp_path, _doc(10, generated="2026-10-20"))
    s = Session(Resp(200, _bytes(_doc(10, generated="2026-10-01"))))
    st = _store(tmp_path, s, bundle=b)
    assert st.update().status == "offline" and not os.path.exists(st.path)


def test_preferred_order_cache_over_bundle(tmp_path):
    b = _bundle(tmp_path)
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12)))), bundle=b)
    st.update()
    cdir = str(tmp_path / "cache")
    assert jh_reco.preferred_path(cdir, b, APP_V) == [st.path, b]
    assert jh_reco.preferred_path(cdir, b, "other.version") == [b]            # 앱 버전이 바뀌면 캐시 무시


def test_preferred_generated_comparison(tmp_path):
    cdir = str(tmp_path / "cache")
    # 번들에 generated 가 더 새것이면 번들
    b_new = _bundle(tmp_path, _doc(10, generated="2026-10-20"))
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12, generated="2026-10-25")))), bundle=b_new)
    st.update()
    assert jh_reco.preferred_path(cdir, b_new, APP_V)[0] == st.path
    # 캐시 파일을 낡은 날짜로 바꿔 놓으면(검증 실패) 번들
    open(st.path, "wb").write(_bytes(_doc(12, generated="2026-10-01")))
    assert jh_reco.preferred_path(cdir, b_new, APP_V) == [b_new]
    # 번들에만 generated 가 있고 캐시에는 없으면 번들
    open(st.path, "wb").write(_bytes(_doc(12, generated="")))
    assert jh_reco.preferred_path(cdir, b_new, APP_V) == [b_new]


def test_corrupt_cache_falls_back_to_bundle(tmp_path, monkeypatch):
    b = _bundle(tmp_path)
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12)))), bundle=b)
    st.update()
    open(st.path, "wb").write(b"{not json")
    assert jh_reco.preferred_path(str(tmp_path / "cache"), b, APP_V) == [b]


def test_app_version_change_discards_cache_and_refetches(tmp_path):
    b = _bundle(tmp_path)
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12)), {"ETag": '"a"'})), bundle=b)
    st.update()
    s2 = Session(Resp(200, _bytes(_doc(11)), {"ETag": '"b"'}))
    st2 = _store(tmp_path, s2, bundle=b, app_version="10.0.0.000000")
    assert st2.update().status == "updated"
    assert "If-None-Match" not in s2.calls[0][1]                              # 이전 버전의 etag 로 조건부 요청하지 않음
    assert st2.meta["app_version"] == "10.0.0.000000"


# ---- load_reco 연결 (실제 앱 폴더 = conftest 의 임시 폴더)
def test_load_reco_uses_cache_then_bundle(tmp_path, monkeypatch):
    bundle_doc = _doc(10)
    b = _bundle(tmp_path, bundle_doc)
    monkeypatch.setattr(jh_savior, "default_data_path", lambda: b)
    assert len(jh_savior.load_reco()) == 10                                   # 캐시 없음 -> 번들
    import jh_db
    st = jh_reco.RecoStore(jh_db.default_cache_dir(), session=Session(Resp(200, _bytes(_doc(13)))), bundle=b,
                           app_version=jh_reco.APP_VERSION)
    assert st.update().status == "updated"
    assert len(jh_savior.load_reco()) == 13                                   # 유효한 캐시 우선
    open(st.path, "wb").write(b"garbage")
    assert len(jh_savior.load_reco()) == 10                                   # 깨졌으면 번들
    assert len(jh_savior.load_reco(b)) == 10                                  # 명시 경로는 그대로


def test_bundled_file_still_loads_by_default():
    assert jh_savior.load_reco()


def test_dbstore_update_reco_never_raises(tmp_path):
    import jh_db
    class Boom:
        def get(self, *a, **k):
            raise OSError("down")
    r = jh_db.DbStore(cache_dir=str(tmp_path / "c"), session=Boom()).update_reco()
    assert r.status == "offline" and not r.changed


def test_valid_full_structure_passes_and_bundled_file_validates():
    ok = _doc(10)
    ok["saviors"][0].update(slots=[{"options": [{"name": "a", "arcanaId": 7102501}], "note": ""}, None],
                            alternatives=[{"options": [{"name": "b", "arcanaId": None}], "note": "a 대체", "slot": 0}])
    jh_reco.validate(ok, (3, 10, "2026-10-10"))
    b = jh_reco.summarize(jh_reco._read_json(jh_reco.bundled_path()))
    assert b[2], "번들에 generated 가 있어야 한다"
    jh_reco.validate(jh_reco._read_json(jh_reco.bundled_path()), b)


def test_higher_schema_is_skip_not_failure(tmp_path):
    clock = Clock()
    s = Session(Resp(200, _bytes(_doc(10, version=4))), Resp(200, _bytes(_doc(12))))
    st = _store(tmp_path, s, clock)
    r = st.update()
    assert r.status == "skipped_version" and not r.changed
    assert st.meta["consecutive_fail"] == 0 and st.meta["next_retry"] == 0 and st.meta["last_fail"] == 0
    clock.t += 3600
    assert st.update().status == "skipped_recent" and len(s.calls) == 1        # 24시간 안에는 다시 확인하지 않음
    clock.t += jh_reco.SKIP_SEC
    assert _store(tmp_path, s, clock).update().status == "updated"             # 24시간 뒤 재확인


def test_permission_error_retries_then_fails_without_counting(tmp_path, monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(jh_reco, "_sleep", sleeps.append)

    def locked(path, data):
        calls.append(1)
        raise PermissionError("locked")
    monkeypatch.setattr(jh_reco.jh_db, "_atomic_write", locked)
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12)))))
    r = st.update()
    assert r.status == "offline" and len(calls) == 3 and sleeps == [0.2, 0.2]
    assert st.meta["consecutive_fail"] == 0 and st.meta["next_retry"] == 0


def test_permission_error_succeeds_on_retry(tmp_path, monkeypatch):
    real, n = jh_reco.jh_db._atomic_write, []
    monkeypatch.setattr(jh_reco, "_sleep", lambda t: None)

    def flaky(path, data):
        n.append(1)
        if len(n) == 1:
            raise PermissionError("locked")
        return real(path, data)
    monkeypatch.setattr(jh_reco.jh_db, "_atomic_write", flaky)
    st = _store(tmp_path, Session(Resp(200, _bytes(_doc(12)))))
    assert st.update().status == "updated" and os.path.isfile(st.path)
