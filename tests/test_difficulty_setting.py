# -*- coding: utf-8 -*-
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


def test_default_is_all(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert jhm.DEFAULT_SETTINGS["difficulty"] == "all"
    assert jhm.load_settings()["difficulty"] == "all"


def test_roundtrip_and_invalid_value(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    st = jhm.load_settings()
    st["difficulty"] = "hard"
    jhm.save_settings(st)
    assert jhm.load_settings()["difficulty"] == "hard"
    d = os.path.join(str(tmp_path), jhm.APP_NAME, "settings.json")
    with open(d, "w", encoding="utf-8") as f:
        json.dump({"difficulty": "nightmare"}, f)
    assert jhm.load_settings()["difficulty"] == "all"


def test_menu_choices_and_english_labels():
    assert [v for _, v in jhm.DIFFICULTY_CHOICES] == ["all", "easy", "normal", "hard"]
    jhm.set_language("en")
    try:
        assert [jhm.tr(l) for l, _ in jhm.DIFFICULTY_CHOICES] == ["All", "Easy", "Normal", "Hard"]
        assert jhm.tr("🎚️ 난이도 표시") == "🎚️ Difficulty filter"
    finally:
        jhm.set_language("ko")
