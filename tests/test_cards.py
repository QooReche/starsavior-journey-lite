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


def test_soup_card_in_shifted_ui_position(store):
    """실기 2026-10-18(1592x891 창): 카드가 기준 위치보다 약 8px 왼쪽·24px 위 -> 예전 +-2px 탐색은 0.28 대 0.24 로 오답 후보가 앞섰다."""
    aid, score, margin = jc.identify(region("soup"), [7101101, 7105501], store)
    assert aid == 7105501 and score > 0.6 and margin > 0.3
    assert jc.identify(region("soup"), [7105501, 7101101], store)[0] == 7105501
    for f in (0.7, 1.4):                                    # 창이 작거나 커도(해상도 차이) 밀린 카드를 찾는다
        r = cv2.resize(region("soup"), None, fx=f, fy=f, interpolation=cv2.INTER_LINEAR)
        assert jc.identify(r, [7101101, 7105501], store)[0] == 7105501
    assert jc.identify(region("soup"), [BUGUL, GONGNYEO], store)[0] is None         # 후보에 없으면 넓은 탐색도 채택하지 않는다


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



def test_missing_reference_does_not_adopt_remaining_candidate(store):
    """후보 2개 중 하나의 참조가 없으면 남은 하나를 채택하지 않는다 (잘못된 아르카나 필터 방지)."""
    MISSING = 999999999
    aid, score, margin = jc.identify(region("head"), [MISSING, BUGUL], store)
    assert aid is None
    aid, _, _ = jc.identify(region("head"), [BUGUL], store)         # 후보가 원래 1개면 기존대로
    assert aid == BUGUL


def test_download_failure_is_negatively_cached(tmp_path, monkeypatch):
    s = jc.CardStore(str(tmp_path), download=True, sleep=0)
    calls = []

    def boom(aid):
        calls.append(aid)
        raise OSError("net")
    monkeypatch.setattr(s, "_fetch", boom)
    assert s.get_image(1) is None and s.get_image(1) is None
    assert calls == [1]


def test_fetch_uses_unique_tmp_files(tmp_path, monkeypatch):
    import io
    ok, buf = cv2.imencode(".png", np.full((40, 30, 3), 128, np.uint8))
    data = buf.tobytes()

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(jc.urllib.request, "urlopen", lambda *a, **k: R(data))
    s = jc.CardStore(str(tmp_path), sleep=0)
    assert s._fetch(5) is not None and s._fetch(5) is not None
    assert sorted(os.listdir(tmp_path)) == ["5.png"]               # 임시 파일이 남지 않음


def test_strong_margin_accepts_lower_score(monkeypatch):
    """실기: 정답이 뚜렷(격차 0.28)한데 점수 0.37 이라 식별 실패하던 경우를 채택한다."""
    import numpy as np
    img = np.zeros((10, 10, 3), np.uint8)
    monkeypatch.setattr(jc, "score_candidates", lambda r, ids, store=None, wide=False: {1: 0.37, 2: 0.09})
    assert jc.identify(img, [1, 2])[0] == 1
    monkeypatch.setattr(jc, "score_candidates", lambda r, ids, store=None, wide=False: {1: 0.37, 2: 0.25})   # 격차 작음 -> 거부
    assert jc.identify(img, [1, 2])[0] is None
    monkeypatch.setattr(jc, "score_candidates", lambda r, ids, store=None, wide=False: {1: 0.25, 2: 0.01})   # 점수 너무 낮음 -> 거부
    assert jc.identify(img, [1, 2])[0] is None
