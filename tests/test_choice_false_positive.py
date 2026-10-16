# -*- coding: utf-8 -*-
"""선택 오감지(대사 넘기기 클릭) 방지: 체류 시간, 버튼 사라짐 확인, 미확정 잠금 (GUI 없이)."""
import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import journey_helper as jhm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def panel_with_buttons():
    g = np.full((36, 64), 40, np.uint8)
    g[5:12, 4:60] = 220          # 버튼 띠들
    g[20:27, 4:60] = 220
    g[8, 10:50] = 40
    g[23, 10:50] = 40
    return g


def panel_plain():
    return np.full((36, 64), 120, np.uint8)


def test_panel_energy_buttons_higher():
    assert jhm.panel_energy(panel_with_buttons()) > jhm.panel_energy(panel_plain()) + 2
    assert jhm.panel_energy(None) == 0.0


def test_hover_dwell_requires_time_on_same_zone():
    c = Clock()
    d = jhm.HoverDwell(clock=c)
    d.update(1)
    assert not d.dwelled()
    c.t += jhm.HOVER_DWELL_SEC + 0.01
    assert d.dwelled()
    d.update(2)                      # 다른 존으로 옮기면 다시 센다
    assert not d.dwelled()
    d.update(None)
    c.t += 5
    assert not d.dwelled()


def test_buttons_disappearing_confirms():
    c = Clock()
    w = jhm.SelectionWatch(panel_with_buttons(), clock=c)
    assert w.feed(panel_plain()) is None          # 변화 1회
    assert w.feed(panel_plain()) == "confirm"     # 2회 연속 + 에너지 감소


def test_buttons_appearing_after_click_is_not_a_selection():
    """대사 넘김 클릭 -> 선택지 버튼이 나타남: 확정하지 않고 해제."""
    c = Clock()
    w = jhm.SelectionWatch(panel_plain(), clock=c)
    assert w.feed(panel_with_buttons()) is None
    assert w.feed(panel_with_buttons()) == "release"


def test_no_change_releases_after_timeout():
    c = Clock()
    w = jhm.SelectionWatch(panel_with_buttons(), clock=c)
    assert w.feed(panel_with_buttons()) is None
    c.t += jhm.NO_REACTION_RELEASE_SEC + 0.1
    assert w.feed(panel_with_buttons()) == "release"


def test_single_flicker_does_not_confirm():
    w = jhm.SelectionWatch(panel_with_buttons(), clock=Clock())
    assert w.feed(panel_plain()) is None
    assert w.feed(panel_with_buttons()) is None   # 되돌아옴 -> 연속 카운트 리셋
    assert w.feed(panel_plain()) is None


# --- MouseHoverManager 메서드를 가짜 self 로 ---------------------------------
class FakeTooltip:
    def __init__(self):
        self.cleared = 0

    def hide_and_clear(self):
        self.cleared += 1


class FakeStatus:
    def set_status(self, *a, **k):
        self.last = a


class FakeTimer:
    def __init__(self):
        self.started = False

    def start(self, ms):
        self.started = True

    def stop(self):
        self.started = False


def make_mgr(has_condition, panels):
    M = jhm.MouseHoverManager
    m = types.SimpleNamespace()
    m.choice_lock = jhm.ChoiceLock(clock=Clock())
    m.zones = [{"idx": 0, "option": {"choice": "a", "has_condition": has_condition}, "y_min": 0, "y_max": 1}]
    m.current_title, m.matched, m.in_journey = "머리사냥", {"rawTitle": "머리사냥"}, True
    m.options, m.ambig, m.not_found, m.card_id = [{}], [], "", None
    m._click_block_until, m._watch, m._pending_hide = 0.0, None, False
    m.tooltip, m.status, m._transition_timer = FakeTooltip(), FakeStatus(), FakeTimer()
    m._capture_choice_panel = lambda: panels.pop(0) if panels else None
    m._idle_text = lambda: "idle"
    m._journey_text = lambda: "journey"
    m._stop_transition_check = lambda: M._stop_transition_check(m)
    m.reset_event_state = lambda: (setattr(m, "in_journey", False), setattr(m, "current_title", ""),
                                   setattr(m, "matched", None), setattr(m, "zones", []), m._stop_transition_check())
    return m


def test_unconditional_choice_click_hides_but_does_not_confirm_or_reset():
    M = jhm.MouseHoverManager
    m = make_mgr(False, [panel_with_buttons()])
    M.on_choice_selected(m, 0)
    assert m.choice_lock.active and not m.choice_lock.confirmed        # 8초짜리 미확정 잠금
    assert m.in_journey and m.matched is not None                      # 이벤트 상태는 그대로
    assert m.tooltip.cleared == 1 and m._pending_hide
    assert m._watch is not None and m._transition_timer.started


def test_false_positive_dialogue_click_recovers_quickly():
    """대사 넘김 클릭 -> 선택지가 나타남: 잠금 해제, 이벤트 상태 유지, 숨김 해제."""
    M = jhm.MouseHoverManager
    m = make_mgr(False, [panel_plain(), panel_with_buttons(), panel_with_buttons()])
    M.on_choice_selected(m, 0)
    M._poll_transition(m)
    M._poll_transition(m)
    assert not m.choice_lock.active and not m._pending_hide
    assert m.in_journey and m.matched is not None


def test_real_selection_confirms_after_buttons_vanish():
    M = jhm.MouseHoverManager
    m = make_mgr(False, [panel_with_buttons(), panel_plain(), panel_plain()])
    M.on_choice_selected(m, 0)
    M._poll_transition(m)
    M._poll_transition(m)
    assert m.choice_lock.active and m.choice_lock.confirmed
    assert m.matched is None


def test_conditional_choice_not_hidden_immediately():
    M = jhm.MouseHoverManager
    m = make_mgr(True, [panel_with_buttons()])
    M.on_choice_selected(m, 0)
    assert not m._pending_hide and m.tooltip.cleared == 0
