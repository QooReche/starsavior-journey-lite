# -*- coding: utf-8 -*-
"""선택지 선택 감지 / 완료 이벤트 잠금 / 화면 전환 판정 (원작 1.0.6~1.1.3 규칙) - GUI 없이 순수 로직만."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_click_inside_zone_selects_hovered_option():
    assert jhm.detect_choice_input({0x01}, set(), 1, 3) == 1


def test_click_outside_zone_ignored():
    assert jhm.detect_choice_input({0x01}, set(), None, 3) is None


def test_held_button_does_not_repeat():
    assert jhm.detect_choice_input({0x01}, {0x01}, 1, 3) is None


def test_number_and_numpad_keys():
    assert jhm.detect_choice_input({0x32}, set(), None, 3) == 1        # '2'
    assert jhm.detect_choice_input({0x63}, set(), None, 3) == 2        # numpad 3
    assert jhm.detect_choice_input({0x34}, set(), None, 3) is None     # 4번은 선택지가 3개라 무시
    assert jhm.detect_choice_input({0x35}, set(), None, 4) is None     # 5 는 대상 아님


def test_lock_blocks_same_title_with_ocr_noise():
    lk = jhm.ChoiceLock(clock=Clock())
    lk.lock("오늘의 날씨 순풍")
    assert lk.blocks(["오늘의날씨소풍"])
    assert lk.blocks("오늘의 날씨 순풍")
    assert not lk.blocks(["리세트의 일과"])
    lk.release()
    assert not lk.active and not lk.blocks(["오늘의 날씨 순풍"])


def test_lock_expiry_unconfirmed_8s_confirmed_60s():
    c = Clock()
    lk = jhm.ChoiceLock(clock=c)
    lk.lock("머리사냥")
    c.t += 7.9
    assert not lk.expired()
    c.t += 0.2
    assert lk.expired()
    lk.lock("머리사냥")
    lk.confirm()
    c.t += 30
    assert not lk.expired()
    c.t += 31
    assert lk.expired()


def test_confirm_without_lock_is_noop():
    lk = jhm.ChoiceLock(clock=Clock())
    lk.confirm()
    assert not lk.active and not lk.expired()


def test_panel_diff():
    a = np.full((36, 64), 100, np.uint8)
    b = np.full((36, 64), 130, np.uint8)
    assert jhm.panel_diff(a, a) == 0.0
    assert jhm.panel_diff(a, b) == 30.0 > jhm.TRANSITION_DIFF
    assert jhm.panel_diff(a, None) is None
    assert jhm.panel_diff(a, np.zeros((10, 10), np.uint8)) is None
