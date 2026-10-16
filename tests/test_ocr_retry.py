# -*- coding: utf-8 -*-
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_backoff_increases_and_caps():
    c = Clock()
    b = jhm.RetryBackoff(clock=c)
    assert b.ready()
    waits = []
    for _ in range(6):
        b.failed()
        waits.append(b._next - c.t)
        assert not b.ready()
        c.t = b._next
        assert b.ready()
    assert waits == [5.0, 15.0, 30.0, 60.0, 60.0, 60.0]
    b.success()
    assert b.ready() and b.fails == 0


def make_watcher(factory, monkeypatch):
    c = Clock()
    w = types.SimpleNamespace(ocr=None, _ocr_retry=jhm.RetryBackoff(clock=c), _ocr_failed_notified=False, events=[])
    w.model_loading = types.SimpleNamespace(emit=lambda: w.events.append("loading"))
    w.model_ready = types.SimpleNamespace(emit=lambda: w.events.append("ready"))
    w.model_failed = types.SimpleNamespace(emit=lambda m: w.events.append("failed"))
    monkeypatch.setattr(jhm, "KoreanOCR", factory)
    monkeypatch.setattr(jhm, "log", lambda *a, **k: None)
    return w, c


def test_load_failure_notifies_once_and_backs_off(monkeypatch):
    calls = []

    def bad():
        calls.append(1)
        raise RuntimeError("no model")
    w, c = make_watcher(bad, monkeypatch)
    for _ in range(50):                      # 200ms 루프를 흉내: 시간이 안 지나면 재시도하지 않아야 한다
        assert jhm.ScreenWatcher._ensure_ocr(w) is False
    assert len(calls) == 1 and w.events == ["loading", "failed"]
    c.t += 5.1
    assert jhm.ScreenWatcher._ensure_ocr(w) is False
    assert len(calls) == 2 and w.events.count("failed") == 1 and w.events.count("loading") == 1


def test_recovers_after_failure(monkeypatch):
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("x")
        return object()
    w, c = make_watcher(flaky, monkeypatch)
    assert jhm.ScreenWatcher._ensure_ocr(w) is False
    c.t += 6
    assert jhm.ScreenWatcher._ensure_ocr(w) is True and w.events[-1] == "ready"
    assert jhm.ScreenWatcher._ensure_ocr(w) is True and state["n"] == 2
