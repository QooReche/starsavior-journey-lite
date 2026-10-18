# -*- coding: utf-8 -*-
"""앱 폴더 경로에 한글·공백이 있을 때(예: C:\\Users\\홍길동\\바탕 화면\\여정 도우미\\JourneyHelperLite) 진짜 디스크 I/O 가 되는지.

2026-10-18: cv2.imwrite 가 한글 경로에 저장하지 못해 카드 그림이 안 뜨던 결함을 pytest 의 영문 tmp_path 가 가렸다.
여기서는 모든 파일 입출력을 한글+공백 폴더에서 실제로 실행한다 (가짜는 네트워크 응답뿐)."""
import io
import json
import os
import shutil
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_cards  # noqa: E402
import jh_db  # noqa: E402
import jh_net  # noqa: E402
import jh_paths as P  # noqa: E402
import jh_update  # noqa: E402
import jh_v2  # noqa: E402
import journey_helper as jhm  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def kr(tmp_path, monkeypatch):
    """한글+공백 앱 폴더. 실제 경로 로직(JH_LOG_PATH 해제)을 시험하되 모두 임시 폴더 안."""
    monkeypatch.delenv("JH_LOG_PATH", raising=False)
    root = tmp_path / "바탕 화면 한글" / "여정 도우미 폴더"
    root.mkdir(parents=True)
    monkeypatch.setenv("JH_APP_DIR", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "구 로밍 폴더"))
    P.reset_for_tests()
    assert not str(root).isascii() and " " in str(root)
    return root


class Resp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class Sess:
    def get(self, url, headers=None, timeout=None):
        if "pages.dev" in url:
            p = os.path.join(FIX, "v2", url.rsplit("/", 1)[1])
        elif url.endswith("arcanas.json"):
            p = os.path.join(FIX, "arcanas", "arcanas.json")
        else:
            p = os.path.join(FIX, "journey", "ko", "journey.json")
        with open(p, "rb") as f:
            return Resp(200, f.read(), {"ETag": '"x"'})


def test_settings_log_readme_rotation(kr):
    s = dict(jhm.DEFAULT_SETTINGS)
    s["language"] = "en"
    jhm.save_settings(s)
    sp = kr / "data" / "settings.json"
    assert sp.is_file() and json.loads(sp.read_text(encoding="utf-8"))["language"] == "en"
    assert jhm.load_settings()["language"] == "en"
    jhm.log("한글 경로 로그")
    lp = kr / "log" / "log.txt"
    assert "한글 경로 로그" in lp.read_text(encoding="utf-8")
    lp.write_bytes(b"x" * (P.LOG_MAX_BYTES + 10))                 # 1MB 초과 -> 다음 기록 때 회전
    jhm.log("회전 후")
    old = kr / "log" / "log.old.txt"
    assert old.is_file() and old.stat().st_size > P.LOG_MAX_BYTES
    assert lp.read_text(encoding="utf-8").strip().endswith("회전 후") and lp.stat().st_size < 1000
    for sub in ("data", "log"):                                   # README (UTF-8 BOM)
        raw = (kr / sub / P.README_NAME).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") and "폴더".encode("utf-8") in raw


def test_dbstore_update_and_reload(kr):
    st = jh_db.DbStore(session=Sess(), seed_dir=None, clock=lambda: 1000.0)       # 기본 캐시 폴더 = 한글 data\cache
    assert os.path.abspath(st.cache_dir).startswith(str(kr))
    r = st.update()
    assert r.status == "updated"
    files = [os.path.join(dp, n) for dp, _d, fs in os.walk(kr / "data" / "cache") for n in fs]
    assert any(f.endswith("meta.json") for f in files) and any(f.endswith("arcanas.json") for f in files)
    assert not [f for f in files if f.endswith(".tmp")]
    st2 = jh_db.DbStore(session=Sess(), seed_dir=None)
    j, a = st2.load()
    assert j and a and st2.has_cache() and not st2.seed_used
    assert st2.meta["files"] and st2.meta["last_check"] == 1000.0               # meta 재로드


def test_v2store_update_and_reload(kr):
    st = jh_v2.V2Store(session=Sess(), seed_dir=None, clock=lambda: 1000.0)
    assert st.update().status == "updated"
    for rel in jh_v2.V2_FILES.values():
        assert (kr / "data" / "cache" / "v2" / rel).is_file()
    st2 = jh_v2.V2Store(session=Sess(), seed_dir=None)
    d = st2.load()
    assert d and d["journeys"] and st2.meta["files"]


def test_seed_fallback_from_korean_seed_dir(kr):
    seed = kr / "assets" / "seed"                                # 시드도 한글 경로에 둔다 (앱 폴더 안 _internal\assets\seed 와 같은 상황)
    (seed / "v2").mkdir(parents=True)
    shutil.copy(os.path.join(FIX, "journey", "ko", "journey.json"), seed / "journey.json")
    shutil.copy(os.path.join(FIX, "arcanas", "arcanas.json"), seed / "arcanas.json")
    for rel in jh_v2.V2_FILES.values():
        shutil.copy(os.path.join(FIX, "v2", rel), seed / "v2" / rel)
    st = jh_db.DbStore(session=Sess(), seed_dir=str(seed))
    j, a = st.load()
    assert j and a and st.seed_used and st.using_seed
    assert st.load_v2()["journeys"] and st.v2_seed_used
    assert not (kr / "data" / "cache" / "meta.json").exists()   # 시드는 meta 를 만들지 않는다


def test_card_store_real_save_and_read_back(kr, monkeypatch):
    ok, buf = cv2.imencode(".png", np.full((40, 30, 3), 128, np.uint8))
    data = buf.tobytes()

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(jh_cards.urllib.request, "urlopen", lambda *a, **k: R(data))
    s = jh_cards.CardStore(sleep=0)                              # 기본 캐시 폴더 (한글 경로)
    assert str(kr) in s.cache_dir
    img = s.get_image(7)
    assert img is not None and os.path.isfile(os.path.join(s.cache_dir, "7.png"))
    assert sorted(os.listdir(s.cache_dir)) == ["7.png"]
    s2 = jh_cards.CardStore(download=False)
    back = s2.get_image(7)                                       # 디스크에서 다시 읽기
    assert back is not None and back.shape == img.shape


def test_update_state_and_custom_events(kr):
    c = jh_update.UpdateChecker("ua", fetch=lambda *a, **k: None, clock=lambda: 5.0)
    c.state["last_check"] = 123.0
    c._save()
    p = kr / "data" / "update_state.json"
    assert p.is_file() and json.loads(p.read_text(encoding="utf-8"))["last_check"] == 123.0
    assert jh_update.UpdateChecker("ua", fetch=lambda *a, **k: None).state["last_check"] == 123.0
    ce = kr / "data" / "custom_events.json"
    d = jh_db.load_custom_events(str(ce))                        # 없으면 기본 파일 생성
    assert ce.is_file() and d
    ce.write_text(json.dumps({"테스트 이벤트": "안내문"}, ensure_ascii=False), encoding="utf-8-sig")
    assert jh_db.load_custom_events(str(ce)) == {"테스트 이벤트": "안내문"}


def test_card_debug_saved_and_trimmed(kr):
    img = np.full((20, 20, 3), 90, np.uint8)
    d = kr / "data" / "card_debug"
    d.mkdir(parents=True)
    for i in range(7):                                           # 오래된 파일 7개 -> keep=5 로 정리
        (d / f"00000{i}.png").write_bytes(b"old")
    jhm.save_card_debug(img, keep=5)
    names = sorted(os.listdir(d))
    assert len(names) == 5
    assert any(cv2.imdecode(np.fromfile(str(d / n), np.uint8), cv2.IMREAD_COLOR) is not None for n in names)


def test_migration_from_korean_legacy_appdata(kr, tmp_path, monkeypatch):
    leg = tmp_path / "새 로밍 한글" / "JourneyHelperLite"
    (leg / "cache" / "cards").mkdir(parents=True)
    (leg / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    (leg / "custom_events.json").write_text(json.dumps({"가": "나"}, ensure_ascii=False), encoding="utf-8")
    (leg / "cache" / "cards" / "1.png").write_bytes(b"png")
    monkeypatch.setenv("APPDATA", str(leg.parent))
    P.reset_for_tests()
    assert jhm.load_settings()["language"] == "en"
    d = kr / "data"
    assert json.loads((d / "custom_events.json").read_text(encoding="utf-8")) == {"가": "나"}
    assert (d / "cache" / "cards" / "1.png").read_bytes() == b"png"
    assert (leg / "settings.json").is_file()                      # 원본 유지


def test_clear_cache_logs_usage_and_diagnostics(kr):
    jhm.save_settings(dict(jhm.DEFAULT_SETTINGS))
    st = jh_db.DbStore(session=Sess(), seed_dir=None, clock=lambda: 1000.0)
    st.update()
    jhm.log("진단용 한글 줄")
    (kr / "log" / "crash_log.txt").write_text("boom", encoding="utf-8")
    size = P.dir_size(P.cache_dir())
    assert size > 1000 and "cache=" in P.usage_summary() and "portable" in P.usage_summary()
    ui = pytest.importorskip("jh_settings_ui")

    class Host:
        settings = dict(jhm.DEFAULT_SETTINGS, hidden_difficulties=[])
        lang = "ko"
        db_event_cache = [1]
        store = st
    text = ui.build_diagnostics(Host(), jhm)
    assert "진단용 한글 줄" in text and "data location: portable" in text
    assert P.clear_cache() and os.listdir(P.cache_dir()) == []
    assert (kr / "data" / "settings.json").is_file()
    assert P.clear_logs() and not (kr / "log" / "log.txt").exists() and not (kr / "log" / "crash_log.txt").exists()
    jhm.log("다시")
    assert "다시" in (kr / "log" / "log.txt").read_text(encoding="utf-8")
