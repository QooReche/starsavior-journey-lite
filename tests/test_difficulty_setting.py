# -*- coding: utf-8 -*-
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402
import journey_helper as jhm  # noqa: E402


def _write(tmp_path, data):
    d = os.path.join(str(tmp_path), "data")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_default_hides_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path))
    st = jhm.load_settings()
    assert st["hidden_difficulties"] == []
    assert st["difficulty"] == "all"


def test_roundtrip_and_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path))
    st = jhm.load_settings()
    st["hidden_difficulties"] = ["hard", "easy"]
    jhm.save_settings(st)
    assert jhm.load_settings()["hidden_difficulties"] == ["easy", "hard"]
    _write(tmp_path, {"hidden_difficulties": ["nightmare", "normal"], "difficulty": "nightmare"})
    st = jhm.load_settings()
    assert st["hidden_difficulties"] == ["normal"]
    assert st["difficulty"] == "all"
    _write(tmp_path, {"hidden_difficulties": "hard"})        # 잘못된 형식 -> 숨김 없음
    assert jhm.load_settings()["hidden_difficulties"] == []


def test_migrates_old_single_difficulty(tmp_path, monkeypatch):
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path))
    _write(tmp_path, {"difficulty": "hard"})
    assert jhm.load_settings()["hidden_difficulties"] == ["easy", "normal"]
    _write(tmp_path, {"difficulty": "hard", "hidden_difficulties": []})   # 새 키가 있으면 그대로
    assert jhm.load_settings()["hidden_difficulties"] == []


def test_toggle_hidden_difficulty():
    assert jhm.toggle_hidden_difficulty([], "hard", True) == ["hard"]
    assert jhm.toggle_hidden_difficulty(["hard"], "easy", True) == ["easy", "hard"]
    assert jhm.toggle_hidden_difficulty(["easy", "hard"], "hard", False) == ["easy"]
    assert jhm.toggle_hidden_difficulty(None, "bogus", True) == []


def test_hidden_set_semantics():
    assert jh_db.hidden_set("all") == set()
    assert jh_db.hidden_set("normal") == {"easy", "hard"}
    assert jh_db.hidden_set(["easy", "x"]) == {"easy"}
    assert jh_db.hidden_set(None) == set()


def test_menu_labels_english():
    assert [v for _, v in jhm.DIFFICULTY_CHOICES] == ["all", "easy", "normal", "hard"]
    jhm.set_language("en")
    try:
        assert [jhm.tr(l) for l, _ in jhm.DIFFICULTY_CHOICES[1:]] == ["Easy", "Normal", "Hard"]
        assert jhm.tr("🎚️ 툴팁에서 숨길 난이도") == "🎚️ Difficulties to hide from tooltip"
        assert jhm.tr("🔤 왼쪽 툴팁 글자 크기") == "🔤 Left tooltip font size"
    finally:
        jhm.set_language("ko")


def test_font_scale_steps_match_original(tmp_path, monkeypatch):
    assert [v for _, v in jhm.FONT_SCALE_CHOICES] == [0.85, 1.0, 1.2, 1.4]
    assert jhm.snap_font_scale(1.25) == 1.2
    assert jhm.snap_font_scale(1.5) == 1.4
    assert jhm.snap_font_scale("x") == 1.0
    monkeypatch.setenv("JH_APP_DIR", str(tmp_path))
    _write(tmp_path, {"tooltip_font_scale": 1.5})
    assert jhm.load_settings()["tooltip_font_scale"] == 1.4
