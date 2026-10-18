# -*- coding: utf-8 -*-
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_net  # noqa: E402
import jh_update as ju  # noqa: E402
import journey_helper as jhm  # noqa: E402

TAG = "https://github.com/QooReche/starsavior-journey-lite/releases/tag/v1.0.01.261099"
OK = (302, TAG)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Fetch:
    """가짜 네트워크: responses 를 순서대로(마지막은 반복) 돌려줌. Exception 이면 raise. 실제 접속 없음."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def __call__(self, url, ua):
        self.calls += 1
        assert url == ju.LATEST_URL
        r = self.responses[min(self.calls, len(self.responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r


def mk(*resp, clock=None):
    clock = clock or Clock()
    f = Fetch(*resp)
    return ju.UpdateChecker("JourneyHelperLite/1.0.01.261017", fetch=f, clock=clock), f, clock


def test_parse_and_compare():
    assert ju.parse_version("v1.0.01.261017") == (1, 0, 1, 261017)
    assert ju.parse_version("1.0.01.261017") == (1, 0, 1, 261017)
    for bad in (None, 5, "", "v1.0.1", "1.0.01.x", "v1.0.01.261017-beta", "latest", "v1.2.3.4.5"):
        assert ju.parse_version(bad) is None
    assert ju.is_newer("1.0.01.261018", "1.0.01.261017")
    assert not ju.is_newer("1.0.01.261017", "1.0.01.261017")
    assert not ju.is_newer("1.0.01.261016", "1.0.01.261017")
    assert not ju.is_newer("garbage", "1.0.01.261017") and not ju.is_newer("1.0.01.261099", "garbage")
    assert ju.is_newer("1.0.02.1", "1.0.01.261017")


def test_parse_location_and_ignore_abnormal():
    c, f, _ = mk(OK)
    r = c.check()
    assert r.status == "updated" and r.latest == "1.0.01.261099" and c.latest == "1.0.01.261099"
    for resp in ((200, None), (302, None), (404, None),   # (302 -> .../releases = 릴리즈 없음 은 정상 응답: test_no_release_is_success_not_error)
                 (302, "https://github.com/x/releases/tag/nightly"), OSError("net")):
        os.remove(c.path()) if os.path.exists(c.path()) else None
        c, f, _ = mk(resp)
        r = c.check()
        assert r.status == "error" and c.latest is None and c.state["consecutive_fail"] == 1


def test_no_redirect_handler():
    assert ju._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://x") is None


def test_within_12h_no_request_then_after_12h_requests():
    c, f, clk = mk(OK)
    c.check()
    assert f.calls == 1
    clk.t += 11 * 3600
    r = c.check()
    assert f.calls == 1 and r.status == "cached" and r.latest == "1.0.01.261099"
    clk.t += 2 * 3600
    c.check()
    assert f.calls == 2


def test_state_persisted_between_instances():
    clk = Clock()
    c, f, _ = mk(OK, clock=clk)
    c.check()
    f2 = Fetch(OK)
    c2 = ju.UpdateChecker("ua", fetch=f2, clock=clk)
    assert c2.check().status == "cached" and f2.calls == 0 and c2.latest == "1.0.01.261099"
    assert os.path.isfile(os.path.join(os.environ["JH_APP_DIR"], "data", ju.STATE_FILE))


def test_backoff_steps():
    clk = Clock()
    c, f, _ = mk((500, None), clock=clk)
    for n, wait in enumerate((600, 1800, 7200, 21600, 21600), 1):
        assert c.check().status == "error"
        assert f.calls == n and c.state["consecutive_fail"] == n
        assert c.state["next_retry"] == clk.t + wait
        clk.t += wait - 1
        assert c.check().status == "backoff" and f.calls == n          # 대기 중에는 요청 없음
        clk.t += 1


def test_backoff_survives_restart():
    clk = Clock()
    c, f, _ = mk((500, None), clock=clk)
    c.check()
    c2 = ju.UpdateChecker("ua", fetch=f, clock=clk)
    assert c2.state["consecutive_fail"] == 1
    assert c2.check().status == "backoff" and f.calls == 1


def test_daily_cap():
    clk = Clock()
    c, f, _ = mk(OK, clock=clk)
    lim = jh_net.get_limiter("update", jh_net.DB_ATTEMPTS_PER_DAY, 86400)
    for _ in range(jh_net.DB_ATTEMPTS_PER_DAY):
        lim.allow(clk.t)
    assert c.check().status == "capped" and f.calls == 0


def test_disabled_makes_no_request():
    c, f, _ = mk(OK)
    assert c.check(enabled=False).status == "disabled" and f.calls == 0


def test_manual_works_when_disabled_and_ignores_12h():
    c, f, clk = mk(OK)
    c.check()
    clk.t += 60
    r = c.check(enabled=False, manual=True)
    assert f.calls == 2 and r.status == "updated"


def test_manual_cooldown_10min():
    c, f, clk = mk(OK)
    assert c.check(manual=True).status == "updated"
    clk.t += 300
    r = c.check(manual=True)
    assert r.status == "cooldown" and r.remain == 300 and f.calls == 1
    clk.t += 300
    assert c.check(manual=True).status == "updated" and f.calls == 2


def test_manual_bypasses_backoff_once_and_raises_step():
    c, f, clk = mk((500, None))
    c.check()                                            # 자동 실패 1회 (backoff 10분)
    clk.t += 601
    c.state["next_retry"] = clk.t + 500                  # backoff 가 남은 상태로 맞춤
    assert c.check(manual=True).status == "error"        # backoff 중이어도 1회 허용
    assert f.calls == 2 and c.state["consecutive_fail"] == 2      # 단계 상승
    clk.t += 601
    c.state["next_retry"] = clk.t + 500
    assert c.check(manual=True).status == "backoff" and f.calls == 2    # 같은 기간 두 번째는 막힘


def test_dismissed_rules():
    cur = "1.0.01.261017"
    assert ju.visible_update("1.0.01.261018", cur, "") == "1.0.01.261018"
    assert ju.visible_update("1.0.01.261018", cur, "1.0.01.261018") is None
    assert ju.visible_update("1.0.01.261018", cur, "1.0.01.261020") is None
    assert ju.visible_update("1.0.01.261030", cur, "1.0.01.261018") == "1.0.01.261030"   # 더 새 버전은 다시 알림
    assert ju.visible_update("1.0.01.261018", cur, "1.0.01.261018", ignore_dismissed=True) == "1.0.01.261018"
    assert ju.visible_update("1.0.01.261017", cur, "") is None
    assert ju.visible_update("1.0.01.261018", cur, "weird") == "1.0.01.261018"


def test_release_url():
    assert ju.release_url("1.0.01.261099").endswith("/releases/tag/v1.0.01.261099")
    assert ju.release_url("evil/../x") == ju.LATEST_URL and ju.release_url(None) == ju.LATEST_URL


def test_corrupt_state_file():
    p = os.path.join(os.environ["JH_APP_DIR"], "data", ju.STATE_FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write('{"latest": 5, "last_check": "x", "consecutive_fail": -3}')
    c = ju.UpdateChecker("ua", fetch=Fetch(OK))
    assert c.latest is None and c.state["consecutive_fail"] == 0


def test_summary_line():
    c, f, _ = mk(OK)
    assert "미확인" in c.summary()
    c.check()
    s = c.summary()
    assert "1.0.01.261099" in s and "정상" in s and "마지막 확인" in s


def test_settings_defaults_and_validation():
    assert jhm.DEFAULT_SETTINGS["check_updates"] is True and jhm.DEFAULT_SETTINGS["dismissed_update_version"] == ""
    p = os.path.join(jhm.get_settings_dir(), "settings.json")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"check_updates": "no", "dismissed_update_version": "zzz"}))
    s = jhm.load_settings()
    assert s["check_updates"] is True and s["dismissed_update_version"] == ""
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"check_updates": False, "dismissed_update_version": "1.0.01.261099"}))
    s = jhm.load_settings()
    assert s["check_updates"] is False and s["dismissed_update_version"] == "1.0.01.261099"


def test_english_translations_exist():
    for k in ("🔔 새 버전 {} 있음 (메뉴에서 받으러 가기)", "🔔 새 버전 {} 받으러 가기", "🔔 지금 업데이트 확인",
              "최신 버전입니다 (현재 {})", "새 버전 {} 있음", "확인하지 못했어요. 잠시 뒤 다시 시도해 주세요",
              "{}분 뒤에 다시 확인할 수 있어요", "이 버전은 알리지 않음", "받으러 가기", "새 버전 확인"):
        assert k in jhm._TRANSLATIONS_EN


# ---- 호스트 연동: 메뉴 항목 / 진단 / 상단바 알림 ----
pytest.importorskip("PyQt5")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def fake_host(latest, dismissed=""):
    h = types.SimpleNamespace()
    h.settings = dict(jhm.DEFAULT_SETTINGS, dismissed_update_version=dismissed)
    h.update_checker = types.SimpleNamespace(latest=latest)
    h.status = types.SimpleNamespace(collapsed=False)
    h._hidden = False
    for n in ("_toggle_collapsed", "manual_reset", "refresh_db", "_toggle_hidden", "open_settings", "quit_app", "open_release_page"):
        setattr(h, n, lambda *a: None)
    h.pending_update = lambda: jhm.MouseHoverManager.pending_update(h)
    return h


def menu_texts(h):
    m = jhm.MouseHoverManager.build_main_menu(h)       # (메뉴가 살아 있는 동안만 액션이 유효)
    return [a.text() for a in m.actions()]


@pytest.mark.parametrize("latest,dismissed,shown", [
    ("9.9.99.999999", "", True),
    (jhm.APP_VERSION, "", False),
    ("0.0.01.000001", "", False),
    ("garbage", "", False),
    (None, "", False),
    ("9.9.99.999999", "9.9.99.999999", False),
    ("9.9.99.999999", "9.9.99.999998", True),
])
def test_menu_item_condition(app, latest, dismissed, shown):
    texts = menu_texts(fake_host(latest, dismissed))
    assert any("🔔" in t and "받으러 가기" in t for t in texts) is shown
    if shown:
        assert "9.9.99.999999" in texts[0]


def test_diagnostics_has_update_line(app):
    import jh_settings_ui as ui
    c, f, _ = mk(OK)
    c.check()
    h = types.SimpleNamespace(settings=dict(jhm.DEFAULT_SETTINGS), db_event_cache=[], lang="ko", update_checker=c,
                              store=types.SimpleNamespace(meta={}, base_url="x", network_summary=lambda: "net"))
    assert "업데이트 확인: 최신 1.0.01.261099" in ui.build_diagnostics(h, jhm, now=0)


def test_on_update_checked_notice_once_and_dismissed(app, monkeypatch):
    calls = []
    monkeypatch.setattr(jhm.QTimer, "singleShot", staticmethod(lambda ms, fn: calls.append(ms)))
    res = ju.UpdateResult("updated", "9.9.99.999999", "", 0)
    f = jhm.MouseHoverManager.on_update_checked
    h = fake_host("9.9.99.999999")
    h._update_notified = False
    f(h, res, False)
    f(h, res, False)
    assert calls == [5000]                           # 앱 실행당 최대 1회
    h2 = fake_host("9.9.99.999999", "9.9.99.999999")
    h2._update_notified = False
    calls.clear()
    f(h2, res, False)
    assert calls == []                               # dismissed 면 알림 없음
    h3 = fake_host("9.9.99.999999")
    h3._update_notified = False
    f(h3, res, True)
    assert calls == []                               # 수동 확인은 상단바 알림을 예약하지 않음


# ---- 게시된 릴리즈가 없을 때(GitHub 가 /releases 로 보냄): 실패가 아니라 '최신' ----
NO_REL = (302, "https://github.com/QooReche/starsavior-journey-lite/releases")


def test_no_release_is_success_not_error():
    c, f, clock = mk(NO_REL)
    r = c.check(enabled=True, manual=True)
    assert r.status == "updated" and r.latest == ""                 # 실패(error) 가 아님
    assert c.state["consecutive_fail"] == 0 and c.state["next_retry"] == 0 and c.state["last_status"] == "정상"
    assert ju.visible_update(r.latest, "1.0.02.261017") is None      # 새 버전으로 표시되지 않음


def test_no_release_respects_12h_cache():
    c, f, clock = mk(NO_REL)
    c.check(enabled=True)
    assert f.calls == 1
    r = c.check(enabled=True)                                        # 12시간 안: 다시 묻지 않음
    assert f.calls == 1 and r.status == "cached"


def test_no_release_ui_says_latest():
    import journey_helper as jhm
    import jh_settings_ui as ui
    from types import SimpleNamespace
    res = ju.UpdateResult("updated", "", "확인함", 0)
    d = ui.SettingsDialog.__new__(ui.SettingsDialog)
    texts = {}
    d.tr_ = lambda s: s
    d.jhm = jhm
    d.btn_check = SimpleNamespace(setEnabled=lambda v: None)
    d.btn_get = SimpleNamespace(hide=lambda: None, show=lambda: texts.setdefault("get", True))
    d.lbl_check = SimpleNamespace(setText=lambda s: texts.__setitem__("t", s))
    d._update_banner = lambda: None
    d._on_check_done(res)
    assert "최신 버전입니다" in texts["t"] and "get" not in texts


def test_real_error_still_error():
    c, f, clock = mk((200, None))                                    # 302 가 아닌 응답은 여전히 실패
    assert c.check(enabled=True, manual=True).status == "error"
    os.remove(c.path())                                              # 앞 검사기의 저장 상태(쿨다운)를 이어받지 않게
    c2, f2, _ = mk(OSError("net"))
    assert c2.check(enabled=True, manual=True).status == "error"
