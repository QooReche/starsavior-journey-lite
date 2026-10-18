"""tools/check_databook.py: 어긋남을 실제로 잡아내는지 (가짜 databook 데이터로 확인)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import check_databook  # noqa: E402
import jh_relic        # noqa: E402


def _relic_entry(sets):
    return {"t": "고대 구원자의 유물", "sel": {"sets": sets}}


def _real_sets():
    return [[f"고대 구원자의 부위 {c}" for c, _ in route] for route in jh_relic.ROUTES.values()]


def test_relic_matches_when_same():
    assert check_databook.check_relic([_relic_entry(_real_sets())]) == []


def test_relic_reports_missing_code():
    sets = _real_sets()
    sets[2] = sets[2][:2]                         # IX 가 빠진 묶음
    assert any("묶음 3" in m for m in check_databook.check_relic([_relic_entry(sets)]))


def test_relic_reports_when_structure_missing():
    assert "찾지 못함" in check_databook.check_relic([{"t": "x"}])[0]


def test_schedule_detects_time_mismatch_and_missing():
    jev = [{"t": "여가 시간", "tm": ["9월 하순 (행동 후)"]}]       # 앱은 3월 중순
    missing, mismatch = check_databook.check_schedule(jev)
    assert any(m[3] == "여가 시간" for m in mismatch)
    assert "구원단 보급" in missing


def test_schedule_accepts_matching_time():
    jev = [{"t": "여가 시간", "tm": ["3월 중순 (행동 후)", "7월 초순"]}]
    _, mismatch = check_databook.check_schedule(jev)
    assert not any(m[3] == "여가 시간" for m in mismatch)
