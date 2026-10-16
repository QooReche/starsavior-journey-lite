# -*- coding: utf-8 -*-
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


def load_with(tmp_path, monkeypatch, data):
    monkeypatch.setattr(jhm, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    return jhm.load_settings()


def test_bad_types_fall_back(tmp_path, monkeypatch):
    s = load_with(tmp_path, monkeypatch, {"status_badge_offset": [5], "low_spec_mode": "false"})
    assert s["status_badge_offset"] is None and s["low_spec_mode"] is False


def test_valid_values_kept(tmp_path, monkeypatch):
    s = load_with(tmp_path, monkeypatch, {"status_badge_offset": [5, 10.5], "low_spec_mode": True})
    assert s["status_badge_offset"] == [5, 10.5] and s["low_spec_mode"] is True
