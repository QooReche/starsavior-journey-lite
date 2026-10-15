"""jh_cards 오프라인 테스트 (네트워크 불필요: tests/fixtures/cards 의 참조 PNG 사용)."""
import os

import cv2
import numpy as np
import pytest

import jh_cards as jc

FX = os.path.join(os.path.dirname(__file__), "fixtures", "cards")
BUNNY, MAID, GONGNYEO, BUGUL, BIRD = 7104401, 6100601, 7101301, 7105701, 7102501


@pytest.fixture(scope="module")
def store():
    return jc.CardStore(FX, download=False)


def region(name):
    img = cv2.imdecode(np.fromfile(os.path.join(FX, f"region_{name}.png"), np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


def test_bunny_birthday_party(store):
    aid, score, margin = jc.identify(region("bunny"), [BUNNY, MAID], store)
    assert aid == BUNNY and score > 0.7 and margin > 0.3


def test_bird(store):
    aid, score, margin = jc.identify(region("bird"), [BIRD], store)
    assert aid == BIRD and score > 0.7


def test_head_hunt_picks_bugul_over_gongnyeo(store):
    aid, score, margin = jc.identify(region("head"), [GONGNYEO, BUGUL], store)
    assert aid == BUGUL and margin > 0.25
    aid2, _, _ = jc.identify(region("head"), [BUGUL, GONGNYEO], store)
    assert aid2 == BUGUL


def test_wrong_candidate_set_is_none(store):
    assert jc.identify(region("head"), [GONGNYEO, MAID], store)[0] is None
    assert jc.identify(region("bunny"), [GONGNYEO, BUGUL], store)[0] is None
    assert jc.identify(region("bird"), [BUGUL], store)[0] is None


def test_scale_robustness(store):
    for f in (0.8, 1.25):
        r = cv2.resize(region("head"), None, fx=f, fy=f, interpolation=cv2.INTER_LINEAR)
        assert jc.identify(r, [GONGNYEO, BUGUL], store)[0] == BUGUL


def test_degenerate_inputs(store):
    assert jc.identify(None, [BUGUL], store) == (None, 0.0, 0.0)
    assert jc.identify(region("head"), [], store) == (None, 0.0, 0.0)
    assert jc.identify(region("head"), [999], store)[0] is None  # 캐시에 없음


def test_crop_card_region_fractions():
    frame = np.zeros((939, 1616, 3), np.uint8)
    r = jc.crop_card_region(frame)
    assert abs(r.shape[1] - 130) <= 2 and abs(r.shape[0] - 160) <= 2

