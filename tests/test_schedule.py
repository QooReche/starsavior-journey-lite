"""좌측 상단 날짜 -> 턴 일정 (jh_schedule + 호버 판정 + OCR 픽스처)."""
import glob
import os
import re
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import jh_schedule as js

FX = os.path.join(os.path.dirname(__file__), "fixtures", "schedule")


def test_parse_date_tolerates_spacing_and_noise():
    assert js.parse_date("3월초순") == "3월 초순"
    assert js.parse_date("3월 중순") == "3월 중순"
    assert js.parse_date("97월중순") == "7월 중순"          # 돋보기 아이콘 조각이 숫자로 읽힌 경우
    assert js.parse_date("112월하순") == "12월 하순"
    assert js.parse_date("평가전") is None and js.parse_date("") is None and js.parse_date(None) is None
    assert js.parse_date("13월초순") is None or js.parse_date("13월초순") == "3월 초순"   # 앞 잡음 1 + 3월


def test_every_dated_turn_is_found_once():
    dated = [r for r in js.SCHEDULE if re.match(r"\d+월 [초중하]순", r[1])]
    assert len(dated) == 36 and len({r[1] for r in dated}) == 36
    for r in dated:
        assert js.find_turn(r[1].replace(" ", ""))[0] == r[0]


def test_goal_distance_matches_widget():
    """게임 화면의 '목표까지 N' 과 일치: 3월 초순 6, 8월 초순 3, 9월 하순 4, 7월 중순 5."""
    for date, n in (("3월초순", 6), ("3월중순", 5), ("7월중순", 5), ("8월초순", 3), ("9월하순", 4)):
        assert js._next_turn_to_goal(js.find_turn(date)) == n


def test_upcoming_and_html():
    row = js.find_turn("3월초순")
    ups = js.upcoming(row, 2)
    assert len(ups) == 2 and ups[0][0] == 1 and all(d > 0 for d, _ in ups)
    html = js.format_schedule_html(row)
    text = re.sub(r"<[^>]+>", "", html)
    assert "3월 초순" in text and "평가전까지 6턴" in text and "기초 평가전 준비" in text and "다음 고정 일정" in text
    assert "<script" not in html


def test_html_for_turn_without_events():
    row = js.find_turn("7월하순")            # 알려진 일정 없음
    assert "알려진 고정 일정이 없습니다" in js.format_schedule_html(row)


def _hover(stub_row, pos, enabled=True):
    import journey_helper as jh
    stub = SimpleNamespace(schedule_row=stub_row, settings={"show_schedule": enabled, "tooltip_font_scale": 1.0})
    rect = {"left": 100, "top": 50, "width": 1600, "height": 900}
    return jh.MouseHoverManager._schedule_hover_html(stub, rect, SimpleNamespace(x=lambda: pos[0], y=lambda: pos[1]))


def test_hover_region_and_toggle():
    row = js.find_turn("3월초순")
    assert _hover(row, (100 + 1600 * 0.1, 50 + 900 * 0.08))                  # 날짜 위젯 위
    assert _hover(row, (100 + 1600 * 0.5, 50 + 900 * 0.5)) is None           # 화면 중앙
    assert _hover(row, (100 + 1600 * 0.1, 50 + 900 * 0.08), enabled=False) is None
    assert _hover(None, (100 + 1600 * 0.1, 50 + 900 * 0.08)) is None         # 턴을 모르면 표시 안 함


def _ocr():
    import journey_helper as jh
    try:
        return jh.KoreanOCR(os.path.join(os.path.dirname(__file__), "..", "models"))
    except Exception as e:                                                    # 모델/런타임이 없는 환경
        pytest.skip(f"OCR 사용 불가: {e}")


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(FX, "date_*.png"))))
def test_ocr_reads_real_game_date_crops(path):
    """사용자 게임 스크린샷에서 앱의 schedule_date 비율로 잘라 낸 크롭을 읽어 정답 날짜와 비교."""
    want = re.search(r"_(\d+월(?:초|중|하)순)\.png$", path).group(1)
    g = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)
    ocr = _ocr()
    got = None
    for sc in (2.0, 1.6):
        text, conf = ocr.read_line(cv2.resize(g, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC))
        if conf >= 0.6 and js.parse_date(text):
            got = js.parse_date(text)
            break
    assert got == want.replace("월", "월 ")


# ---- 일정 툴팁: 베타 표시 + 고정 이벤트 주요 보상 (2026-10-18) ----
def _ev(title, rewards):
    import jh_db
    return {"rawTitle": title, "cleanTitle": jh_db.clean_title(title), "isArcana": False,
            "options": [{"choice": "a", "reward": r, "variants": [], "info": []} for r in rewards]}


def test_key_rewards_skips_costs_stats_and_difficulty():
    ev = _ev("테스트", ["이지 오래된 동전 사용 50\n스태미나 +15 or 스태미나 +20\n정체불명의 유물\nor\n노말 보호 70 필요\n잠재력 포인트 +15", "없음"])
    assert js.key_rewards(ev) == ["정체불명의 유물", "잠재력 포인트 +15"]


def test_schedule_tooltip_is_beta_and_shows_key_rewards_for_fixed_events():
    events = [_ev("미궁 탐사", ["정체불명의 유물\n잠재력 포인트 +15"])]
    row = js.find_turn("7월 중순")
    html = js.format_schedule_html(row, 1.0, events)
    assert "[베타]" in html and "주요 보상" in html and "정체불명의 유물" in html
    assert "주요 보상" not in js.format_schedule_html(row)                      # 이벤트 캐시가 없으면 기존 표시 그대로


def test_multiple_contents_get_event_name_prefix_and_random_events_have_no_rewards():
    events = [_ev("토벌의 전리품", ["아이템A"]), _ev("리세트의 유혹", ["아이템B"]), _ev("여가 시간", ["아이템C"])]
    html = js.format_schedule_html(js.find_turn("6월 하순"), 1.0, events)
    assert "토벌의 전리품: " in html and "리세트의 유혹: " in html
    assert "아이템C" not in js.format_schedule_html(js.find_turn("3월 중순"), 1.0, events)   # 랜덤 이벤트는 보상 줄 없음


def test_key_rewards_skips_difficulty_header_and_hidden_stat_items():
    ev = _ev("평가전 승리", ["이지 토벌 평가전\n힘 +5 or 체력 +5\n스탯 인자 힘 상승 Ⅱ - 힘 15\nor\n하드 토벌 평가전\n스탯 인자 힘 상승 Ⅲ - 힘 20"])
    assert js.key_rewards(ev) == []


def test_weather_group_shows_only_kinds_for_this_time_and_short_rewards():
    def w(kind, reward, src):
        e = _ev(f"오늘의 날씨 - {kind}", [reward])
        e["sources"] = [src]
        return e
    events = [w("돌풍", "컨디션 -1\n힘 +15", "5월 하순 7월 중순"), w("쾌청", "잠재력 포인트 +30", "5월 하순 7월 중순"),
              w("순풍", "여정 버프 획득 상쾌함 +3턴", "5월 하순 7월 중순"), w("폭설", "체력 +20", "11월 중순")]
    html = js.format_schedule_html(js.find_turn("7월 중순"), 1.0, events)
    assert "랜덤 3종" in html and "돌풍 힘" in html and "쾌청 잠재력 +30" in html and "순풍 상쾌함 +3턴" in html
    assert "폭설" not in html                                                    # 11월 중순에만 나오는 종류는 제외


def test_next_fixed_schedule_shows_named_key_rewards():
    events = [_ev("습격", ["속도의 감각 20 % 할인\n잠재력 포인트 +20"])]
    html = js.format_schedule_html(js.find_turn("7월 중순"), 1.0, events)
    assert "└ 습격: " in html and "속도의 감각 20 % 할인" in html


def test_evaluation_events_get_no_reward_line():
    events = [_ev("토벌 평가전 승리", ["아이템X"]), _ev("원정 평가전 승리", ["아이템Y"])]
    html = js.format_schedule_html(js.find_turn("6월 하순"), 1.0, events)
    assert "아이템X" not in html and "아이템Y" not in js.format_schedule_html(js.find_turn("7월 중순"), 1.0, events)
