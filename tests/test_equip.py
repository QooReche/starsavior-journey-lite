"""구원자 '장비' 화면: 화면 판별, 구원자 식별(실제 화면 픽스처), 상태기계, 추천 장비 툴팁, 호버/설정."""
import os
import re
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import jh_equip as eq
import jh_savior as sv

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "fixtures", "equip")


def _plain(html):
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ")


@pytest.fixture(scope="module")
def saviors():
    s = sv.load_reco()
    assert s
    return s


def by_key(saviors, key):
    return next(s for s in saviors if s["key"] == key)


# ------------------------------------------------------------------ 데이터
def test_equipment_data_shape(saviors):
    withdata = [s for s in saviors if s.get("equipment")]
    assert len(withdata) >= 50
    for s in withdata:
        e = s["equipment"]
        assert set(e) <= {"necklace", "ring", "sets", "setNotes", "potential"}, s["key"]
        for k in ("necklace", "ring", "sets"):
            for o in e.get(k, []):
                assert o["value"] and "미정" not in o["value"] and "엑셀" not in o["value"], s["key"]


def test_ready_made_lydia_has_equipment_from_site_patch(saviors):
    e = by_key(saviors, "lydia-moon-caster")["equipment"]
    assert e["necklace"][0]["value"] == "속도" and e["ring"][0]["value"] == "공격력%"
    assert e["sets"][0]["value"] == "파괴(4) + 투지(2)" and e["potential"] == "AX"


# ------------------------------------------------------------------ 툴팁 HTML
def test_html_shows_all_rows(saviors):
    txt = _plain(eq.format_html(by_key(saviors, "lydia-moon-caster")))
    for need in ("리디아", "PVE 추천 장비", "목걸이", "속도", "반지", "공격력%", "추천 세트", "파괴(4) + 투지(2)", "잠재력", "AX"):
        assert need in txt, need


def test_defender_shows_pure_and_attack_tank_info(saviors):
    d = next(s for s in saviors if s.get("sets") and s["name"] == "카르멘")
    txt = _plain(eq.format_html(d))
    assert "※ 퓨어탱커, EX" in txt and "※ 공격탱커(AX)" in txt          # 두 운영 방식을 모두 보여 준다
    assert "생명(4) + 장벽(2)" in txt and "공격(4) + 투지(2)" in txt
    assert "AX1은 공격력% 효율이 높거나" in txt


def test_no_equipment_means_no_tooltip(saviors):
    s = dict(by_key(saviors, "lydia-moon-caster"))
    s.pop("equipment")
    assert eq.format_html(s) == "" and not eq.usable([s])


def test_usable_rules(saviors):
    a, b = by_key(saviors, "petra"), by_key(saviors, "gwen")
    assert eq.usable([a]) and not eq.usable([a, b]) and not eq.usable(None) and not eq.usable([])
    assert not eq.usable([{"name": "신규", "unknown": True, "equipment": {"potential": "AX"}}])     # 데이터에 없는 신규 구원자


def test_geometry_scales_with_window(saviors):
    _, w1, cw1 = eq.geometry({"width": 1600, "height": 900}, 1.0)
    _, w2, _ = eq.geometry({"width": 800, "height": 450}, 1.0)
    assert w2 < w1 and cw1 == w1 - 36
    assert eq.TOOLTIP_X_FRAC + eq.TOOLTIP_MAX_W_FRAC < 0.74         # 우측 장비 패널(0.74~)을 가리지 않는 폭


# ------------------------------------------------------------------ 화면 판별 (글자 매칭)
@pytest.mark.parametrize("text,ok", [("구원자", True), ("구원자9", True), ("구원자로", True), ("여정시작", False), ("", False), (None, False)])
def test_match_header(text, ok):
    assert eq.match_header(text) is ok


@pytest.mark.parametrize("text,ok", [("장비", True), ("I장비", True), ("스킬", False), ("", False), ("장비강화세트", False)])
def test_match_title(text, ok):
    assert eq.match_title(text) is ok


@pytest.mark.parametrize("text,kind,ok", [("전체해제", "clear", True), ("자동장착", "auto", True), ("자동착장", "auto", True),
                                          ("'자동장착'", "auto", True), ("확인", "auto", False), ("_", "clear", False)])
def test_match_button(text, kind, ok):
    assert eq.match_button(text, kind) is ok


def test_screen_needs_title_and_button():
    assert eq.screen_hit(True, {"clear"}) and eq.screen_hit(True, {"auto"})
    assert not eq.screen_hit(True, set()) and not eq.screen_hit(False, {"clear", "auto"})


# ------------------------------------------------------------------ 실제 화면 OCR
def _ocr():
    import journey_helper as jh
    try:
        return jh.KoreanOCR(os.path.join(HERE, "..", "models"))
    except Exception as e:
        pytest.skip(f"OCR 사용 불가: {e}")


def _img(name, scale=1.0):
    img = cv2.imdecode(np.fromfile(os.path.join(FX, name), np.uint8), cv2.IMREAD_COLOR)
    return img if scale == 1.0 else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


class _IO:
    def __init__(self, img, ocr):
        import journey_helper as jh
        self.img, self.ocr, self.jh = img, ocr, jh

    def gray(self, name):
        f = self.jh.REGION_FRACTIONS[name]
        h, w = self.img.shape[:2]
        return cv2.cvtColor(self.img[int(h * f["top"]):int(h * (f["top"] + f["height"])), int(w * f["left"]):int(w * (f["left"] + f["width"]))],
                            cv2.COLOR_BGR2GRAY)

    def lines(self, gray, scales):
        return [self.ocr.read_line(cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)) for sc in scales]

    def titles(self, gray):
        return self.jh.read_title_candidates(self.ocr, gray, scales=eq.TITLE_SCALES, min_conf=0.3)


def _header(io):
    return io.lines(io.gray("savior_header"), (2.0,))[0]


def test_regions_registered_in_watcher():
    import journey_helper as jh
    for k in eq.EQUIP_REGIONS:
        assert k in jh.REGION_FRACTIONS


@pytest.mark.parametrize("scale", [1.0, 0.92, 1.08])
@pytest.mark.parametrize("shot,key", [("equip_petra.jpg", "petra"), ("equip_gwen.jpg", "gwen")])
def test_tracker_identifies_savior_on_real_equip_screens(saviors, shot, key, scale):
    """실제 장비 화면 2장(해상도가 조금씩 다름, ±8% 리사이즈 포함): 두 번 연속 읽혀 화면+구원자가 확정된다."""
    io = _IO(_img(shot, scale), _ocr())
    tr = eq.EquipTracker(saviors, io)
    ev = None
    for _ in range(3):
        ev = tr.step(_header(io)) or ev
    assert ev and ev[0] == "savior" and [m["key"] for m in ev[1]] == [key], ev


def test_tracker_ignores_journey_start_screen(saviors):
    io = _IO(_img("journey_start_petra.jpg"), _ocr())
    tr = eq.EquipTracker(saviors, io)
    assert [tr.step(_header(io)) for _ in range(4)] == [None] * 4


# ------------------------------------------------------------------ 상태기계 (가짜 입출력)
class FakeIO:
    """영역마다 다른 값의 이미지를 주고, 그 값으로 읽기 결과를 정하는 가짜. screen: 장비 화면 글자가 보이는가, ident: (부제 후보, 이름 후보)"""
    TEXT = {1: "장비", 2: "전체해제", 3: "자동착장"}
    VALUE = {"equip_title": 1, "equip_btn_clear": 2, "equip_btn_auto": 3, "equip_subtitle": 10, "equip_name": 20}

    def __init__(self):
        self.screen, self.ident = True, (["페트라조합"], ["페트라"])

    def gray(self, name):
        return np.full((4, 4), self.VALUE[name], np.uint8)

    def lines(self, gray, scales):
        v = int(gray[0, 0])
        text = self.TEXT.get(v, "") if self.screen else ""
        return [(text, 0.99 if text else 0.0)] * len(scales)

    def titles(self, gray):
        return self.ident[0] if int(gray[0, 0]) == 10 else self.ident[1]


def _tracker(saviors):
    io = FakeIO()
    return eq.EquipTracker(saviors, io), io


HDR_OK, HDR_NO = ("구원자", 0.99), ("여정시작", 0.99)


def test_tracker_needs_two_reads_and_clears_after_three_misses(saviors):
    tr, io = _tracker(saviors)
    assert tr.step(HDR_OK) is None                                  # 한 번만 읽혀서는 확정하지 않는다
    ev = tr.step(HDR_OK)
    assert ev is None or ev[0] == "savior"
    ev = ev or tr.step(HDR_OK)
    assert ev and [m["key"] for m in ev[1]] == ["petra"]
    assert tr.step(HDR_NO) is None and tr.step(HDR_NO) is None      # 두 번 못 읽는 것은 깜빡임으로 본다
    assert tr.step(HDR_NO) == ("savior", None)                      # 세 번째에 해제
    assert tr.step(HDR_NO) is None                                  # 이미 해제됨: 다시 알리지 않는다


def test_tracker_recheck_reidentifies_after_click(saviors):
    tr, io = _tracker(saviors)
    for _ in range(4):
        tr.step(HDR_OK)
    assert tr.cur is not None
    io.ident = (["람파디스"], ["그웬"])                              # 화살표로 구원자가 바뀜
    tr.recheck()
    ev = tr.step(HDR_OK)
    assert ev and [m["key"] for m in ev[1]] == ["gwen"]


def test_tracker_screen_without_title_never_confirms(saviors):
    tr, io = _tracker(saviors)
    io.screen = False
    assert [tr.step(HDR_OK) for _ in range(5)] == [None] * 5


def test_tracker_throttles_scans(saviors):
    now = [0.0]
    tr = eq.EquipTracker(saviors, FakeIO(), clock=lambda: now[0])
    assert tr.due() and not tr.due()
    now[0] += eq.SCAN_SEC
    assert tr.due()


# ------------------------------------------------------------------ 호버 / 설정
def _hover(matches, enabled=True, in_journey=False):
    import journey_helper as jh
    stub = SimpleNamespace(equip_matches=matches, in_journey=in_journey, settings={"show_savior_equip": enabled, "tooltip_font_scale": 1.0})
    rect = {"left": 100, "top": 50, "width": 1600, "height": 900}
    return jh.MouseHoverManager._equip_hover_html(stub, rect)


def test_hover_toggle_and_conflicts(saviors):
    m = [by_key(saviors, "petra")]
    fn = _hover(m)
    assert callable(fn) and "페트라" in _plain(fn(0)) and fn.width_px > 0
    assert _hover(m, enabled=False) is None                          # 설정 끔
    assert _hover(m, in_journey=True) is None                        # 이벤트 화면에서는 안 뜸
    assert _hover(None) is None
    assert _hover([by_key(saviors, "petra"), by_key(saviors, "gwen")]) is None      # 하나로 못 정하면 안 뜸
    s = dict(m[0])
    s.pop("equipment")
    assert _hover([s]) is None                                       # 데이터 없으면 안 뜸


def test_setting_default_on_and_persisted(tmp_path, monkeypatch):
    import journey_helper as jh
    assert jh.DEFAULT_SETTINGS["show_savior_equip"] is True and jh.DEFAULT_SETTINGS["show_savior_stat"] is True
    monkeypatch.setattr(jh, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "settings.json").write_text('{"show_savior_equip": false}', encoding="utf-8")
    assert jh.load_settings()["show_savior_equip"] is False
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    assert jh.load_settings()["show_savior_equip"] is True


def test_equip_screen_does_not_touch_relic_savior():
    """장비 화면에서 본 구원자가 '훈련의 방향성' 추천(jh_relic)에 쓰이지 않는다 (여정 구원자가 아니다)."""
    import journey_helper as jh
    import jh_relic
    jh_relic.clear_savior()
    stub = SimpleNamespace(equip_matches=None)
    jh.MouseHoverManager.on_equip_changed(stub, [{"name": "페트라", "subtitle": "페트라 조합", "key": "petra"}])
    assert stub.equip_matches and jh_relic._savior is None
