# -*- coding: utf-8 -*-
"""show_capture_debug: 툴팁이 라벨/제목/카드 OCR 영역을 가리지 않는 배치."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402

RECT = {"left": 1448, "top": 760, "width": 1610, "height": 940}
AVAIL = (0, 0, 3840, 2160)


def overlaps_any(x, y, w, h):
    return any(jhm.boxes_overlap((x, y, x + w, y + h), b) for b in jhm.capture_region_boxes(RECT))


def test_default_placement_overlaps_card_region_without_debug():
    x, y, clip = jhm.place_tooltip(RECT, AVAIL, 300, False)
    assert overlaps_any(x, y, 440, 300)           # 0.30H 는 카드 영역(~0.314H)과 겹침 -> 디버그 모드 대책 필요


def test_debug_places_below_all_regions():
    x, y, clip = jhm.place_tooltip(RECT, AVAIL, 300, True)
    assert not overlaps_any(x, y, 440, 300) and clip is None


def test_debug_tall_tooltip_is_clipped_not_pushed_up():
    x, y, clip = jhm.place_tooltip(RECT, (0, 0, 3840, 1400), 900, True)
    assert clip is not None and y + clip + 10 <= 1400
    assert not overlaps_any(x, y, 440, clip)


def test_non_debug_tall_tooltip_pushed_up_within_screen():
    x, y, clip = jhm.place_tooltip(RECT, (0, 0, 3840, 1400), 900, False)
    assert clip is None and y + 900 + 10 <= 1400


def test_no_game_window_falls_back():
    x, y, clip = jhm.place_tooltip(None, (0, 0, 1920, 1080), 200, True)
    assert (x, y, clip) == (25, 324, None)


# --- 다중 모니터 ---
PRIMARY = (0, 0, 1920, 1040)
SECOND = (1920, -200, 3840, 880)       # 위로 어긋난 보조 모니터


def test_pick_monitor_containing_game_center():
    r = {"left": 2000, "top": 0, "width": 1610, "height": 800}
    assert jhm.pick_screen_geometry(r, [PRIMARY, SECOND]) == SECOND
    r2 = {"left": 100, "top": 100, "width": 1610, "height": 800}
    assert jhm.pick_screen_geometry(r2, [PRIMARY, SECOND]) == PRIMARY
    assert jhm.pick_screen_geometry(None, [PRIMARY, SECOND]) == PRIMARY
    assert jhm.pick_screen_geometry(r, []) is None


def test_pick_monitor_fallback_to_largest_overlap():
    r = {"left": 5000, "top": 0, "width": 100, "height": 100}        # 어느 모니터에도 중심이 없음
    assert jhm.pick_screen_geometry({"left": 1500, "top": 900, "width": 800, "height": 300}, [PRIMARY, SECOND]) in (PRIMARY, SECOND)
    assert jhm.pick_screen_geometry(r, [PRIMARY, SECOND]) in (PRIMARY, SECOND)


def test_tooltip_clamped_to_game_monitor_not_primary():
    r = {"left": 2000, "top": 0, "width": 1610, "height": 800}
    x, y, clip = jhm.place_tooltip(r, SECOND, 700, False)
    assert SECOND[1] <= y and y + 700 + 10 <= SECOND[3]               # 보조 모니터 안
    assert x == 2025
