# -*- coding: utf-8 -*-
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402
import jh_v2  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _raw(*p):
    with open(os.path.join(FIX, *p), "rb") as f:
        return f.read()


def load_v2():
    return {k: json.loads(_raw("v2", rel)) for k, rel in jh_v2.V2_FILES.items()}


def guide():
    return json.loads(_raw("journey", "ko", "journey.json")), json.loads(_raw("arcanas", "arcanas.json"))


class Resp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class Sess:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self.handler(url, headers or {})


def v2_ok(url, headers):
    return Resp(200, _raw("v2", url.rsplit("/", 1)[1]), {"ETag": '"a"'})


def by_title(cache, title):
    return next((e for e in cache if e["rawTitle"] == title), None)


# --- 변환 ---
def test_supplement_adds_only_missing_titles():
    j, a = guide()
    base = jh_db.build_event_cache(j, a)
    full = jh_db.build_event_cache(j, a, v2=load_v2())
    base_titles = {e["cleanTitle"] for e in base}
    added = [e for e in full if e.get("v2")]
    assert added and all(e["cleanTitle"] not in base_titles for e in added)
    assert len(full) == len(base) + len(added)
    assert "리세트의일과" in base_titles                       # V2 에도 있지만 가이드 우선
    assert by_title(full, "리세트의 일과") == by_title(base, "리세트의 일과")


def test_empty_reward_title_and_difficulty_event():
    full = jh_db.build_event_cache(*guide(), v2=load_v2())
    e = by_title(full, "출발점")
    assert e and e["isArcana"] is False and e["options"][0]["reward"] == ""


def test_reward_text_buff_coin_arcana():
    full = jh_db.build_event_cache(*guide(), v2=load_v2())
    o = by_title(full, "강인한 훈련은 강인한 식사로부터")["options"][0]
    assert o["reward"] == "여정 버프 획득 고마움 +2턴"
    assert o["result"] == "여정 버프 획득 (고마움+2턴)"
    assert o["info"] and o["info"][0]["name"] == "고마움" and o["info"][0]["desc"]
    w = by_title(full, "세계")
    assert w["isArcana"] is True
    assert "오래된 주화 +8" in w["options"][0]["reward"]


def test_stat_alternatives_and_repeat_collapse():
    e = by_title(jh_db.build_event_cache(*guide(), v2=load_v2()), "여정의 끝에서")
    lines = e["options"][0]["reward"].split("\n")
    assert lines[0] == "힘 +10 or 체력 +10 or 인내 +10 or 집중 +10 or 보호 +10"
    assert lines[1] == "스탯 인자 ×5"
    assert len(e["sources"]) == 3                              # 같은 제목의 이벤트 3개 -> 출처 라벨 분리


def test_condition_item_consume_and_failure_tags():
    v2 = load_v2()
    cache = []
    ix = jh_v2.V2Index(v2, "ko")
    ent = jh_v2.convert_title("칼에 숨겨진 힘", v2["journeys"]["칼에 숨겨진 힘"], ix)
    o = ent["options"][0]
    lines = o["reward"].split("\n")
    assert o["has_condition"] and "소모" in lines[0]
    assert any(l.startswith("[SUCCESS] ") for l in lines) and any(l.startswith("[FAIL] ") for l in lines)
    assert {"name": o["items"][0]["name"], "role": "consume"} == {"name": o["items"][0]["name"], "role": o["items"][0]["role"]}
    assert cache == []


def test_item_desc_exposed_without_changing_html():
    j, a = guide()
    base = jh_db.build_event_cache(j, a)
    full = jh_db.build_event_cache(j, a, v2=load_v2())
    assert any(it.get("desc") for e in full for o in e["options"] for it in o.get("items") or [])
    for e in base:                                              # 가이드 이벤트의 표시(HTML)는 V2 가 있어도 같다
        f = by_title(full, e["rawTitle"])
        for ob, of in zip(e["options"], f["options"]):
            assert jh_db.format_option_html(ob, detail=1) == jh_db.format_option_html(of, detail=1)


def test_buff_desc_prefers_v2_over_builtin():
    v2 = load_v2()
    v2["journey_buffs"][0]["desc"]["ko-KR"] = "V2 설명"
    name = v2["journey_buffs"][0]["name"]["ko-KR"]
    idx = jh_db.InfoIndex({}, "ko", v2=v2)
    assert idx.by_name[name]["desc"] == "V2 설명"
    assert jh_db.InfoIndex({}, "ko").by_name.get(name, {}).get("desc") != "V2 설명"


def test_garbage_v2_never_breaks_build():
    j, a = guide()
    base = jh_db.build_event_cache(j, a)
    for bad in ({"journeys": {"x": "notalist"}}, {"journeys": {"x": [{"choices": 5}]}, "journey_items": [1, None]},
                {"journeys": {"y": [None]}}, {"journeys": []}, {}):
        assert len(jh_db.build_event_cache(j, a, v2=bad)) == len(base)


def test_trial_titles_are_not_supplemented():
    j, a = guide()
    full = jh_db.build_event_cache(j, a, v2=load_v2())
    assert not any(jh_db.IGNORED_TITLE_WORD in e["cleanTitle"] and e.get("v2") for e in full)
    assert jh_db.match_title(["기초평가전승리"], full) == ([], [])


# --- 갱신 ---
def test_store_update_load_and_304(tmp_path):
    st = jh_v2.V2Store(str(tmp_path), session=Sess(v2_ok), clock=lambda: 1000.0)
    r = st.update()
    assert r.status == "updated" and st.load()["journeys"]
    st2 = jh_v2.V2Store(str(tmp_path), session=Sess(v2_ok), clock=lambda: 1000.0 + 13 * 3600)
    assert st2.update().status == "not_modified"


def test_store_html_response_keeps_cache(tmp_path):
    st = jh_v2.V2Store(str(tmp_path), session=Sess(v2_ok), clock=lambda: 1000.0)
    st.update()
    before = st.load()
    html = Sess(lambda u, h: Resp(200, b"<!doctype html><html></html>"))
    st2 = jh_v2.V2Store(str(tmp_path), session=html, clock=lambda: 1000.0 + 13 * 3600)
    r = st2.update()
    assert r.status == "offline" and not r.changed
    assert st2.load() == before


def test_store_structure_change_and_shrink_keep_cache(tmp_path):
    st = jh_v2.V2Store(str(tmp_path), session=Sess(v2_ok), clock=lambda: 1000.0)
    st.update()
    before = st.load()
    big = {f"t{i}": [{"choices": []}] for i in range(10)}
    st.session.handler = lambda u, h: Resp(200, json.dumps(big).encode()) if u.endswith("/journeys.json") else v2_ok(u, h)
    st.update(manual=True)
    assert len(st.load()["journeys"]) == 10                     # 정상 확대는 반영
    st.session.handler = lambda u, h: Resp(200, json.dumps({"a": [{"choices": []}]}).encode()) if u.endswith("/journeys.json") else v2_ok(u, h)
    r = st.update(manual=True)
    assert r.status == "offline" and len(st.load()["journeys"]) == 10
    st.session.handler = lambda u, h: Resp(200, json.dumps({"a": 5}).encode()) if u.endswith("/journeys.json") else v2_ok(u, h)
    st.update(manual=True)
    assert len(st.load()["journeys"]) == 10 and before["journey_items"] == st.load()["journey_items"]


def test_store_network_error_and_backoff(tmp_path):
    def boom(u, h):
        raise ConnectionError("down")
    st = jh_v2.V2Store(str(tmp_path), session=Sess(boom), clock=lambda: 5000.0)
    assert st.update().status == "offline"
    assert st.load() is None
    assert st.update().status == "backoff"


def test_dbstore_wrappers_never_raise(tmp_path):
    s = jh_db.DbStore(str(tmp_path), session=Sess(lambda u, h: Resp(404)))
    assert s.load_v2() is None
    assert s.update_v2().status == "offline"
