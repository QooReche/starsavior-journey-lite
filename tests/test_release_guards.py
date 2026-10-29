# -*- coding: utf-8 -*-
"""릴리즈 전 코드 리뷰(2026-10-18) 지적 사항 회귀: DB 갱신 중복 방지, 응답 크기 상한, 라이선스 고지문 생성"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def test_second_db_refresh_is_skipped_while_one_is_running():
    pytest.importorskip("PyQt5")
    import journey_helper as jhm
    running = SimpleNamespace(isRunning=lambda: True)
    fake = SimpleNamespace(_db_threads=[running], store=None)
    jhm.MouseHoverManager.start_db(fake, True)             # 이미 갱신 중이면 새 스레드를 만들지 않는다(store=None 이라 만들었다면 예외)
    assert fake._db_threads == [running]


def test_finished_db_threads_are_pruned(monkeypatch):
    pytest.importorskip("PyQt5")
    import journey_helper as jhm
    done = SimpleNamespace(isRunning=lambda: False)
    started = []

    class FakeThread:
        def __init__(self, store, manual=False):
            self.finished_ok = SimpleNamespace(connect=lambda f: None)
            started.append(self)

        def isRunning(self):
            return False

        def start(self):
            pass

    monkeypatch.setattr(jhm, "DbThread", FakeThread)
    fake = SimpleNamespace(_db_threads=[done], store=None, on_db_loaded=lambda *a: None)
    jhm.MouseHoverManager.start_db(fake, False)
    assert len(started) == 1 and fake._db_threads == [started[0]]       # 끝난 스레드는 목록에서 빠지고 새로 하나만 시작


def test_card_download_over_size_cap_is_rejected(monkeypatch, tmp_path):
    import jh_cards

    class Resp:
        def __init__(self):
            self.n = 0

        def read(self, size):
            self.n += 1
            return b"x" * size                              # 끝나지 않는 응답

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(jh_cards.urllib.request, "urlopen", lambda req, timeout=None: Resp())
    store = jh_cards.CardStore(str(tmp_path), download=True, sleep=0)
    with pytest.raises(ValueError):
        store._fetch(1)
    assert jh_cards.classify_error(ValueError("image too large")) == "missing"   # 서킷브레이커를 열지 않고 그 카드만 음성 캐시


def test_default_user_agent_points_to_this_repo():
    import jh_db
    assert "starsavior-journey-lite" in jh_db.DEFAULT_USER_AGENT and "QooReche/journeyhelper" not in jh_db.DEFAULT_USER_AGENT
    assert jh_db.MAX_JSON_BYTES >= 5 * 1024 * 1024


def test_collect_licenses_fills_dist(tmp_path):
    pytest.importorskip("requests")
    sys.path.insert(0, os.path.join(SRC, "tools"))
    import collect_licenses
    dist = tmp_path / "JourneyNavigator"
    dist.mkdir()
    (dist / "JourneyNavigator.exe").write_bytes(b"MZ")
    assert collect_licenses.main([str(dist), "--src", SRC]) == 0
    for need in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", os.path.join("licenses", "Apache-2.0.txt")):
        assert (dist / need).is_file(), need
    notices = (dist / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert "PyQt5" in notices and "RapidOCR" in notices and "GPL-3.0" in notices
    assert (dist / "LICENSE").read_text(encoding="utf-8").startswith("                    GNU GENERAL PUBLIC LICENSE")


def test_license_file_is_gpl3_and_readme_mentions_it():
    lic = open(os.path.join(SRC, "LICENSE"), encoding="utf-8").read()
    readme = open(os.path.join(SRC, "README.md"), encoding="utf-8").read()
    assert "GNU GENERAL PUBLIC LICENSE" in lic and "Version 3" in lic and "GPL-3.0" in readme


# ---- 툴팁 깜빡임: OCR 글자만 흔들리는 같은 이벤트는 다시 만들지 않는다 (2026-10-18 '분홍색 연구') ----
def _event_for(title):
    import jh_db
    return {"rawTitle": title, "cleanTitle": jh_db.clean_title(title), "isArcana": False, "timing": "", "sources": [],
            "options": [{"choice": "a", "reward": "힘 +1", "variants": [], "info": []},
                        {"choice": "b", "reward": "체력 +1", "variants": [], "info": []}]}


def _fake_manager(matched, events):
    cleared = []
    return SimpleNamespace(
        current_title="분홍색연구G", matched=matched, db_event_cache=events, ambig=[],
        choice_lock=SimpleNamespace(active=False),
        tooltip=SimpleNamespace(hide_and_clear=lambda: cleared.append(1)), cleared=cleared,
        _is_same_event_as_current=None)


def test_noisy_same_title_does_not_reset_tooltip():
    pytest.importorskip("PyQt5")
    import journey_helper as jhm
    pink = _event_for("분홍색 연구")
    other = _event_for("미래를 위한 선택")
    fake = _fake_manager(pink, [pink, other])
    fake._is_same_event_as_current = lambda c, k: jhm.MouseHoverManager._is_same_event_as_current(fake, c, k)
    for noisy in (["분홍색영구", "1분홍색연구G"], ["1분홍색연구C", "분홍색연구"], ["분홍색연구시C", "분홍색연구"]):
        jhm.MouseHoverManager.on_title(fake, noisy, None)
        assert fake.cleared == [] and fake.current_title == "분홍색연구G"          # 숨기지도, 제목을 바꾸지도 않는다


def test_different_event_still_resets():
    pytest.importorskip("PyQt5")
    import journey_helper as jhm
    pink = _event_for("분홍색 연구")
    other = _event_for("미래를 위한 선택")
    fake = _fake_manager(pink, [pink, other])
    assert jhm.MouseHoverManager._is_same_event_as_current(fake, ["미래를위한선택", "미래를위한선택비"], None) is False
    assert jhm.MouseHoverManager._is_same_event_as_current(fake, ["분홍색연구", "1분홍색연구"], None) is True


def test_version_info_resource_has_product_and_version(tmp_path):
    """백신 휴리스틱 오탐 완화: PyInstaller exe 에 제품명/버전/라이선스를 넣는다(tools/make_version_info.py)."""
    pytest.importorskip("PyInstaller")
    sys.path.insert(0, os.path.join(SRC, "tools"))
    import make_version_info
    from jh_version import APP_VERSION
    out = tmp_path / "version_info.txt"
    assert make_version_info.main(str(out)) == 0
    txt = out.read_text(encoding="utf-8")
    for need in ("여정 내비게이터", "JourneyNavigator.exe", APP_VERSION, "GPL-3.0", "'041204b0'"):
        assert need in txt, need
    assert "filevers=(1, 0, 4, 0)" in txt                          # 숫자 버전은 16비트라 빌드 날짜를 못 담는다


# 툴팁 글자색·크기는 jh_common.span() 으로 만든다. 인라인 <span style="color:..."> 줄 수는 늘리지 않고 줄여 나간다(래칫).
# 줄이면 이 숫자도 같이 내린다. 늘려야 한다면 span() 으로 쓸 수 없는 이유가 있어야 한다.
INLINE_SPAN_CEILING = {"jh_db.py": 23, "jh_schedule.py": 9, "jh_savior.py": 7, "jh_relic.py": 2}


@pytest.mark.parametrize("name,ceiling", sorted(INLINE_SPAN_CEILING.items()))
def test_inline_span_styles_do_not_grow(name, ceiling):
    with open(os.path.join(SRC, name), encoding="utf-8") as f:
        n = sum(1 for line in f if 'style="color:' in line)
    assert n <= ceiling, f"{name}: 인라인 span 스타일 줄 {n}개 (상한 {ceiling}). jh_common.span() 을 쓰세요"
