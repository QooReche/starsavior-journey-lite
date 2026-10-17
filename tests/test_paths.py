# -*- coding: utf-8 -*-
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import jh_cards  # noqa: E402
import jh_db  # noqa: E402
import jh_paths as P  # noqa: E402
import journey_helper as jhm  # noqa: E402

STAMP = os.path.join(os.path.dirname(__file__), "..", "tools", "stamp_version.py")


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    monkeypatch.delenv("JH_LOG_PATH", raising=False)       # 실제 경로 로직을 시험(임시 앱 폴더)
    root = tmp_path / "한글 앱"
    root.mkdir()
    monkeypatch.setenv("JH_APP_DIR", str(root))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    P.reset_for_tests()
    return root


def test_portable_layout_dev_and_frozen(app_root, monkeypatch):
    assert P.is_portable()
    assert P.data_dir() == str(app_root / "data") and P.log_dir() == str(app_root / "log")
    assert P.cache_dir() == str(app_root / "data" / "cache")
    assert jh_db.default_cache_dir() == P.cache_dir()
    assert jh_cards.default_cache_dir() == os.path.join(P.cache_dir(), "cards")
    assert jhm.get_settings_dir() == P.data_dir()
    monkeypatch.delenv("JH_APP_DIR")
    assert P.app_dir() == os.path.dirname(os.path.abspath(P.__file__))          # 개발 실행: 소스 폴더
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_root / "x" / "JourneyHelperLite.exe"))
    assert P.app_dir() == str(app_root / "x")                                   # frozen: 실행 파일 폴더


def test_fallback_when_unwritable(app_root, monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_writable", lambda d: False)
    P.reset_for_tests()
    assert not P.is_portable()
    legacy = str(tmp_path / "roaming" / "JourneyHelperLite")
    assert P.data_dir() == legacy and P.log_dir() == legacy
    assert P.display_path("log").startswith("%APPDATA%")
    P.write_log("x")
    text = open(os.path.join(legacy, "log.txt"), encoding="utf-8").read()
    assert "쓸 수 없어" in text                                                  # 폴백 안내 한 줄
    assert "fallback" in P.usage_summary()


def test_bundled_assets_do_not_collide_with_runtime_data():
    base = os.path.dirname(P.__file__)
    assert os.path.isfile(os.path.join(base, "assets", "savior_reco.json"))
    assert not os.path.exists(os.path.join(base, "data", "savior_reco.json"))
    assert jhm.jh_savior.load_reco()


def test_delete_tolerance_save_log_cache(app_root):
    st = jhm.load_settings()
    jhm.save_settings(st)
    jhm.log("첫 줄")
    store = jh_cards.CardStore(download=False)
    shutil.rmtree(app_root / "data")
    shutil.rmtree(app_root / "log")
    jhm.save_settings(st)
    jhm.log("둘째 줄")
    assert (app_root / "data" / "settings.json").is_file()
    assert "둘째 줄" in (app_root / "log" / "log.txt").read_text(encoding="utf-8")
    shutil.rmtree(app_root / "data")
    s = jh_db.DbStore()
    s._save_meta()                                                   # cache 폴더 없이도 저장 시 재생성
    assert (app_root / "data" / "cache" / "meta.json").is_file()
    shutil.rmtree(app_root / "data")
    assert store.get_image(1) is None                                # 읽기는 없으면 None
    assert jhm.load_settings()["language"] == "ko"                   # 없으면 기본값
    assert jh_db.load_custom_events(os.path.join(jhm.get_settings_dir(), "custom_events.json"))
    assert (app_root / "data" / "custom_events.json").is_file()


def test_broken_settings_recover_to_defaults(app_root):
    (app_root / "data").mkdir(exist_ok=True)
    (app_root / "data" / "settings.json").write_text("{깨짐", encoding="utf-8")
    assert jhm.load_settings()["language"] == "ko"


def _make_legacy(tmp_path):
    leg = tmp_path / "roaming" / "JourneyHelperLite"
    (leg / "cache" / "cards").mkdir(parents=True)
    (leg / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    (leg / "custom_events.json").write_text("{}", encoding="utf-8")
    (leg / "cache" / "cards" / "1.png").write_bytes(b"png")
    (leg / "log.txt").write_text("old log", encoding="utf-8")
    return leg


def test_migration_copies_only_and_is_idempotent(app_root, tmp_path):
    leg = _make_legacy(tmp_path)
    before = sorted(str(p.relative_to(leg)) for p in leg.rglob("*"))
    assert jhm.load_settings()["language"] == "en"                   # 새 위치로 복사된 설정을 읽음
    d = app_root / "data"
    assert (d / "settings.json").is_file() and (d / "custom_events.json").is_file() and (d / "cache" / "cards" / "1.png").is_file()
    assert sorted(str(p.relative_to(leg)) for p in leg.rglob("*")) == before            # 원본 유지
    assert (leg / "settings.json").read_text(encoding="utf-8") == json.dumps({"language": "en"})
    jhm.log("시작")
    logtxt = (app_root / "log" / "log.txt").read_text(encoding="utf-8")
    assert "[이전]" in logtxt and "old log" not in logtxt            # 로그는 새로 시작
    (d / "settings.json").write_text(json.dumps({"language": "ko"}), encoding="utf-8")
    P.reset_for_tests()
    assert jhm.load_settings()["language"] == "ko"                   # 두 번째 실행: 덮어쓰지 않음
    assert not P.migrate_legacy(str(d), str(leg))


def test_log_rotation_keeps_one_old(app_root):
    jhm.log("a")
    p = app_root / "log" / "log.txt"
    with open(p, "ab") as f:
        f.write(b"x" * (P.LOG_MAX_BYTES + 5))
    jhm.log("b")
    old = app_root / "log" / "log.old.txt"
    assert old.is_file() and p.stat().st_size < 1000
    with open(p, "ab") as f:
        f.write(b"y" * (P.LOG_MAX_BYTES + 5))
    jhm.log("c")
    assert b"y" in old.read_bytes()[-100:] and not (app_root / "log" / "log.txt.1").exists()
    assert jhm.LOG_MAX_BYTES == 1024 * 1024


def test_log_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("JH_LOG_PATH", str(tmp_path / "sub" / "t.txt"))
    jhm.log("z")
    assert (tmp_path / "sub" / "t.txt").is_file()


def test_readme_created_recreated_bom_and_not_counted(app_root):
    jhm.log("x")
    P.data_dir()
    for kind in ("data", "log"):
        f = app_root / kind / P.README_NAME
        raw = f.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert "JourneyHelperLite" in raw.decode("utf-8-sig") or "settings.json" in raw.decode("utf-8-sig")
        f.unlink()
    shutil.rmtree(app_root / "log")
    jhm.log("y")
    P.data_dir()
    assert (app_root / "log" / P.README_NAME).is_file() and (app_root / "data" / P.README_NAME).is_file()
    assert "log.old.txt" in (app_root / "log" / P.README_NAME).read_text(encoding="utf-8-sig")
    assert "settings.json" in (app_root / "data" / P.README_NAME).read_text(encoding="utf-8-sig")
    assert P.dir_size(str(app_root / "log")) == (app_root / "log" / "log.txt").stat().st_size       # README 제외
    assert P.dir_size(str(app_root / "data")) == 0


def test_readme_english(app_root, monkeypatch):
    monkeypatch.setattr(P, "LANG", "en")
    P.data_dir()
    assert "This folder stores" in (app_root / "data" / P.README_NAME).read_text(encoding="utf-8-sig")


def test_clear_cache_keeps_settings(app_root):
    jhm.save_settings(jhm.load_settings())
    c = P.cache_dir()
    open(os.path.join(c, "meta.json"), "w").write("{}")
    P.clear_cache()
    assert os.path.isdir(c) and os.listdir(c) == []
    assert (app_root / "data" / "settings.json").is_file()


def test_clear_logs_then_log_again(app_root):
    jhm.log("a")
    P.clear_logs()
    assert not (app_root / "log" / "log.txt").exists()
    jhm.log("b")
    assert (app_root / "log" / "log.txt").is_file()


# ---------------------------------------------------------------- 버전
def test_version_format_and_single_source():
    import jh_version
    assert re.fullmatch(r"\d+\.\d+\.\d{2}\.\d{6}", jh_version.APP_VERSION)
    assert jhm.APP_VERSION == jh_version.APP_VERSION
    assert jh_version.APP_VERSION in jh_db.DEFAULT_USER_AGENT and jh_version.APP_VERSION in jh_cards.USER_AGENT
    src = open(os.path.join(os.path.dirname(__file__), "..", "journey_helper.py"), encoding="utf-8").read()
    assert not re.search(r'^APP_VERSION\s*=\s*"', src, flags=re.M) and "1.0.6-lite" not in src


def test_stamp_version_on_copy(tmp_path):
    import stamp_version
    import jh_version
    cp = tmp_path / "jh_version.py"
    shutil.copy(os.path.join(os.path.dirname(__file__), "..", "jh_version.py"), cp)
    assert stamp_version.stamp(str(cp), "270102") == f"{jh_version.BASE_VERSION}.270102"
    assert 'BUILD_DATE = "270102"' in cp.read_text(encoding="utf-8")
    r = subprocess.run([sys.executable, "-X", "utf8", STAMP, "--file", str(cp), "--date", "261017"], capture_output=True, text=True)
    assert r.stdout.strip() == f"{jh_version.BASE_VERSION}.261017"
    today = subprocess.run([sys.executable, "-X", "utf8", STAMP, "--file", str(cp)], capture_output=True, text=True).stdout.strip()
    assert re.fullmatch(re.escape(jh_version.BASE_VERSION) + r"\.\d{6}", today)
    with pytest.raises(ValueError):
        stamp_version.stamp(str(cp), "2610")
