# -*- coding: utf-8 -*-
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_leave_grace_requires_continuous_time():
    c = Clock()
    g = jhm.LeaveGrace(1.5, c)
    assert not g.bad_long_enough()          # 막 안 보이기 시작
    c.t = 1.0
    assert not g.bad_long_enough()
    g.good()                                 # 깜빡임: 다시 보임 -> 타이머 초기화
    c.t = 1.4
    assert not g.bad_long_enough()
    c.t = 3.0
    assert g.bad_long_enough()


def test_title_memo_reuses_within_ttl_only():
    c = Clock()
    m = jhm.TitleMemo(4.0, c)
    img = np.full((20, 40), 100, np.uint8)
    assert m.recall(img) is None
    m.remember(img, ["머리사냥"], "머리사냥")
    assert m.recall(img + 0) == (["머리사냥"], "머리사냥")
    assert m.recall(np.full((20, 40), 200, np.uint8)) is None     # 다른 이미지
    assert m.recall(np.zeros((10, 10), np.uint8)) is None          # 크기 다름
    c.t = 5.0
    assert m.recall(img) is None                                   # 만료


def test_title_memo_ignores_empty():
    m = jhm.TitleMemo(4.0, Clock())
    m.remember(np.zeros((2, 2), np.uint8), [], "")
    assert m.recall(np.zeros((2, 2), np.uint8)) is None
