# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402

L, K2 = jhm.VK_LBUTTON, 0x32


def test_number_keys_ignored_when_game_not_foreground():
    assert jhm.filter_game_inputs({K2}, False, True) == set()          # 디스코드에서 '2' 입력 + 커서는 게임 위
    assert jhm.filter_game_inputs({K2}, True, False) == {K2}


def test_click_needs_cursor_over_game_or_foreground():
    assert jhm.filter_game_inputs({L}, False, False) == set()
    assert jhm.filter_game_inputs({L}, False, True) == {L}
    assert jhm.filter_game_inputs({L}, True, False) == {L}


def test_mixed_inputs():
    assert jhm.filter_game_inputs({L, K2}, False, True) == {L}
    assert jhm.filter_game_inputs(set(), True, True) == set()
