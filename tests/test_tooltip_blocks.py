# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402


def opt(reward):
    return {"choice": "선택", "reward": reward, "info": [], "variants": [{"sources": [], "reward": reward, "info": []}]}


RANDOM = opt("힘 +1\nor\n힘 +2\nor\n힘 +3")
DIFF = opt("이지 힘 +1\nor\n노말 힘 +2\nor\n하드 힘 +3")
SUCCFAIL = opt("[SUCCESS] 힘 +5\n[FAIL] 힘 -5")


def test_or_divider_for_random_blocks():
    h = jh_db.format_option_html(RANDOM)
    assert h.count("── 또는 ──") == 2
    assert "── OR ──" in jh_db.format_option_html(RANDOM, lang="en")


def test_no_or_divider_for_difficulty_or_success_fail():
    assert "또는" not in jh_db.format_option_html(DIFF)
    assert "또는" not in jh_db.format_option_html(SUCCFAIL)


def test_difficulty_filter():
    assert jh_db.format_option_html(DIFF).count("힘 +") == 3
    for d, keep in (("easy", "+1"), ("normal", "+2"), ("hard", "+3")):
        h = jh_db.format_option_html(DIFF, difficulty=d)
        assert keep in h and h.count("힘 +") == 1, d
    assert jh_db.format_option_html(DIFF, difficulty="all").count("힘 +") == 3
    assert jh_db.format_option_html(DIFF, difficulty="bogus").count("힘 +") == 3


def test_hidden_difficulty_list():
    h = jh_db.format_option_html(DIFF, difficulty=["easy"])
    assert h.count("힘 +") == 2
    assert jh_db.format_option_html(DIFF, difficulty=[]).count("힘 +") == 3
    assert jh_db.format_option_html(DIFF, difficulty=["easy", "normal", "hard"]).count("힘 +") == 3   # 전부 숨기면 원본


def test_difficulty_leaves_unlabelled_events_alone():
    assert jh_db.format_option_html(RANDOM, difficulty="hard").count("힘 +") == 3


def test_difficulty_two_levels_only_falls_back_when_empty():
    o = opt("이지 힘 +1\nor\n노말 힘 +2")
    h = jh_db.format_option_html(o, difficulty="hard")      # 해당 난이도가 없으면 전부 표시
    assert h.count("힘 +") == 2


def test_difficulty_applies_in_ambiguous_html():
    h = jh_db.format_ambiguous_html([("A", DIFF), ("B", DIFF)], difficulty="easy")
    assert h.count("힘 +") == 2 and "+2" not in h


def test_detail_levels_shrink_output():
    many = opt("\nor\n".join(f"힘 +{i}" for i in range(1, 8)))
    full, d2 = jh_db.format_option_html(many), jh_db.format_option_html(many, detail=2)
    assert full.count("힘 +") == 7 and d2.count("힘 +") == 6
    assert "…외 1건" in d2 and len(d2) < len(full)
    en = jh_db.format_option_html(many, lang="en", detail=2)
    assert "…+1 more" in en


def test_detail_drops_info_and_item_sections():
    o = dict(opt("힘 +1"), info=[{"name": "N", "desc": "DESC", "kind": "k"}],
             item_use=[{"item": "열쇠", "uses": [{"event": "E", "timing": "", "choice": "", "result": ""}]}])
    assert "DESC" in jh_db.format_option_html(o) and "열쇠" in jh_db.format_option_html(o)
    d1 = jh_db.format_option_html(o, detail=1)
    assert "DESC" not in d1 and "열쇠" not in d1


def test_detail_limits_variants():
    o = opt("힘 +1")
    o["variants"] = [{"sources": [f"A{i}"], "reward": f"힘 +{i}", "info": []} for i in range(4)]
    h = jh_db.format_option_html(o, detail=2)
    assert "A0" in h and "A1" in h and "A2" not in h and "…외 2건" in h


def test_or_groups_with_success_fail_pairs():
    """'or' 로 갈리는 랜덤 결과 묶음 사이엔 '또는', 한 묶음의 성공/실패 사이엔 일반 구분선. 압축 모드는 6블록까지 보여 준다."""
    reward = "\n".join(["[SUCCESS] 컨디션 +1", "[SUCCESS] 스태미나 +10", "[FAIL] 컨디션 -1", "[FAIL] 스태미나 +10", "or",
                        "[SUCCESS] 집중 +20", "[SUCCESS] 보호 +10", "[FAIL] 집중 +15", "or",
                        "[SUCCESS] 보호 +20", "[FAIL] 보호 +15"])
    blocks = jh_db._split_blocks(reward)
    assert [b["grp"] for b in blocks] == [0, 0, 1, 1, 2, 2]
    o = {"choice": "c", "reward": reward, "info": []}
    full = jh_db.format_option_html(o)
    assert full.count("또는") == 2
    d2 = jh_db.format_option_html(o, detail=2)
    assert "외" not in d2 and "컨디션 +1 · 스태미나 +10" in d2
