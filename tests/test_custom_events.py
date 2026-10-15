# -*- coding: utf-8 -*-
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402

TITLE = "운명이 이어 준 첫 번째 인연"


def test_default_file_created_with_fate_event(tmp_path):
    p = str(tmp_path / "custom_events.json")
    c = jh_db.load_custom_events(p)
    assert TITLE in c and os.path.exists(p)
    with open(p, encoding="utf-8") as f:
        assert TITLE in json.load(f)


def test_existing_file_is_authoritative_and_bom_ok(tmp_path):
    p = tmp_path / "custom_events.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"내 이벤트": "메모"}, ensure_ascii=False).encode("utf-8"))
    assert jh_db.load_custom_events(str(p)) == {"내 이벤트": "메모"}


def test_broken_file_is_ignored_not_overwritten(tmp_path):
    p = tmp_path / "custom_events.json"
    p.write_text("{ not json", encoding="utf-8")
    assert TITLE in jh_db.load_custom_events(str(p))
    assert p.read_text(encoding="utf-8") == "{ not json"


def test_merge_matches_and_renders():
    db = [{"rawTitle": "운명", "cleanTitle": "운명", "isArcana": False, "options": []}]
    cache = jh_db.merge_custom_events(db, {TITLE: "안내문 <b>"})
    assert len(cache) == 2 and cache[1]["custom"]
    cands, amb = jh_db.match_title([jh_db.clean_title(TITLE)], cache, "general")
    assert cands[0][0]["rawTitle"] == TITLE
    html = jh_db.format_option_html(cands[0][0]["options"][0])
    assert "안내문 &lt;b&gt;" in html and "📝" in html


def test_merge_db_wins_and_skips_bad_entries():
    db = [{"rawTitle": "운명", "cleanTitle": "운명", "isArcana": False, "options": []}]
    out = jh_db.merge_custom_events(db, {"운명": "x", "빈": "  ", "숫자": 5, "객체": {"note": "n", "arcana": True}})
    assert [e["rawTitle"] for e in out] == ["운명", "객체"] and out[1]["isArcana"] is True
    assert jh_db.merge_custom_events(None, None) == []
