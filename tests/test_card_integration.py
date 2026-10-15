# -*- coding: utf-8 -*-
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_cards as jc  # noqa: E402
import jh_db  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures", "cards")
GONGNYEO, BUGUL = 7101301, 7105701


def _arcana(aid, name, stat):
    return {"arcanaId": aid, "arcanaName": name, "eventGroups": [
        {"title": "머리사냥", "choices": [{"name": "싸운다", "successRewards": [[{"label": "힘", "value": stat}]]}]}]}


def _event():
    j = {"arcana": [_arcana(GONGNYEO, "공녀", "+10"), _arcana(BUGUL, "불굴의 역작", "+20")], "events": []}
    return jh_db.build_event_cache(j, None)[0]


def _region(name):
    return cv2.imdecode(np.fromfile(os.path.join(FX, f"region_{name}.png"), np.uint8), cv2.IMREAD_COLOR)


def test_cache_records_arcana_ids():
    ev = _event()
    assert ev["arcanaIds"] == {"공녀": GONGNYEO, "불굴의 역작": BUGUL}
    assert jh_db.arcana_ids_of(ev) == [GONGNYEO, BUGUL]
    assert len(ev["options"][0]["variants"]) == 2


def test_filter_event_by_arcana_keeps_only_that_arcana():
    ev = _event()
    f = jh_db.filter_event_by_arcana(ev, BUGUL)
    v = f["options"][0]["variants"]
    assert len(v) == 1 and v[0]["sources"] == ["불굴의 역작"] and "+20" in v[0]["reward"]
    html = jh_db.format_option_html(f["options"][0])
    assert "+20" in html and "+10" not in html and "여러 아르카나" not in html
    assert len(ev["options"][0]["variants"]) == 2        # 원본 불변


def test_filter_unknown_id_falls_back_to_all():
    ev = _event()
    assert jh_db.filter_event_by_arcana(ev, 999) is ev


def test_identify_stable_picks_bugul():
    store = jc.CardStore(FX, download=False)
    aid, score, margin, tries = jc.identify_stable(lambda: _region("head"), [GONGNYEO, BUGUL], store, sleep=lambda s: None)
    assert aid == BUGUL and tries == 2


def test_identify_stable_unstable_or_failed_gives_none():
    store = jc.CardStore(FX, download=False)
    seq = iter([_region("head"), _region("bunny")] * 10)        # 번갈아 다른 결과 -> 안정되지 않음
    r = jc.identify_stable(lambda: next(seq), [GONGNYEO, BUGUL], store, max_tries=6, sleep=lambda s: None)
    assert r[0] is None
    assert jc.identify_stable(lambda: None, [GONGNYEO, BUGUL], store, max_tries=3, sleep=lambda s: None)[0] is None
    def boom():
        raise RuntimeError("x")
    assert jc.identify_stable(boom, [GONGNYEO, BUGUL], store, max_tries=2, sleep=lambda s: None)[0] is None
