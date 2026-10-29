"""구원자 추천 아르카나: 추출기(슬림 JSON), 식별(OCR 픽스처 4장), 툴팁 HTML, 호버, 실패 시 비파괴."""
import json
import os
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import jh_savior as sv

HERE = os.path.dirname(__file__)
FX = os.path.join(HERE, "fixtures", "savior")
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import extract_savior_reco as ex  # noqa: E402


def text_of(html):
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ")


@pytest.fixture(scope="module")
def saviors():
    s = sv.load_reco()
    assert s, "assets/savior_reco.json 을 읽지 못함"
    return s


def find(saviors, name, subtitle):
    return next(s for s in saviors if s["name"] == name and s["subtitle"] == subtitle)


# ------------------------------------------------------------------ 슬림 JSON 변환 (사이트 로직 포팅)
SITE_DIR = os.path.join(HERE, "..", "..", "..", "temp", "journeyhelper", "work", "site_2026-10-29")      # 사이트 사본(저장소 밖, 없으면 건너뜀)
SITE_ENV = os.environ.get("JH_SITE_DIR") or SITE_DIR
needs_site = pytest.mark.skipif(not os.path.exists(os.path.join(SITE_ENV, "app-core.js")), reason="사이트 사본 없음")

CORE = '''
const ARCANA_DETAIL_IDS = { "별칭카드": 7000002 };
const ARCANA_LIBRARY = {
  "약칭": [ { name: "별칭카드", image: `x` } ],
  "둘다": [ { name: "카드A", image: "a" }, { name: "카드B", image: "b" } ]
};
ARCANA_LIBRARY["오탈자"] = ARCANA_LIBRARY["약칭"];
Object.values(ARCANA_LIBRARY).flat().forEach((card) => {
  if (!ARCANA_LIBRARY[card.name]) {
    ARCANA_LIBRARY[card.name] = [card];
  }
});
const COMMON_ARCANA_SLOTS = [ { name: "카드A", note: "" }, { name: "카드B", note: "" }, { name: "약칭", note: "" },
  { name: "카드A", note: "" }, { name: "카드B", note: "" } ];
const DEFENDER_PURE_TANK_ARCANA = Object.freeze({ recommended: [ { name: "카드A or 카드B", note: "" } ],
  alternatives: [ { name: "약칭", note: "카드A 대체" }, { name: "", note: "대체불가" } ] });
const DEFENDER_ATTACK_TANK_ARCANA = Object.freeze({ recommended: [ { name: "카드B", note: "" } ], alternatives: [] });
const STRIKER_ARCANA_SWAP_IDS = new Set(["swapper"]);
const STRIKER_ARCANA_TARGET_IDS = new Set(["target"]);
const PENDING_ARCANA = () => Array.from({ length: 5 }, () => ({ name: "미정", note: "미정" }));
const SAVIORS = [
  { "id": "plain", "name": "일반", "subtitle": "부제", "element": "sun", "className": "레인저", "grade": "SSR",
    "detail": { "arcana": { "pve": [ {"name": "카드A or 카드B", "note": ""}, {"name": "약칭", "note": "메모 ] 괄호"}, {"name": "없는카드", "note": ""}, null, null ],
      "alternatives": [ {"name": "카드B", "note": "카드A 대체"}, null, null, null, null ], "pvp": PENDING_ARCANA() } } },
  { "id": "commoner", "name": "공용", "subtitle": "x", "className": "레인저", "detail": { "arcana": { "pve": [ {"name": "공용 아르카나", "note": ""}, null, null, null, null ], "alternatives": [] } } },
  { "id": "swapper", "name": "스왑", "subtitle": "y", "className": "스트라이커", "detail": { "arcana": { "pve": [ {"name": "카드A", "note": ""}, null, null, null, null ],
      "alternatives": [ {"name": "카드B", "note": ""}, null, null, null, null ] } } },
  { "id": "tank", "name": "탱커", "subtitle": "z", "className": "디펜더", "detail": { "arcana": { "pve": [ {"name": "카드A", "note": ""}, null, null, null, null ] } } },
  { "id": "pending", "name": "미정이", "subtitle": "w" }
];
const ARCANA_CATEGORIES = [];
'''
APP = '''
core.addEventListener("load", () => {
  const robertaBuild = SAVIORS.find((savior) => savior.id === "roberta");
});
'''
ARCANAS = {"arcanas": [{"id": 7000010, "name": {"ko": "카드A"}}, {"id": 7000011, "name": {"ko": "카드B"}},
                       {"id": 7000002, "name": {"ko": "별칭카드"}}]}


def _by_key(data):
    return {s["key"]: s for s in data["saviors"]}


def _names(slot):
    return [o["name"] for o in slot["options"]] if slot else None


def test_js_to_json_handles_site_literals():
    v = ex.js_to_json('{ recommended: [ { name: "a: b, c", note: "" }, ], alternatives: [] , x: null }')
    assert v == {"recommended": [{"name": "a: b, c", "note": ""}], "alternatives": [], "x": None}


def test_extract_synthetic_site_rules():
    data, rs = ex.build(CORE, APP, ARCANAS)
    sv_ = _by_key(data)
    plain = sv_["plain"]
    assert [(o["name"], o["arcanaId"]) for o in plain["slots"][0]["options"]] == [("카드A", 7000010), ("카드B", 7000011)]   # or 분할 + 하이드레이트 ID
    assert plain["slots"][1]["options"][0]["arcanaId"] == 7000002 and plain["slots"][1]["note"] == "메모 ] 괄호"              # 약칭 -> 카드
    assert plain["slots"][2]["options"][0] == {"name": "없는카드", "arcanaId": None, "unresolved": True}                        # 미해결은 원문만
    assert plain["slots"][3] is None
    assert [(a["pos"], _names(a), a["slot"], a["target"]) for a in plain["alternatives"]] == [(0, ["카드B"], 0, "카드A")]       # 대상 슬롯 연결
    assert sv_["commoner"]["commonSet"] and [_names(x)[0] for x in sv_["commoner"]["slots"]] == ["카드A", "카드B", "별칭카드", "카드A", "카드B"]
    assert sv_["pending"]["slots"] == [] and "sets" not in sv_["pending"]                                                       # 데이터 없음 -> 미정
    t = sv_["tank"]                                                                                                              # 디펜더: 구원자 값이 아니라 2세트 프리셋
    assert t["slots"] == [] and [x["label"] for x in t["sets"]] == ["퓨어탱커", "공격탱커"]
    assert _names(t["sets"][0]["slots"][0]) == ["카드A", "카드B"] and t["sets"][0]["alternatives"][0]["slot"] == 0
    assert t["sets"][0]["alternatives"][1] == {"options": [], "note": "대체불가", "pos": 1, "slot": None, "target": None}
    assert "없는카드" in rs.unresolved
    json.dumps(data, ensure_ascii=False)


def test_striker_swap_and_eternal_bond_rules():
    consts = {"swap_ids": {"s"}, "target_ids": set(), "common": []}
    names = ["음독의 각오", "완벽한 바니걸", "하얀 달의 온기는 햇빛처럼", "영원 속박의 굴레", "단점 보완 맞춤 훈련"]
    R = ex.Resolver([], {}, {n: [n] for n in names}, [])
    pve = [{"name": "단점 보완 맞춤 훈련", "note": ""}, {"name": "완벽한 바니걸", "note": ""}, {"name": "하얀 달의 온기는 햇빛처럼", "note": ""}]
    sav = {"id": "s", "className": "스트라이커", "detail": {"arcana": {"pve": pve, "alternatives": [{"name": "음독의 각오", "note": "x"}]}}}
    kind, p, alts = ex.final_build(sav, R, consts)
    assert [x["name"] for x in p][1] == "음독의 각오"                                         # 스트라이커 교체: 바니걸 -> 음독의 각오
    got = [(a["name"], a["note"]) for a in alts if a]
    assert ("완벽한 바니걸", "음독의 각오 대체") in got                                         # 대체 쪽은 음독 -> 바니걸 + 대체 문구
    assert ("노 페인, 노 게인", "단점 보완 맞춤 훈련 대체") in got and ("어느 한 기사의 맹세", "하얀 달의 온기는 햇빛처럼 대체") in got
    assert got[-1] == ("영원 속박의 굴레", "하얀 달의 온기는 햇빛처럼 대체")                    # 영원 속박은 마지막에 덧붙임
    sup = {"id": "q", "className": "서포터", "detail": {"arcana": {"pve": pve, "alternatives": []}}}
    _, _, alts2 = ex.final_build(sup, R, consts)
    assert "영원 속박의 굴레" not in [a["name"] for a in alts2 if a]                           # 서포터/디펜더는 제외
    assert ("누구보다 프로페셔널", "공녀, 왕좌에 오르다 대체") in [(a["name"], a["note"]) for a in alts2]


def test_extract_main_writes_file(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "app-core.js").write_text(CORE, encoding="utf-8")
    (d / "app.js").write_text(APP, encoding="utf-8")
    (d / "arcanas.json").write_text(json.dumps(ARCANAS), encoding="utf-8")
    out = tmp_path / "o" / "r.json"
    assert ex.main(["x", str(d), "--out", str(out)]) == 2                      # 폴더 이름에서 날짜를 못 얻으면 --generated 필수(생략하지 않는다)
    assert not out.exists()
    assert ex.main(["x", str(d), "--out", str(out), "--generated", "2026-10-29"]) == 0
    assert len(sv.load_reco(str(out))) == 5 and json.load(open(out, encoding="utf-8"))["generated"] == "2026-10-29"


def test_extract_bad_input_raises_clearly():
    with pytest.raises(ValueError):
        ex.build("const X = 1;", "", ARCANAS)


def test_unported_site_patch_is_reported():
    assert ex.unported_patches("", 'const x = SAVIORS.find((s) => s.id === "new-guy");') == ["new-guy"]
    assert ex.unported_patches("", 'SAVIORS.find((s) => s.id === "roberta")') == []


def test_bundled_json_quality(saviors):
    assert len(saviors) >= 50
    assert all(len(s["slots"]) in (0, 5) for s in saviors)
    pools = [sl for s in saviors for sl in (s["slots"] + s["alternatives"] + [x for st in s.get("sets", []) for x in st["slots"] + st["alternatives"]]) if sl]
    opts = [o for sl in pools for o in sl["options"]]
    assert sum(1 for o in opts if o["arcanaId"]) / len(opts) > 0.99          # 해석률 (실측 100%)
    assert sum(1 for s in saviors if not (any(s["slots"]) or s.get("sets"))) == 0      # 미정 구원자 수 (2026-10-17 사본 기준)


def _alt(s, pos):
    return next(a for a in s["alternatives"] if a["pos"] == pos)


def test_asherah_voyager_matches_site_screen(saviors):
    """사용자가 준 사이트 화면의 아세라(보이저) = 스트라이커 9명 규칙(최종 5슬롯 + 대체)."""
    s = find(saviors, "아세라", "보이저 구원단")
    assert [_names(x) for x in s["slots"]] == [["단점 보완 맞춤 훈련"], ["불굴의 역작"], ["꽃들에게 죽음을"], ["음독의 각오"], ["자칭 베테랑 탐정의 수사법"]]
    assert [a["pos"] for a in s["alternatives"]] == [0, 1, 2, 3]                       # 슬롯5 대체 없음
    assert _names(_alt(s, 0)) == ["노 페인, 노 게인"]
    assert _names(_alt(s, 1)) == ["메이드 바이 페트라♡", "별을 보며 꿈을"]
    assert _names(_alt(s, 2)) == ["하얀 달의 온기는 햇빛처럼", "어느 한 기사의 맹세"] and _alt(s, 2)["note"] == "부족한 자리 대체"
    assert _names(_alt(s, 3)) == ["완벽한 바니걸"]
    assert not s.get("commonSet")


def test_other_saviors_match_site_logic(saviors):
    smile = next(s for s in saviors if s["key"] == "smile")                            # 전용 대체 분기(buildAlternativeArcana smile)
    assert [_names(x)[0] for x in smile["slots"]] == ["단점 보완 맞춤 훈련", "허수의 개척자", "꽃들에게 죽음을", "종말은 소녀의 얼굴을 하고 있다.", "휴가 준비는 쇼핑에서부터!"]
    assert [(a["pos"], _names(a)[0], a["slot"]) for a in smile["alternatives"]] == [
        (0, "노 페인, 노 게인", 0), (1, "메이드 바이 페트라♡", 2), (2, "하얀 달의 온기는 햇빛처럼", 3), (3, "불굴의 역작", 1)]
    sup = next(s for s in saviors if s["key"] == "frey")                              # 서포터(10.29 표준 서포터 세팅): 사이트가 넣은 대체 목록 그대로
    assert [(_names(a)[0], a["note"]) for a in sup["alternatives"]] == [
        ("어느 한 기사의 맹세", "하얀 달의 온기는 햇빛처럼 대체"), ("누구보다 프로페셔널", "공녀, 왕좌에 오르다 대체"), ("언더커버 캅", "본 투 비 와일드 대체"), ("하늘의 시련", "부족한 자리 대체")]   # 10.29: 표준 서포터 일괄 세팅(대체 목록을 그대로 씀)
    lugh = next(s for s in saviors if s["key"] == "lugh")                             # 10.29 리워크: 더는 '공용 아르카나'가 아니라 전용 5장 + 사이트 대체 목록 그대로
    assert not lugh.get("commonSet") and [_names(x)[0] for x in lugh["slots"]] == [
        "단점 보완 맞춤 훈련", "불굴의 역작", "탐닉자의 유흥거리", "꽃들에게 죽음을", "영원 속박의 굴레"]
    assert [_names(a)[0] for a in lugh["alternatives"]] == ["노 페인, 노 게인", "메이드 바이 페트라♡", "하얀 달의 온기는 햇빛처럼"]
    haydee = next(s for s in saviors if s["key"] == "haydee")                         # 디펜더: 퓨어탱커/공격탱커 프리셋
    assert [x["label"] for x in haydee["sets"]] == ["퓨어탱커", "공격탱커"] and haydee["slots"] == []
    assert [_names(x)[0] for x in haydee["sets"][0]["slots"]] == ["조용한 휴식 시간", "꽃들에게 죽음을", "한여름밤의 하늘", "본 투 비 와일드", "하얀 달의 온기는 햇빛처럼"]
    assert haydee["sets"][1]["alternatives"][1]["note"] == "단점 보완 맞춤 훈련은 절대 대체불가합니다" and haydee["sets"][1]["alternatives"][1]["options"] == []
    lydia = next(s for s in saviors if s["key"] == "lydia")                           # app.js 패치(이게 옷이라고요?) + 대체 그대로
    assert _names(lydia["slots"][4]) == ["이게 옷이라고요?"] and lydia["slots"][4]["options"][0]["arcanaId"]
    roberta = next(s for s in saviors if s["key"] == "roberta")
    assert _names(roberta["slots"][4]) == ["음독의 각오", "자칭 베테랑 탐정의 수사법"]


@needs_site
def test_regenerating_from_site_copy_equals_bundled_json():
    """저장소의 assets/savior_reco.json 이 사이트 사본(2026-10-17)에서 추출기로 재생성한 것과 같다."""
    rd = lambda n: open(os.path.join(SITE_ENV, n), encoding="utf-8-sig").read()
    data, rs = ex.build(rd("app-core.js"), rd("app.js"), json.loads(rd("arcanas.json")))
    assert data["unportedPatches"] == [] and rs.stats["unresolved"] == 0
    shipped = json.load(open(sv.default_data_path(), encoding="utf-8-sig"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", shipped["generated"])         # 원격 갱신이 비교하는 날짜(추출기가 사본 폴더 이름에서 넣는다)
    assert ex.stamp(data, shipped["generated"]) == shipped
    assert ex.site_date("x/site_2026-10-29") == "2026-10-29" and ex.site_date("x/other") is None


@needs_site
@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_port_equals_site_js_executed_in_node():
    """사이트 JS(app-core.js + app.js)를 node 로 실제 실행한 최종 PVE/대체 슬롯(원문 name/note)과 해석된 카드 이름이 전원 포팅 결과와 같다."""
    out = subprocess.run(["node", os.path.join(HERE, "..", "tools", "site_logic_check.js"), SITE_ENV], capture_output=True, timeout=120)
    node = json.loads(out.stdout.decode("utf-8-sig"))
    rd = lambda n: open(os.path.join(SITE_ENV, n), encoding="utf-8-sig").read()
    core, arc = rd("app-core.js"), json.loads(rd("arcanas.json"))
    saviors_ = ex.parse_savior_block(core)
    consts = ex.parse_site_constants(core)
    ex.apply_load_time_patches(saviors_, consts)
    R = ex.Resolver(arc["arcanas"], ex.parse_detail_ids(core), ex.parse_arcana_library(core), consts["common"])
    norm = lambda l, k: ([None if not x else {"name": x["name"], "note": x.get("note") or ""} for x in l] + [None] * k)[:max(k, len(l))]
    assert len(node) == len(saviors_) >= 50
    for s in saviors_:
        n = node[s["id"]]
        kind, pve, alts = ex.final_build(s, R, consts)
        assert n["kind"] == kind, s["id"]
        if kind == "defender":
            continue
        k, ka = max(5, len(n["pve"]), len(pve)), max(5, len(n["alt"]), len(alts))
        assert norm(n["pve"], k) == norm(pve, k), s["id"]
        assert norm(n["alt"], ka) == norm(alts, ka), s["id"]
        assert n["pveChoices"] == [R.choices(x["name"]) if x else [] for x in pve], s["id"]
        assert n["altChoices"] == [R.choices(x["name"]) if x else [] for x in alts], s["id"]
@needs_site
@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_stat_priority_and_equipment_equal_site_js_executed_in_node():
    """여정 스탯 우선순위(문구 포함)와 PVE 장비가 사이트 JS 를 node 로 실제 실행한 결과와 전원 같다."""
    out = subprocess.run(["node", os.path.join(HERE, "..", "tools", "site_logic_check.js"), SITE_ENV], capture_output=True, timeout=120)
    node = json.loads(out.stdout.decode("utf-8-sig"))
    rd = lambda n: open(os.path.join(SITE_ENV, n), encoding="utf-8-sig").read()
    data, _ = ex.build(rd("app-core.js"), rd("app.js"), json.loads(rd("arcanas.json")))
    strip = lambda x: re.sub(r"^[※-]\s*", "", x or "")
    for s in data["saviors"]:
        n = node[s["key"]]
        site_rows = [{"label": strip(r.get("label")), "note": strip(r.get("note")), "groups": r["groups"]} for r in n["stat"]]
        assert s["statPriority"] == site_rows, s["key"]
        assert ex.slim_equipment(n["equipPve"]) == s.get("equipment"), s["key"]
        expected = [r["note"] for r in s["statPriority"] if r["note"]] + ([s["statNote"]] if s.get("statNote") else [])
        assert [strip(x) for x in n["statNotes"]] == expected, s["key"]


# ------------------------------------------------------------------ 식별 로직
def test_homonyms_are_split_by_subtitle(saviors):
    voy, wal = find(saviors, "아세라", "보이저 구원단"), find(saviors, "아세라", "왈츠 오브 스타라이트")
    assert sv.identify(saviors, ["보이저구원단"], ["아세라"]) == ([voy], "subtitle")
    assert sv.identify(saviors, ["왈츠오브스타라이트"], ["아세라"]) == ([wal], "subtitle")
    assert sv.identify(saviors, ["보이져구원단"], ["아세라_"])[0] == [voy]           # 실제 OCR 오인식('져')
    assert sv.identify(saviors, ["월츠오브스타라이트]"], ["아세라"])[0] == [wal]


def test_unreadable_subtitle_lists_all_candidates(saviors):
    m, how = sv.identify(saviors, [], ["아세라"])
    assert how == "ambiguous" and len(m) == 2 and {x["subtitle"] for x in m} == {"보이저 구원단", "왈츠 오브 스타라이트"}
    m2, how2 = sv.identify(saviors, ["알수없는글자열"], ["아세라"])
    assert how2 == "ambiguous" and len(m2) == 2
    html = sv.format_html(m, 1.0)
    assert "같은 이름 구원자가 2명" in text_of(html) and "달" in text_of(html) and "스트라이커" in text_of(html)   # 속성·직업 병기


def test_unique_name_needs_no_subtitle(saviors):
    m, how = sv.identify(saviors, [], ["로베르타"])
    assert how == "name" and [x["key"] for x in m] == ["roberta"]


def test_identify_garbage_is_harmless(saviors):
    assert sv.identify(saviors, [], []) == ([], "none")
    assert sv.identify(saviors, None, None) == ([], "none")
    assert sv.identify(saviors, ["가나다"], ["힣힣힣힣"])[1] == "unknown"      # 데이터에 없는 이름 -> 아직 데이터 없음 표시
    assert sv.identify([], ["a"], ["b"]) == ([], "none")
    assert sv.identify(saviors, [None, 3], ["아세라"])[1] in ("ambiguous", "none")       # 잘못된 후보 항목에도 예외 없음


def test_header_match():
    for ok in ("여정시작", "여정 시작", "여정시작9", "여정시작'0", "여정시작이", "여정시작?"):
        assert sv.match_header(ok), ok
    for bad in ("", None, "여정 이벤트", "훈련 이벤트", "프리셋", "여정 시작 정보 확인하기 버튼", "전체 해제"):
        assert not sv.match_header(bad), bad


# ------------------------------------------------------------------ 스크린샷 4장 (OCR 실측)
SHOTS = [("asherah_normal", "아세라", "보이저 구원단"), ("asherah_cosmic", "아세라", "왈츠 오브 스타라이트"),
         ("carmen_normal", "카르멘", "모나스티르 기사단"), ("carmen_another", "카르멘", "이터널 프로미스")]


def _ocr():
    import journey_helper as jh
    try:
        return jh.KoreanOCR(os.path.join(HERE, "..", "models"))
    except Exception as e:
        pytest.skip(f"OCR 사용 불가: {e}")


def _crop(img, frac):
    h, w = img.shape[:2]
    return cv2.cvtColor(img[int(h * frac["top"]):int(h * (frac["top"] + frac["height"])),
                            int(w * frac["left"]):int(w * (frac["left"] + frac["width"]))], cv2.COLOR_BGR2GRAY)


def _read_screen(ocr, img):
    import journey_helper as jh
    rf = jh.REGION_FRACTIONS
    text, conf = ocr.read_line(cv2.resize(_crop(img, rf["savior_header"]), None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC))
    subs = jh.read_title_candidates(ocr, _crop(img, rf["savior_subtitle"]), min_conf=0.3)
    names = jh.read_title_candidates(ocr, _crop(img, rf["savior_name"]), min_conf=0.3)
    return (conf >= 0.4 and sv.match_header(text)), subs, names


@pytest.mark.parametrize("scale", [1.0, 0.92, 1.08])
@pytest.mark.parametrize("shot,name,subtitle", SHOTS)
def test_ocr_identifies_savior_on_real_screens(saviors, shot, name, subtitle, scale):
    """실제 '여정 시작' 화면 4장(해상도가 조금씩 다름, 추가로 ±8% 리사이즈)에서 (부제, 이름) 이 읽혀 구원자가 하나로 정해진다."""
    img = cv2.imdecode(np.fromfile(os.path.join(FX, shot + ".png"), np.uint8), cv2.IMREAD_COLOR)
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    header, subs, names = _read_screen(_ocr(), img)
    assert header, "머리글 '여정 시작' 을 못 읽음"
    matches, how = sv.identify(saviors, subs, names)
    assert how == "subtitle" and [(m["subtitle"], m["name"]) for m in matches] == [(subtitle, name)], (subs, names)


def test_ocr_header_absent_on_other_screens():
    """같은 영역을 이벤트 화면 크롭(일정 날짜 픽스처)에서 읽어도 '여정 시작' 으로 오인하지 않는다."""
    ocr = _ocr()
    import glob
    paths = glob.glob(os.path.join(HERE, "fixtures", "schedule", "date_*.png"))[:3]
    assert paths
    for p in paths:
        g = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_GRAYSCALE)
        text, _ = ocr.read_line(cv2.resize(g, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC))
        assert not sv.match_header(text)


# ------------------------------------------------------------------ 툴팁 텍스트
def _png(path, w=40, h=70):
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), 90, np.uint8))
    buf.tofile(str(path))


def test_tooltip_text_slots_alternatives_and_note(saviors):
    s = next(x for x in saviors if x["key"] == "roberta")
    t = text_of(sv.format_reco_html(s, 1.0, None))
    assert "로베르타" in t and "궤도 수색대" in t and "별" in t and "스트라이커" in t
    for i in "12345":
        assert i in t
    assert "허수의 개척자" in t and "또는" in t and "불굴의 역작" in t            # 'A or B' 슬롯
    assert "↳ 대체 카드 →" in t and "없으면 →" not in t and "부족한 자리 대체 →" in t and "그 밖의 대체" in t and "본 투 비 와일드" in t   # 대체 안내 문구
    assert "<img" not in sv.format_reco_html(s, 1.0, None)                       # 썸네일 경로 함수가 없으면 텍스트만


def test_tooltip_thumbnails_only_when_cached(saviors, tmp_path):
    s = next(x for x in saviors if x["key"] == "roberta")
    ids = sv.thumb_ids(s)
    assert len(ids) >= 8 and len(set(ids)) == len(ids)
    have = {ids[0]: str(tmp_path / "a.png"), ids[1]: str(tmp_path / "b.png")}
    for p in have.values():
        _png(p)
    html = sv.format_reco_html(s, 1.0, lambda aid: have.get(aid) or str(tmp_path / "없음.png"))
    assert html.count("<img") == 2                                              # 캐시에 있는 것만 <img>, 나머지는 이름만
    m = re.search(r'<img src="([^"]+)" width="(\d+)" height="(\d+)"', html)
    assert m and int(m.group(3)) == 40 and abs(int(m.group(2)) - round(40 * 40 / 70)) <= 1       # 비율 유지
    assert "<img" not in sv.format_reco_html(s, 1.0, lambda aid: have.get(aid), detail=4)          # 가장 압축 단계는 썸네일 없음
    assert sv.format_reco_html(s, 1.0, lambda aid: (_ for _ in ()).throw(RuntimeError()))          # 경로 함수 예외도 텍스트로 폴백


def test_tooltip_handles_empty_and_odd_data(saviors):
    pending = {"name": "미정이", "slots": [], "alternatives": []}
    assert "미정" in text_of(sv.format_reco_html(pending))
    tank = next(x for x in saviors if x.get("sets"))                                  # 디펜더: 퓨어탱커/공격탱커 2세트
    tt = text_of(sv.format_reco_html(tank))
    assert "퓨어탱커" in tt and "공격탱커" in tt and "대체불가" in tt
    assert sv.format_html([]) == ""
    weird = {"name": "<b>x</b>", "slots": [{"options": [{"name": "<script>", "arcanaId": None}], "note": "&"}, None, None, None, None],
             "alternatives": [{"slot": 0, "options": [], "note": ""}, "깨짐", None]}
    html = sv.format_html([weird], 1.0, None)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert sv.format_html([{"slots": 5}], 1.0, None) == ""                      # 구조가 깨진 데이터는 예외 대신 빈 문자열


def test_load_reco_bad_files(tmp_path):
    assert sv.load_reco(str(tmp_path / "없음.json")) == []
    p = tmp_path / "bad.json"
    p.write_text("{깨짐", encoding="utf-8")
    assert sv.load_reco(str(p)) == []
    p.write_text('{"saviors": [1, {"x": 1}]}', encoding="utf-8")
    assert sv.load_reco(str(p)) == []


# ------------------------------------------------------------------ 호버 / 설정 / 충돌 방지
def _hover(matches, pos, enabled=True, in_journey=False, arcana=True):
    import journey_helper as jh
    stub = SimpleNamespace(savior_matches=matches, in_journey=in_journey, arcana_screen=arcana,
                           settings={"show_savior_reco": enabled, "tooltip_font_scale": 1.0}, _thumb_path=lambda aid: None)
    rect = {"left": 100, "top": 50, "width": 1600, "height": 900}
    return jh.MouseHoverManager._savior_hover_html(stub, rect, SimpleNamespace(x=lambda: pos[0], y=lambda: pos[1]))


def test_always_shown_toggle_and_event_screen_conflict(saviors):
    m = [next(x for x in saviors if x["key"] == "roberta")]
    anywhere = (100 + 1600 * 0.5, 50 + 900 * 0.5)
    fn = _hover(m, anywhere)                                                   # 호버 없이 화면 어디에 마우스가 있어도 표시
    assert callable(fn) and "로베르타" in text_of(fn(0))
    assert _hover(m, anywhere, enabled=False) is None                          # 설정 끔
    assert _hover(m, anywhere, in_journey=True) is None                        # 이벤트 화면에서는 안 뜸
    assert _hover(None, anywhere) is None                                      # 식별 못 했으면 안 뜸
    assert _hover(m, anywhere, arcana=False) is None                           # 구원자 고르기/인자작 화면(편성 화면 아님)에서는 안 뜸


def test_hover_and_schedule_regions_do_not_overlap():
    import journey_helper as jh
    l, t, r, b = sv.SAVIOR_HOVER
    sl, st, sr, sb = jh.SCHEDULE_HOVER
    assert b <= st or t >= sb or r <= sl or l >= sr or t >= sb


def test_tooltip_placed_between_stats_and_cards():
    import journey_helper as jh
    rect = {"left": 0, "top": 0, "width": 1600, "height": 900}
    avail = (0, 0, 1920, 1080)
    x, y, clip = jh.place_tooltip(rect, avail, 400, False, sv.TOOLTIP_TOP_FRAC, sv.TOOLTIP_X_FRAC)
    assert x >= 1600 * 0.235 and clip is None                                   # 좌측 스탯 패널(~0.235)을 가리지 않음
    assert x + 440 <= 1600 * 0.53                                               # 우측 편성 카드(0.53~)를 가리지 않는 폭
    assert jh.place_tooltip(rect, avail, 400)[0] == 25                          # x_frac 없으면 기본 동작 그대로
    assert jh.place_tooltip(rect, avail, 400)[1] == int(900 * 0.30)


def test_setting_default_and_load(tmp_path, monkeypatch):
    import journey_helper as jh
    assert jh.DEFAULT_SETTINGS["show_savior_reco"] is True
    monkeypatch.setattr(jh, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "settings.json").write_text('{"show_savior_reco": false}', encoding="utf-8")
    assert jh.load_settings()["show_savior_reco"] is False
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    assert jh.load_settings()["show_savior_reco"] is True
    for k in ("savior_header", "savior_subtitle", "savior_name"):
        assert k in jh.REGION_FRACTIONS


def test_cached_path_does_not_download(tmp_path):
    import jh_cards
    st = jh_cards.CardStore(cache_dir=str(tmp_path), download=False)
    assert jh_cards.cached_path(7102501, st) is None
    _png(tmp_path / "7102501.png")
    assert jh_cards.cached_path(7102501, st) == str(tmp_path / "7102501.png")


def test_unknown_savior_shows_no_data_message(saviors):
    m, how = sv.identify(saviors, ["새로운부제"], ["미지의구원자"])
    assert how == "unknown" and m[0].get("unknown")
    html = sv.format_html(m)
    assert "미지의구원자" in html and "데이터가 없습니다" in html
    assert sv.identify(saviors, [], ["x"])[1] == "none"              # 한글 2자 미만 잡음은 표시하지 않는다
    one = [x for x in saviors if sum(1 for o in saviors if o["name"] == x["name"]) == 1][0]
    m2, how2 = sv.identify(saviors, ["전혀다른문구"], [one["name"]])  # 같은 이름의 신규(이격 등) -> 기존 구원자로 오인 금지
    assert how2 == "unknown" and m2[0]["name"] == one["name"]
    assert sv.identify(saviors, [], [one["name"]])[1] == "name"      # 부제를 못 읽었으면 이름만으로


# ------------------------------------------------------------------ 아르카나 편성 화면만 판별 (9 구원자 고르기 / 10 인자작 / 11 편성 / 12 카드 고르기)
def test_match_arcana_screen_text():
    for t, k in (("전체해제", "clear"), ("전체 해제", "clear"), ("전체해지", "clear"), ("자동편성", "auto"), ("자동관성", "auto"), ("자동정성", "auto"),
                 ("특수재화우선사용-", "special"), ("특수재화우선사용", "special"), ("여정시작", "start"), ("여정 시작", "start")):
        assert sv.match_arcana_screen(t, k), (t, k)
    for t in ("", None, "_K", "'I경", "선택", "자동 장착", "확인", "프리셋1", "프리셋 마이세팅"):          # 프리셋 이름/번호는 근거가 아니다
        for k in ("clear", "auto", "special", "start"):
            assert not sv.match_arcana_screen(t, k), (t, k)
    assert "arcana_preset" not in sv.ARCANA_SCREEN_REGIONS
    assert sv.arcana_screen_hit({"clear"}) and sv.arcana_screen_hit({"auto"}) and sv.arcana_screen_hit({"special", "start"})
    assert not sv.arcana_screen_hit({"special"}) and not sv.arcana_screen_hit({"start"}) and not sv.arcana_screen_hit(set())


def test_match_picker_text():
    assert sv.match_picker("78/81", "count") and sv.match_picker("1/81", "count") and sv.match_picker("100/120", "count")
    for t in ("8/8", "-", "_-", "", None, "여정 기록"):
        assert not sv.match_picker(t, "count"), t
    assert sv.match_picker("구원자와동일한타이틀을지닌아르카나를", "guide") and sv.match_picker("스구원자와동일한타이를을지닌", "guide")
    for t in ("정기록기록없음", "정기록19435s", "", None):
        assert not sv.match_picker(t, "guide"), t


ARCANA_SHOTS = [("arcana_asherah_normal", True), ("arcana_asherah_cosmic", True), ("arcana_carmen_normal", True), ("arcana_carmen_another", True),
                ("arcana_11_equip", True), ("other_9_savior_pick", False), ("other_10_inja", False), ("picker_12", False)]


def _screen_signals(ocr, img, jh):
    """워처와 같은 방식(영역 OCR)으로 읽은 (긍정 글자 집합, 부정 신호 여부)."""
    def read(name, scales):
        g = _crop(img, jh.REGION_FRACTIONS[name])
        return [ocr.read_line(cv2.resize(g, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)) for sc in scales]
    neg = any(c >= 0.5 and sv.match_picker(t, "count") for t, c in read("picker_count", (2.0,))) or \
        any(c >= 0.5 and sv.match_picker(t, "guide") for t, c in read("picker_guide", (2.0,)))
    found = set()
    for name in ("arcana_special", "arcana_start"):
        if any(c >= 0.6 and sv.match_arcana_screen(t, sv.ARCANA_KINDS[name]) for t, c in read(name, (2.0,))):
            found.add(sv.ARCANA_KINDS[name])
    if not sv.arcana_screen_hit(found):
        for name in ("arcana_btn_clear", "arcana_btn_auto"):
            if any(c >= 0.4 and sv.match_arcana_screen(t, sv.ARCANA_KINDS[name]) for t, c in read(name, (2.0, 3.0))):
                found.add(sv.ARCANA_KINDS[name])
    return found, neg


@pytest.mark.parametrize("scale", [1.0, 0.92, 1.08])
@pytest.mark.parametrize("shot,expected", ARCANA_SHOTS)
def test_ocr_arcana_screen_only_on_equip_screen(shot, expected, scale):
    """고정 글자(전체 해제/자동 편성/특수재화 우선 사용/여정 시작)는 편성 화면에서만 읽히고, 구원자 고르기(9)·인자작(10)·카드 고르기(12)에서는 안 읽힌다.
    카드 고르기에서만 부정 신호(안내줄/보유 수)가 읽힌다 (±8% 리사이즈 포함).
    픽스처에는 '프리셋' 영역이 아예 없으므로(검정) 프리셋 이름과 무관함도 함께 보장된다."""
    import journey_helper as jh
    img = cv2.imdecode(np.fromfile(os.path.join(FX, shot + ".png"), np.uint8), cv2.IMREAD_COLOR)
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    found, neg = _screen_signals(_ocr(), img, jh)
    assert (sv.arcana_screen_hit(found) and not neg) is expected, (found, neg)
    assert neg is (shot == "picker_12")


def _watcher_stub(script):
    """_scan_arcana_screen 만 검증하는 스텁. script: 스캔마다 {영역이름: 문자열}. 안 적은 영역은 빈 문자열."""
    import journey_helper as jh
    emitted = []
    stub = SimpleNamespace(_ar_on=False, _ar_streak=0, _ar_miss=0, arcana_screen_changed=SimpleNamespace(emit=emitted.append))
    it = iter(script)
    cur = {}

    def read_region(sct, rect, name, scales=(2.0, 1.6)):
        if name == "picker_count":                                   # 한 스캔의 첫 읽기: 다음 스캔 데이터로 교체
            cur.clear()
            cur.update(next(it))
        return [(cur.get(name, ""), 0.9)]
    stub._read_region = read_region
    return stub, emitted, lambda: jh.ScreenWatcher._scan_arcana_screen(stub, None, None)


EQUIP = {"arcana_special": "특수재화우선사용", "arcana_start": "여정시작"}
BUTTONS = {"arcana_btn_clear": "전제해제"}
PICKER = {"picker_count": "78/81"}


def test_arcana_screen_hysteresis_no_flicker_and_conservative():
    import journey_helper as jh
    assert jh.ARCANA_LOST == 2
    stub, emitted, scan = _watcher_stub([{}, EQUIP, {}, EQUIP, EQUIP, {}, EQUIP, {}, {}, EQUIP, EQUIP])
    scan()
    assert emitted == [] and not stub._ar_on                               # 못 읽음 -> 보수적으로 아님
    scan()
    assert emitted == []                                                   # 1회 읽힘은 아직 확정 아님
    scan(); scan(); scan()
    assert emitted == [True] and stub._ar_on                               # 끊기면 리셋, 연속 2회 -> 편성 화면
    scan(); scan()
    assert emitted == [True] and stub._ar_on                               # 한 번 못 읽는 정도는 유지(깜빡임 없음)
    scan(); scan()
    assert emitted == [True, False] and not stub._ar_on                    # 연속 2회 못 읽으면 해제
    scan(); scan()
    assert emitted == [True, False, True]


def test_buttons_alone_and_single_fixed_text_rules():
    stub, emitted, scan = _watcher_stub([BUTTONS, BUTTONS])
    scan(); scan()
    assert emitted == [True]                                               # 버튼 줄 하나로 충분
    stub2, emitted2, scan2 = _watcher_stub([{"arcana_special": "특수재화우선사용"}] * 3 + [{"arcana_start": "여정시작"}] * 3)
    for _ in range(6):
        scan2()
    assert emitted2 == []                                                  # 고정 글자 하나만으로는 불충분


def test_preset_label_is_irrelevant():
    import journey_helper as jh
    stub, emitted, scan = _watcher_stub([{**EQUIP, "arcana_preset": "내맘대로세팅7"}] * 2)
    scan(); scan()
    assert emitted == [True]                                               # 프리셋 이름이 뭐든 판정 유지 (영역을 읽지도 않음)
    assert "arcana_preset" not in jh.REGION_FRACTIONS


def test_picker_negative_signal_hides_immediately_and_blocks_reshow():
    stub, emitted, scan = _watcher_stub([EQUIP, EQUIP, PICKER, EQUIP, EQUIP, {"picker_guide": "구원자와동일한타이틀을지닌"}, EQUIP, EQUIP])
    scan(); scan()
    assert emitted == [True]
    scan()
    assert emitted == [True, False] and not stub._ar_on                    # 부정 신호 1회 -> 즉시 해제
    scan()
    assert emitted == [True, False]                                        # 재확정에는 연속 2회가 다시 필요
    scan()
    assert emitted == [True, False, True]
    scan()
    assert emitted == [True, False, True, False]                           # 안내줄 글자로도 즉시 해제
    scan(); scan()
    assert emitted[-1] is True


def test_click_hides_until_positive_reconfirmation(monkeypatch):
    """클릭 -> 툴팁 즉시 숨김 + 편성 화면 확정 해제. 새 스캔에서 긍정 확정(1회)될 때까지 재노출 없음, 부정/불확실이면 계속 숨김, 확정되면 다시 표시."""
    import journey_helper as jh
    emitted = []
    wstub = SimpleNamespace(_ar_on=True, _ar_streak=5, _ar_miss=0, _sv_next=99.0, arcana_screen_changed=SimpleNamespace(emit=emitted.append))
    mstub = SimpleNamespace(arcana_screen=True, savior_matches=[{"name": "x"}],
                            watcher=SimpleNamespace(request_arcana_recheck=lambda: jh.ScreenWatcher.request_arcana_recheck(wstub)))
    mstub.on_arcana_screen_changed = lambda on: jh.MouseHoverManager.on_arcana_screen_changed(mstub, on)
    wstub.arcana_screen_changed = SimpleNamespace(emit=lambda on: (emitted.append(on), mstub.on_arcana_screen_changed(on)))
    state = {"down": False, "t": 100.0}
    user32 = SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if state["down"] else 0)
    monkeypatch.setattr(jh.ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(jh, "is_game_foreground", lambda: True)
    monkeypatch.setattr(jh, "is_cursor_over_game", lambda: True)
    monkeypatch.setattr(jh.time, "monotonic", lambda: state["t"])
    state["down"] = True
    assert jh.MouseHoverManager._savior_click_hold(mstub) is True                       # 클릭 순간 숨김
    assert mstub.arcana_screen is False and wstub._ar_on is False and wstub._sv_next == 0.0   # 확정 해제 + 즉시 재판정 요청
    state["down"] = False
    state["t"] = 101.5                                                                   # 0.5초가 지나도 아직 확정 전이므로 숨김 유지(재노출 없음)
    assert jh.MouseHoverManager._savior_click_hold(mstub) is False and mstub.arcana_screen is False
    # 다음 스캔이 부정/불확실이면 계속 숨김
    stub, em, scan = _watcher_stub([{}, PICKER])
    stub._ar_on, stub._ar_streak = False, jh.ARCANA_CONFIRM - 1
    scan(); scan()
    assert em == [] and not stub._ar_on
    # 긍정 한 번으로 확정 -> 다시 표시
    stub2, em2, scan2 = _watcher_stub([EQUIP])
    jh.ScreenWatcher.request_arcana_recheck(stub2)
    scan2()
    assert em2 == [True] and stub2._ar_on


def test_window_scale_k_and_geometry(saviors):
    """창 높이 기반 k = clamp(H/935, 0.6, 1.4). 보통 모드 폭은 min(440*scale*k, 0.285*W-여백)로 좌우 카드 영역을 침범하지 않고, 넓은 모드는 0.50*W."""
    assert sv.window_k(935) == 1.0 and sv.window_k(100) == 0.6 and sv.window_k(5000) == 1.4 and sv.window_k(None) == 1.0
    for W, H in ((1280, 720), (1600, 935), (1920, 1080), (1000, 560), (2560, 1440)):
        rect = {"left": 0, "top": 0, "width": W, "height": H}
        eff, w, cw = sv.tooltip_geometry(rect, 1.0, False)
        assert abs(eff - sv.window_k(H)) < 1e-9 and cw == w - 36
        assert sv.TOOLTIP_X_FRAC * W + w < 0.53 * W, (W, H, w)                      # 우측 편성 카드(0.53*W~) 비침범
        assert w <= 440 * sv.window_k(H) + 1
        eff2, w2, _ = sv.tooltip_geometry(rect, 1.0, True)
        assert w2 == round(W * sv.WIDE_WIDTH_FRAC) and sv.WIDE_X_FRAC * W + w2 <= 0.53 * W
    _, w15, _ = sv.tooltip_geometry({"width": 1600, "height": 935}, 1.5, False)
    assert w15 <= 0.285 * 1600                                                      # 설정 배율이 커도 상한으로 제한
    s = next(x for x in saviors if x["key"] == "asherah-voyager")
    small, big = sv.format_reco_html(s, 0.77, None, 0), sv.format_reco_html(s, 1.16, None, 0)
    px = lambda h: max(int(v) for v in re.findall(r"font-size:(\d+)px", h))
    assert px(small) < px(big)                                                      # 글자 크기가 배율에 따라 달라진다


def test_defender_two_sets_both_visible_with_headings(saviors):
    """퓨어탱커/공격탱커 2세트를 위아래로 모두, 소제목과 함께 표시하고, 가장 압축한 단계에서도 두 세트의 카드 이름이 모두 보인다."""
    for key in ("haydee", "carmen", "emily"):
        s = next(x for x in saviors if x["key"] == key)
        assert [st["label"] for st in s["sets"]] == ["퓨어탱커", "공격탱커"]
        for d in (0, 1, 2):
            t = text_of(sv.format_reco_html(s, 1.0, None, d))
            assert t.index("퓨어탱커") < t.index("공격탱커")
            for st in s["sets"]:
                for sl in st["slots"]:
                    for o in sl["options"]:
                        assert o["name"] in t, (key, d, o["name"])


# ------------------------------------------------------------------ 썸네일 유지 단계 (높이가 모자라도 마지막 수단 전까지 썸네일을 끝까지 보여 준다)
def test_detail_levels_keep_thumbnails_until_last_resort(saviors, tmp_path):
    s = next(x for x in saviors if x["key"] == "haydee")
    for aid in sv.thumb_ids(s):
        _png(tmp_path / f"{aid}.png")
    f = lambda aid: str(tmp_path / f"{aid}.png")
    heights = []
    for d in range(sv.MAX_DETAIL + 1):
        html = sv.format_reco_html(s, 1.0, f, d)
        heights.append([int(h) for h in re.findall(r'<img [^>]*height="(\d+)"', html)])
    assert sv.MAX_DETAIL == 4
    assert [max(h) for h in heights[:4]] == [40, 36, 30, 26]                                # 40 -> 36 -> 30 -> 26px
    assert all(len(h) >= 15 for h in heights[:4]) and len({len(h) for h in heights[:4]}) == 1   # 같은 카드 수가 모두 유지
    assert heights[4] == []                                                              # 마지막 수단에서만 제거
    for d in range(5):                                                                   # 어느 단계든 두 세트의 모든 카드 이름
        t = text_of(sv.format_reco_html(s, 1.0, f, d))
        assert all(o["name"] in t for st in s["sets"] for sl in st["slots"] for o in sl["options"])


def test_two_set_defender_wide_mode_fits_with_thumbnails(saviors, tmp_path):
    """넓은 모드(폭 0.50*1600=800px, 시작 y 0.345 -> 게임 창 아래끝까지 약 590px)에서 에데/카르멘이 썸네일을 지우지 않고 맞는다 (실제 Qt 렌더 높이)."""
    import journey_helper as jh
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        tip = jh.FixedLeftTooltip(1.0)
    except Exception as e:
        pytest.skip(f"Qt 사용 불가: {e}")
    limit = 930 - 6 - 10 - int(930 * sv.WIDE_TOP_FRAC)
    w = round(1600 * sv.WIDE_WIDTH_FRAC)
    tip.setFixedWidth(w)
    for key in ("haydee", "carmen"):
        s = next(x for x in saviors if x["key"] == key)
        assert sv.wide_mode([s])
        for aid in sv.thumb_ids(s):
            _png(tmp_path / f"{aid}.png", 62, 100)
        f = lambda aid: str(tmp_path / f"{aid}.png")
        chosen = None
        for d in range(sv.MAX_DETAIL + 1):
            html = sv.format_reco_html(s, 1.0, f, d, w - 36)
            tip._fit(html)
            if tip.height() <= limit:
                chosen = d
                break
        assert chosen is not None and chosen <= 1, (key, chosen, tip.height(), limit)     # 넓은 모드는 풀(0)~중간(1) 단계에서 맞는다                  # 썸네일을 지우지 않고 맞는다
        assert "<img" in html and max(int(h) for h in re.findall(r'<img [^>]*height="(\d+)"', html)) >= 26


def test_wide_mode_geometry_and_two_columns(saviors):
    """넓은 모드: 폭/위치가 이름 블록(y<0.34)과 우측 편성 카드(x>=0.53)를 가리지 않고, 좌측 스탯 패널(x<0.235)은 가려도 된다. 슬롯 행 + 좌우 2열."""
    import journey_helper as jh
    assert sv.WIDE_X_FRAC + sv.WIDE_WIDTH_FRAC <= 0.52 and sv.WIDE_X_FRAC < 0.235
    assert sv.WIDE_TOP_FRAC >= 0.34
    rect = {"left": 100, "top": 470, "width": 1600, "height": 930}
    avail = (0, 0, 3440, 1400)
    x, y, clip = jh.place_tooltip(rect, avail, 560, False, sv.WIDE_TOP_FRAC, sv.WIDE_X_FRAC, sv.WIDE_TOP_FRAC)
    assert y >= 470 + int(930 * 0.34) and clip is None                                         # 이름 블록 아래에서 시작(위로 밀지 않음)
    assert y + 560 + 6 <= 470 + 930                                                            # 게임 창 아래끝 안
    assert x + 800 <= 100 + 1600 * 0.53
    one = next(x for x in saviors if x["key"] == "roberta")
    assert not sv.wide_mode([one]) and not sv.wide_mode([]) and not sv.wide_mode([one, one])
    s = next(x for x in saviors if x["key"] == "haydee")
    html = sv.format_reco_html(s, 1.0, None, 0, 764)
    assert html.count("▌") == 2 and html.index("퓨어탱커") < html.index("공격탱커")
    assert html.count('width="50%"') >= 2 * 5                                                  # 슬롯 행마다 좌우 2칸
    t = text_of(html)
    assert all(o["name"] in t for st in s["sets"] for sl in st["slots"] for o in sl["options"])
    assert "단점 보완 맞춤 훈련은 절대 대체불가합니다" in t


def test_inline_alternatives_and_footer_gap(saviors):
    s = next(x for x in saviors if x["key"] == "asherah-voyager")
    html = sv.format_reco_html(s, 1.0, None, 0)
    t = text_of(html)
    assert "↳ 대체 카드 →" in t and "없으면" not in t and "또는" in t     # 대체는 한 줄 `↳ 대체 카드 → Y`, 대상 이름은 반복하지 않는다
    assert html.index('<div style="font-size:8px;">') < html.index("출처:")                        # 푸터 위 간격


def test_tooltip_limit_allows_pushing_up_to_below_top_bar():
    import journey_helper as jh
    rect = {"left": 0, "top": 470, "width": 1600, "height": 930}
    avail = (0, 0, 3440, 1400)
    x, y, clip = jh.place_tooltip(rect, avail, 790, False, 0.20, 0.245)
    assert y >= 470 + int(930 * jh.TOOLTIP_MIN_TOP_FRAC) and clip is None               # 위로 밀되 상단바 아래까지만
    assert y + 790 + 10 <= 1400
    assert jh.place_tooltip(rect, avail, 400, False, 0.20, 0.245)[1] == 470 + int(930 * 0.20)    # 맞으면 그대로


def test_single_and_two_set_share_one_grid_renderer(saviors):
    """1세트·2세트가 같은 격자 코드를 쓴다: 슬롯 수-1 개의 같은 구분선, 위쪽 정렬 번호 칸, 열 머리는 2열 이상에서만, 대체 표기 길이만 다르다."""
    divider = f'style="background-color:{sv.COL_DIVIDER}; font-size:{sv.DIVIDER_PX}px;"'
    num_td = f'<td valign="top" align="center" width="{sv.NUM_W}">'
    one = next(x for x in saviors if x["key"] == "lydia")
    two = next(x for x in saviors if x["key"] == "haydee")
    for d in range(sv.MAX_DETAIL + 1):
        h1, h2 = sv.format_reco_html(one, 1.0, None, d), sv.format_reco_html(two, 1.0, None, d, 764)
        assert h1.count(divider) == len(one["slots"]) - 1 == 4 and h2.count(divider) == len(two["sets"][0]["slots"]) - 1 == 4
        assert h1.count(num_td) == 5 and h2.count(num_td) == 5
        assert "▌" not in h1 and h2.count("▌") == 2                                      # 열 머리는 열이 2개 이상일 때만
    h1, h2 = sv.format_reco_html(one, 1.0, None, 0), sv.format_reco_html(two, 1.0, None, 0, 764)
    assert "↳ 대체 카드 →" in text_of(h1) and "↳ 대체 카드 →" in text_of(h2) and "없으면" not in text_of(h1) + text_of(h2)   # 1세트·2세트 같은 문구
    assert "퓨어탱커 · ↳ 특정 아르카나 부족 시 대체 →" in text_of(h2)                 # 그 밖의 대체: note 문구 + 세트 접두 유지
    import re as _re
    consts = [n for n in dir(sv) if n.startswith("COL_")]
    src = open(sv.__file__, encoding="utf-8").read()
    assert len(consts) >= 10 and not _re.search(r'color:#[0-9a-fA-F]{6}', src.split("def format_candidates_html")[0].split("COL_COMMON")[1])

