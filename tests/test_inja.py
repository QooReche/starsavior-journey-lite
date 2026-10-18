"""인자 고르기 화면: 여정 스탯 우선순위 툴팁 (화면 판별, 툴팁 HTML, 호버/설정). 추천 아르카나 툴팁에는 스탯 우선순위가 없다."""
import glob
import os
import re
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import jh_savior as sv

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "fixtures", "inja")
SAVIOR_FX = os.path.join(HERE, "fixtures", "savior")


def _plain(html):
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ").replace("&gt;", ">")


@pytest.fixture(scope="module")
def saviors():
    s = sv.load_reco()
    assert s
    return s


def by_key(saviors, key):
    return next(s for s in saviors if s["key"] == key)


# ------------------------------------------------------------------ 툴팁 HTML
def test_stat_priority_is_shown_with_groups(saviors):
    txt = _plain(sv.format_stat_html(by_key(saviors, "asherah-voyager")))
    assert "여정 스탯 우선순위" in txt and "아세라" in txt
    assert "힘 > 체력 = 인내 > 집중 = 보호" in txt                         # 같은 묶음은 '=', 묶음 사이는 '>'


def test_defender_shows_both_stat_priorities_side_by_side(saviors):
    d = next(s for s in saviors if s.get("sets"))
    html = sv.format_stat_html(d)
    txt = _plain(html)
    assert "▌퓨어탱커" in txt and "▌공격력 탱커" in txt
    assert "체력 = 인내 > 힘 > 집중 = 보호" in txt and "힘 > 체력 > 인내 > 집중 = 보호" in txt
    assert "이터널 프로미스 카르멘 없을시 사용불가" in txt
    assert html.index("▌퓨어탱커") < html.index("▌공격력 탱커")                                 # 왼쪽 퓨어탱커, 오른쪽 공격력 탱커
    head_row = html[html.index("<tr><td></td>"):html.index("</tr>")]
    assert head_row.count("<td valign=\"middle\"") == 2                     # 머리글이 좌우 두 칸
    assert sv.stat_two_columns(d) and not sv.stat_two_columns(by_key(saviors, "petra"))


def test_stat_geometry_two_columns_is_wider(saviors):
    rect = {"width": 1600, "height": 900}
    _, w1 = sv.stat_geometry(rect, 1.0, False)
    _, w2 = sv.stat_geometry(rect, 1.0, True)
    assert w2 > w1
    assert sv.STAT_TOOLTIP_X_FRAC + sv.STAT_WIDE_W_FRAC < 0.7              # 인자 슬롯/우측 영역을 가리지 않음


def test_waltz_asherah_stat_note(saviors):
    assert "힘 1,000 / 체력 1,000" in _plain(sv.format_stat_html(by_key(saviors, "waltz-asherah")))


def test_no_data_means_no_tooltip(saviors):
    s = dict(by_key(saviors, "asherah-voyager"))
    s.pop("statPriority")
    assert sv.format_stat_html(s) == "" and not sv.stat_usable([s])


def test_usable_rules(saviors):
    a, b = by_key(saviors, "petra"), by_key(saviors, "gwen")
    assert sv.stat_usable([a]) and not sv.stat_usable([a, b]) and not sv.stat_usable(None) and not sv.stat_usable([])
    assert not sv.stat_usable([{"name": "신규", "unknown": True, "statPriority": [{"label": "", "note": "", "groups": [["힘"]]}]}])


def test_every_savior_has_stat_priority(saviors):
    assert all(s.get("statPriority") for s in saviors)


def test_recommended_arcana_tooltip_has_no_stat_priority(saviors):
    """추천 아르카나 툴팁은 이미 정보가 많아 스탯 우선순위를 넣지 않는다."""
    for key in ("asherah-voyager", "petra"):
        assert "여정 스탯 우선순위" not in sv.format_html([by_key(saviors, key)], 1.0, None, 0, 404)
    d = next(s for s in saviors if s.get("sets"))
    assert "여정 스탯 우선순위" not in sv.format_html([d], 1.0, None, 0, 600)


# ------------------------------------------------------------------ 화면 판별
@pytest.mark.parametrize("text,kind,ok", [("여정시작정보/", "info", True), ("여정시작정보y", "info", True), ("여정시작", "info", False),
                                          ("자동장착", "auto", True), ("[자동장학", "auto", True), ("자동장작", "auto", True),
                                          ("확인", "ok", True), ("학인", "ok", False), ("", "ok", False), (None, "info", False)])
def test_match_inja_screen(text, kind, ok):
    assert sv.match_inja_screen(text, kind) is ok


def test_inja_screen_needs_info_and_a_button():
    assert sv.inja_screen_hit({"info", "ok"}) and sv.inja_screen_hit({"info", "auto"}) and sv.inja_screen_hit({"info", "auto", "ok"})
    assert not sv.inja_screen_hit({"info"}) and not sv.inja_screen_hit({"auto", "ok"}) and not sv.inja_screen_hit(set())


def _ocr():
    import journey_helper as jh
    try:
        return jh.KoreanOCR(os.path.join(HERE, "..", "models"))
    except Exception as e:
        pytest.skip(f"OCR 사용 불가: {e}")


def _crop(img, frac):
    h, w = img.shape[:2]
    return cv2.cvtColor(img[int(h * frac["top"]):int(h * (frac["top"] + frac["height"])), int(w * frac["left"]):int(w * (frac["left"] + frac["width"]))],
                        cv2.COLOR_BGR2GRAY)


def _found(ocr, img):
    import journey_helper as jh
    found = set()
    for name, kind in sv.INJA_KINDS.items():
        g = _crop(img, jh.REGION_FRACTIONS[name])
        if any(c >= 0.4 and sv.match_inja_screen(t, kind) for t, c in
               [ocr.read_line(cv2.resize(g, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)) for sc in (2.0, 3.0)]):
            found.add(kind)
    return found


@pytest.mark.parametrize("scale", [1.0, 0.92, 1.08])
@pytest.mark.parametrize("shot", ["inja_petra.jpg", "inja_petra_filled.jpg", "inja_gwen.jpg"])
def test_ocr_detects_inja_screen(shot, scale):
    """실제 인자 고르기 화면 3장(인자를 채운 상태 포함, ±8% 리사이즈 포함)에서 확정 조건이 충족된다."""
    img = cv2.imdecode(np.fromfile(os.path.join(FX, shot), np.uint8), cv2.IMREAD_COLOR)
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    found = _found(_ocr(), img)
    assert sv.inja_screen_hit(found), found


def test_ocr_arcana_and_picker_screens_are_not_inja_screens():
    """아르카나 편성/카드 고르기 화면에서는 확정 조건이 충족되지 않는다."""
    ocr = _ocr()
    for p in sorted(glob.glob(os.path.join(SAVIOR_FX, "arcana_*.png"))) + [os.path.join(SAVIOR_FX, "picker_12.png")]:
        img = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)
        assert not sv.inja_screen_hit(_found(ocr, img)), os.path.basename(p)


def test_regions_registered_in_watcher():
    import journey_helper as jh
    for k in sv.INJA_REGIONS:
        assert k in jh.REGION_FRACTIONS


# ------------------------------------------------------------------ 호버 / 설정
def _hover(matches, inja=True, enabled=True, in_journey=False, arcana=False):
    import journey_helper as jh
    stub = SimpleNamespace(savior_matches=matches, inja_screen=inja, arcana_screen=arcana, in_journey=in_journey,
                           settings={"show_savior_stat": enabled, "tooltip_font_scale": 1.0})
    return jh.MouseHoverManager._stat_hover_html(stub, {"left": 100, "top": 50, "width": 1600, "height": 900})


def test_hover_only_on_inja_screen_and_toggle(saviors):
    m = [by_key(saviors, "gwen")]
    fn = _hover(m)
    assert callable(fn) and "그웬" in _plain(fn(0)) and fn.width_px > 0
    assert _hover(m, inja=False) is None                              # 인자 고르기 화면이 아님
    assert _hover(m, arcana=True) is None                             # 아르카나 편성 화면에서는 안 뜸
    assert _hover(m, enabled=False) is None                           # 설정 끔
    assert _hover(m, in_journey=True) is None                         # 이벤트 화면에서는 안 뜸
    assert _hover(None) is None
    assert _hover([by_key(saviors, "gwen"), by_key(saviors, "petra")]) is None      # 하나로 못 정하면 안 뜸
    s = dict(m[0])
    s.pop("statPriority")
    assert _hover([s]) is None                                        # 데이터 없으면 안 뜸


def test_inja_tooltip_position_clears_stats_and_slots():
    assert 0.2 <= sv.STAT_TOOLTIP_X_FRAC and sv.STAT_TOOLTIP_TOP_FRAC < 0.45           # 좌측 스탯 패널(y 0.43~) 오른쪽, 인자 슬롯(y 0.5~) 위


def test_setting_default_on():
    import journey_helper as jh
    assert jh.DEFAULT_SETTINGS["show_savior_stat"] is True


def test_two_column_stat_tooltip_is_wide_enough_for_each_column(saviors):
    """열 하나(최장: '힘 > 체력 > 인내 > 집중 = 보호', 15px 굵은 글씨)가 잘리지 않을 폭: 안쪽 폭의 절반이 글자 폭 추정 이상."""
    d = next(s for s in saviors if s.get("sets"))
    rect = {"width": 1600, "height": 935}
    eff, w = sv.stat_geometry(rect, 1.0, True)
    longest = "힘 > 체력 > 인내 > 집중 = 보호"
    need = sv._tw(longest, round(15 * eff)) * 0.8                      # 굵은 글씨 폭 추정(보수적으로 80%)
    assert (w - 36) / 2 >= need, (w, need)


# ------------------------------------------------------------------ 화면을 바꿀 때 바로 사라지는지
def _stub_for_click(**kw):
    import journey_helper as jh
    calls = []
    stub = SimpleNamespace(inja_screen=True, savior_matches=None, _ij_click_prev=False, calls=calls,
                           watcher=SimpleNamespace(request_inja_recheck=lambda: calls.append("recheck")), **kw)
    stub._left_click_edge = lambda attr: True
    return jh, stub


def test_click_hides_stat_tooltip_and_requests_recheck(saviors):
    jh, stub = _stub_for_click()
    stub.savior_matches = [by_key(saviors, "gwen")]
    assert jh.MouseHoverManager._stat_click(stub) is True
    assert stub.inja_screen is False and stub.calls == ["recheck"]


def test_click_ignored_when_tooltip_not_showing(saviors):
    jh, stub = _stub_for_click()
    stub.inja_screen = False
    stub.savior_matches = [by_key(saviors, "gwen")]
    assert jh.MouseHoverManager._stat_click(stub) is False and stub.calls == []
    jh, stub = _stub_for_click()
    stub.savior_matches = None
    assert jh.MouseHoverManager._stat_click(stub) is False


def _watcher_stub(jh, ar_on=False, ar_block=False, ij_on=True):
    emitted = []
    w = SimpleNamespace(_ar_on=ar_on, _ar_block=ar_block, _ij_on=ij_on, _ij_streak=1, _ij_miss=0, emitted=emitted,
                        inja_screen_changed=SimpleNamespace(emit=lambda v: emitted.append(v)),
                        _read_region=lambda *a, **k: [("", 0.0)])
    return w


def test_arcana_screen_text_turns_off_inja_tooltip_immediately():
    import journey_helper as jh
    for kw in ({"ar_on": True}, {"ar_block": True}):
        w = _watcher_stub(jh, **kw)
        jh.ScreenWatcher._scan_inja_screen(w, None, {})
        assert w.emitted == [False] and w._ij_on is False             # 기다리지 않고 한 번에 해제


def test_inja_lost_is_short_and_recheck_needs_one_read():
    import journey_helper as jh
    assert jh.INJA_LOST <= 2 and jh.SAVIOR_SCAN_SEC <= 0.8
    w = SimpleNamespace(_ij_on=True, _ij_streak=0, _ij_miss=5, _sv_next=99.0)
    jh.ScreenWatcher.request_inja_recheck(w)
    assert w._ij_on is False and w._ij_streak == jh.INJA_CONFIRM - 1 and w._sv_next == 0.0   # 다음 루프에서 글자가 한 번만 읽혀도 다시 확정


# ------------------------------------------------------------------ 속도: 확실히 읽히면 한 번에, 불필요한 영역은 읽지 않는다
def _scan_stub(confs, ij_streak=0):
    """_region_conf 가 영역 이름별로 정해진 신뢰도를 돌려주는 가짜 워처. calls 에 읽은 영역을 기록한다."""
    calls, emitted = [], []

    def region_conf(sct, rect, name, match, min_conf=0.4, scales=(2.0, 3.0)):
        calls.append(name)
        return confs.get(name, 0.0)

    w = SimpleNamespace(_ar_on=False, _ar_block=False, _ij_on=False, _ij_streak=ij_streak, _ij_miss=0, _region_conf=region_conf,
                        inja_screen_changed=SimpleNamespace(emit=lambda v: emitted.append(v)))
    return w, calls, emitted


def test_strong_read_confirms_inja_screen_in_one_scan():
    import journey_helper as jh
    w, calls, emitted = _scan_stub({"inja_info": 0.95, "inja_ok": 0.92, "inja_auto": 0.0})
    jh.ScreenWatcher._scan_inja_screen(w, None, {})
    assert emitted == [True] and w._ij_on is True


def test_weak_read_needs_two_scans_for_inja_screen():
    import journey_helper as jh
    w, calls, emitted = _scan_stub({"inja_info": 0.6, "inja_ok": 0.6})
    jh.ScreenWatcher._scan_inja_screen(w, None, {})
    assert emitted == [] and w._ij_on is False
    jh.ScreenWatcher._scan_inja_screen(w, None, {})
    assert emitted == [True]


def test_no_info_button_means_no_other_regions_are_read():
    import journey_helper as jh
    w, calls, emitted = _scan_stub({})
    jh.ScreenWatcher._scan_inja_screen(w, None, {})
    assert calls == ["inja_info"] and emitted == []                    # 첫 글자가 없으면 나머지는 읽지 않는다 (대부분의 화면)


def test_identify_fast_reads_fast_scales_first_and_falls_back(saviors):
    seen = []

    def read(gray, scales):
        seen.append(scales)
        return {"sub": ["페트라조합"], "name": ["페트라"]}[gray]

    res, subs, names = sv.identify_fast(saviors, read, "sub", "name", (2.0,), (2.0, 1.6, 1.0))
    assert res[1] in ("name", "subtitle") and [m["key"] for m in res[0]] == ["petra"]
    assert seen == [(2.0,), (2.0,)]                                    # 하나로 정해지면 전체 배율은 읽지 않는다
    seen.clear()
    sv.identify_fast(saviors, lambda g, sc: (seen.append(sc), [])[1], "sub", "name", (2.0,), (2.0, 1.6, 1.0))
    assert (2.0, 1.6, 1.0) in seen                                     # 못 정하면 전체 배율로 다시 읽는다


def test_scan_interval_is_short():
    import journey_helper as jh
    assert jh.SAVIOR_SCAN_SEC <= 0.5 and jh.INJA_STRONG >= 0.8
