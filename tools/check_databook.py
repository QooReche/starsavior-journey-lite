"""개발용: 앱에 내장한 일정·유물 경로를 databook(starsavior-databook) 데이터와 대조해 어긋난 곳을 보고한다.

앱에는 번들하지 않는다. 데이터북 갱신 때 손으로 비교하던 일을 대신하는 점검 도구.

사용법 (jev.json 은 data/jev.js 의 `window.__SSD.jev = {...}` 를 JSON 으로 저장한 것):
    python -X utf8 tools\\check_databook.py <jev.json 경로>
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import jh_relic          # noqa: E402
import jh_schedule       # noqa: E402

_PREFIX = re.compile(r"^\([^)]*\)\s*")
# 이름이 일반적이라 databook 의 단일 이벤트와 대응하지 않는 일정 문구
_GENERIC = ("아르카나 이벤트", "구원자 이벤트", "인자 계승", "평가전", "별빛의 인도")


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["jev"] if isinstance(data, dict) and "jev" in data else data


def check_schedule(jev):
    """고정/확률/랜덤 이벤트가 databook 에 있는지, 그 시기(예: '3월 중순')가 databook 시기 목록에 있는지"""
    by_title = {e.get("t"): e for e in jev}
    missing, mismatch = set(), []
    for turn, label, _title, befores, currents, afters in jh_schedule.SCHEDULE:
        for _region, occur, _kind, names in befores + currents + afters:
            for raw in names:
                name = _PREFIX.sub("", raw)
                if any(g in name for g in _GENERIC):
                    continue
                ev = by_title.get(name)
                if ev is None:
                    missing.add(name)
                    continue
                times = ev.get("tm") or []
                if times and not any(label in t for t in times):
                    mismatch.append((turn, label, occur, name, times))
    return sorted(missing), mismatch


def check_relic(jev):
    """'고대 구원자의 유물' 묶음(선택지별 코드 3종)이 jh_relic.ROUTES 와 같은지"""
    sets = None
    for e in jev:
        s = (e.get("sel") or {}).get("sets")
        if s and any("고대 구원자의" in x for grp in s for x in grp):
            sets = s
            break
    if sets is None:
        return ["databook 에서 '고대 구원자의 유물' 묶음을 찾지 못함 (구조가 바뀌었을 수 있음)"]
    theirs = [[x.rsplit(" ", 1)[-1] for x in grp] for grp in sets]
    ours = [[c for c, _ in route] for route in jh_relic.ROUTES.values()]
    return [f"묶음 {i + 1}: databook {t} / 앱 {o}"
            for i, (t, o) in enumerate(zip(theirs, ours)) if t != o] + (
        [f"묶음 개수 다름: databook {len(theirs)} / 앱 {len(ours)}"] if len(theirs) != len(ours) else [])


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    jev = load(argv[1])
    missing, mismatch = check_schedule(jev)
    relic = check_relic(jev)
    print(f"[일정] databook 이벤트 {len(jev)}건과 대조")
    print(f"  databook 에 같은 이름이 없음 {len(missing)}건: " + (", ".join(missing) or "없음"))
    print(f"  시기가 databook 목록에 없음 {len(mismatch)}건")
    for turn, label, occur, name, times in mismatch:
        print(f"    {turn}턴 {label} [{occur}] {name} -> databook: {' / '.join(times)}")
    print("[유물 경로] " + ("일치" if not relic else ""))
    for line in relic:
        print("  " + line)
    return 1 if (mismatch or relic) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
