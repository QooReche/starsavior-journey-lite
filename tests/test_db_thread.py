# -*- coding: utf-8 -*-
"""DbThread: 캐시 먼저 사용, 갱신 예외가 캐시를 버리지 않음 / _load_meta 타입 손상."""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402
import journey_helper as jhm  # noqa: E402


class FakeStore:
    def __init__(self, journey, update):
        self.journey, self._update, self.order = journey, update, []

    def load(self):
        self.order.append("load")
        return self.journey, {}

    def update(self, manual=False):
        self.order.append("update")
        if isinstance(self._update, Exception):
            raise self._update
        return self._update


def thread(store, manual=False):
    t = types.SimpleNamespace(store=store, manual=manual)
    t._build_cache = lambda: jhm.DbThread._build_cache(t)
    return t


JOURNEY = {"events": []}


def run_work(store, manual=False, monkeypatch=None):
    out = []
    t = thread(store, manual)
    jhm.DbThread.work(t, lambda c, s, m: out.append((len(c), s)))
    return out


def test_cache_emitted_before_update_and_exception_keeps_cache(monkeypatch):
    monkeypatch.setattr(jh_db, "build_event_cache", lambda j, a, *x: [{"rawTitle": "x"}])
    st = FakeStore(JOURNEY, RuntimeError("boom"))
    out = run_work(st)
    assert st.order[0] == "load" and st.order.index("load") < st.order.index("update")
    assert out == [(1, "cache")]                       # 갱신 예외 후에도 [] 를 보내지 않음


def test_no_cache_and_update_failure_reports_empty(monkeypatch):
    monkeypatch.setattr(jh_db, "build_event_cache", lambda j, a, *x: [])
    out = run_work(FakeStore(None, RuntimeError("net")))
    assert out == [(0, "offline")]


def test_changed_update_emits_second_time(monkeypatch):
    monkeypatch.setattr(jh_db, "build_event_cache", lambda j, a, *x: [{"rawTitle": "x"}])
    out = run_work(FakeStore(JOURNEY, jh_db.UpdateResult("updated", True, "ok")))
    assert out == [(1, "cache"), (1, "updated")]


def test_manual_emits_once_after_update(monkeypatch):
    monkeypatch.setattr(jh_db, "build_event_cache", lambda j, a, *x: [{"rawTitle": "x"}])
    out = run_work(FakeStore(JOURNEY, jh_db.UpdateResult("cooldown", False, "wait")), manual=True)
    assert out == [(1, "cooldown")]


def test_load_meta_type_corruption(tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    store = jh_db.DbStore(cache_dir=str(cache), user_agent="t")
    with open(store.meta_path, "w", encoding="utf-8") as f:
        json.dump({"last_check": "abc", "last_fail": None, "last_manual": True, "files": ["x"]}, f)
    meta = store._load_meta()
    assert meta["last_check"] == 0 and meta["last_fail"] == 0 and meta["last_manual"] == 0
    assert meta["files"] == {}
