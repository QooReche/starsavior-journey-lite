# -*- coding: utf-8 -*-
"""'훈련의 방향성' 특수 잠재력(AX, CX ...) 안내 + 구원자 추천 (jh_relic)"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jh_db
import jh_relic
import jh_savior

OPT = lambda name: {"choice": name, "reward": "힘 +5", "variants": [], "info": []}


def setup_function(_):
    jh_relic._savior = None


def test_each_choice_shows_its_letters_without_numbers():
    html = jh_db.format_option_html(OPT("생존에 도움이 되는 응원"))
    assert "BX · CX · DX" in html and "AX" not in html
    assert "XX1 · XX2 · XX3" in jh_db.format_option_html(OPT("전술 운용에 도움이 되는 응원"))   # XX 는 번호가 곧 종류


def test_other_events_and_english_are_untouched():
    assert jh_relic.section(OPT("아무 선택지")) == ""
    assert "특수 잠재력" not in jh_db.format_option_html(OPT("공격에 도움이 되는 응원"), lang="en")


def test_detail_levels():
    o = OPT("공격에 도움이 되는 응원")
    assert "턴 시작 시 공격력" in jh_relic.section(o, 1.0, 0)
    short = jh_relic.section(o, 1.0, 1)
    assert "AX · EX · FX" in short and "턴 시작 시" not in short            # 높이 부족 시 설명 생략, 코드 줄은 유지
    assert jh_relic.TIP in jh_relic.section(o, 1.0, 0)


def test_savior_recommendation_only_on_matching_route():
    sv = {"name": "트리쉬", "subtitle": "아셀루스 협회", "potential": "AX1 - 공격력% 효율이 높은 구원자"}
    jh_relic.set_savior([sv])
    assert "⭐ 트리쉬(아셀루스 협회) 추천" in jh_relic.section(OPT("공격에 도움이 되는 응원"))
    assert "추천" not in jh_relic.section(OPT("생존에 도움이 되는 응원"))


def test_savior_is_remembered_and_ambiguous_is_ignored():
    jh_relic.set_savior([{"name": "a", "potential": "BX"}])
    jh_relic.set_savior(None)                                                   # 화면을 벗어나도 유지
    assert "추천" in jh_relic.section(OPT("생존에 도움이 되는 응원"))
    jh_relic.set_savior([{"name": "a", "potential": "BX"}, {"name": "a", "potential": "AX1"}])   # 동명이인이라 모호
    assert "추천" not in jh_relic.section(OPT("생존에 도움이 되는 응원"))


def test_bare_xx_matches_every_xx_route():
    sv = {"name": "x", "potential": "XX2 (생명력 흡수)"}
    assert jh_relic.recommend_lines(jh_relic.ROUTES["전술 운용에 도움이 되는 응원"], sv)
    assert not jh_relic.recommend_lines(jh_relic.ROUTES["공격에 도움이 되는 응원"], sv)


def test_bundled_reco_has_potential_text():
    saviors = jh_savior.load_reco()
    withp = [s for s in saviors if s.get("potential")]
    assert len(withp) >= 40
    assert all("엑셀에 없음" not in s["potential"] for s in withp)


def test_route_choice_names_exist_in_bundled_v2_seed():
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "seed", "v2", "journeys.json")
    ev = json.load(open(base, encoding="utf-8"))["훈련의 방향성"][0]
    names = [c["name"]["ko-KR"] for c in ev["choices"]]
    assert set(names) == set(jh_relic.ROUTES)                                   # DB 가 선택지를 바꾸면 이 테스트가 알려 준다


def test_codes_are_in_a_table_so_wrapped_text_stays_aligned():
    html = jh_relic.section(OPT("공격에 도움이 되는 응원"))
    assert html.count("<table") == 1 and html.count("<tr>") >= 5              # 코드 칸 + 설명 칸, 행 사이 구분선
    import jh_common
    import jh_savior
    assert jh_common.COL_DIVIDER in html and jh_savior.COL_DIVIDER == jh_common.COL_DIVIDER      # 구분선 색은 모든 툴팁이 공용 값 하나


def test_clear_savior_drops_previous_journey_recommendation():
    jh_relic.set_savior([{"name": "A", "potential": "BX"}])
    jh_relic.clear_savior()
    assert "추천" not in jh_relic.section(OPT("생존에 도움이 되는 응원"))


def test_bare_xx_without_number_matches_xx_routes():
    sv = {"name": "x", "potential": "XX - 아무거나"}
    assert jh_relic.recommend_lines(jh_relic.ROUTES["전술 운용에 도움이 되는 응원"], sv)
    assert not jh_relic.recommend_lines(jh_relic.ROUTES["공격에 도움이 되는 응원"], sv)


def test_variable_route_includes_ix():
    codes = [c for c, _ in jh_relic.ROUTES["변수 대처에 도움이 되는 응원"]]
    assert codes == ["GX", "HX", "IX"]                                          # 데이터북 기준 3종 (V2 DB 유물 이벤트에도 IX 가 있음)
    assert "GX · HX · IX" in jh_relic.section(OPT("변수 대처에 도움이 되는 응원"))
