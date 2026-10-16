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
