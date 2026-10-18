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
    dist = tmp_path / "JourneyHelperLite"
    dist.mkdir()
    (dist / "JourneyHelperLite.exe").write_bytes(b"MZ")
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
