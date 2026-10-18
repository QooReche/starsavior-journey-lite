# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jh_db  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
FULL = os.environ.get("JH_SAMPLES")        # 전체 샘플 디렉터리(선택): journey_ko.json, arcanas.json


def _read(*p):
    with open(os.path.join(FIX, *p), "rb") as f:
        return f.read()


class FakeResp:
    def __init__(self, code, content=b"", headers=None):
        self.status_code, self.content, self.headers = code, content, headers or {}


class FakeSession:
    """URL별 응답을 돌려주고 요청 헤더를 기록한다."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        return self.handler(url, headers or {})


def ok_handler(url, headers):
    rel = url.split("/data/", 1)[1]
    etag = '"v1"'
    if headers.get("If-None-Match") == etag:
        return FakeResp(304)
    return FakeResp(200, _read(*rel.split("/")), {"ETag": etag, "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"})


class CacheBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.journey = json.loads(_read("journey", "ko", "journey.json"))
        cls.arcanas = json.loads(_read("arcanas", "arcanas.json"))
        cls.cache = jh_db.build_event_cache(cls.journey, cls.arcanas)
        cls.by = {e["cleanTitle"]: e for e in cls.cache}

    def test_structure(self):
        self.assertGreater(len(self.cache), 10)
        for e in self.cache:
            self.assertTrue({"rawTitle", "cleanTitle", "isArcana", "options"} <= set(e))
            for o in e["options"]:
                self.assertTrue({"choice", "reward", "has_condition"} <= set(o))

    def test_arcana_choices_and_info(self):
        e = self.by[jh_db.clean_title("서류 작업은 언제나 맑은 뒤 흐림")] if "서류작업은언제나맑은뒤흐림" in self.by else None
        if e is None:       # 픽스처에 없는 경우 다른 다중 선택지 이벤트 사용
            e = next(x for x in self.cache if x["isArcana"] and len(x["options"]) >= 2)
        self.assertGreaterEqual(len(e["options"]), 2)
        self.assertTrue(any(o["info"] for e2 in self.cache if e2["isArcana"] for o in e2["options"]))

    def test_potential_description_has_no_markup(self):
        for e in self.cache:
            for o in e["options"]:
                for inf in o["info"]:
                    self.assertNotIn("<", inf["desc"])

    def test_table_event_options(self):
        e = self.by[jh_db.clean_title("여가 시간")]
        self.assertEqual(len(e["options"]), 4)
        self.assertEqual(e["options"][0]["choice"], "한정 디저트를 먹으러 가자!")
        self.assertIn("오래된 동전 사용 50", e["options"][0]["reward"])
        self.assertTrue(e["options"][0]["has_condition"])
        self.assertFalse(e["isArcana"])

    def test_wrapped_or_line_joined(self):
        e = self.by[jh_db.clean_title("여가 시간")]
        self.assertIn("인내 +10 or 인내 +20", e["options"][2]["reward"])

    def test_table_event_info_from_names(self):
        e = self.by[jh_db.clean_title("슬라포칼립스 승리")]
        names = [i["name"] for i in e["options"][0]["info"]]
        self.assertIn("보호의 감각", names)

    def test_duplicate_titles_merge_with_variants(self):
        e = self.by.get(jh_db.clean_title("무엇이든 물어보세요"))
        self.assertIsNotNone(e)
        self.assertGreaterEqual(len(e["sources"]), 3)
        self.assertTrue(e["options"][0].get("variants"))

    def test_raw_fallback_arcana(self):          # journey.json 에 없고 arcanas.json 에만 있는 아르카나
        self.assertIn(jh_db.clean_title("인기 강좌의 비결"), self.by)

    def test_html(self):
        e = self.by[jh_db.clean_title("여가 시간")]
        h = jh_db.format_option_html(e["options"][0])
        self.assertIn("한정 디저트", h)
        self.assertIn("오래된 동전 사용 50", h)
        e2 = next(x for x in self.cache if x["isArcana"] and any(o["info"] for o in x["options"]))
        o2 = next(o for o in e2["options"] if o["info"])
        self.assertIn(o2["info"][0]["name"], jh_db.format_option_html(o2))

    def test_match(self):
        best, score = jh_db.find_best_event("여가시간", self.cache)
        self.assertEqual(best["rawTitle"], "여가 시간")
        best, score = jh_db.find_best_event("여가시깐", self.cache)
        self.assertIsNotNone(best)

    @unittest.skipUnless(FULL and os.path.isdir(FULL), "JH_SAMPLES 미설정")
    def test_full_samples(self):
        j = json.load(open(os.path.join(FULL, "journey_ko.json"), encoding="utf8"))
        a = json.load(open(os.path.join(FULL, "arcanas.json"), encoding="utf8"))
        c = jh_db.build_event_cache(j, a)
        self.assertGreater(len(c), 200)
        self.assertTrue(all(e["options"] for e in c))


class EnglishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        journey = json.loads(_read("journey", "en", "journey.json"))
        arcanas = json.loads(_read("arcanas", "arcanas.json"))
        cls.cache = jh_db.build_event_cache(journey, arcanas, lang="en")
        cls.by = {e["cleanTitle"]: e for e in cls.cache}

    def test_titles_and_options(self):
        e = self.by[jh_db.clean_title("Leisure Time")]
        self.assertEqual(len(e["options"]), 4)
        self.assertIn("Use Old Coin 50", e["options"][0]["reward"])
        self.assertTrue(e["options"][0]["has_condition"])
        self.assertIn("Endurance +10 or Endurance +20", e["options"][2]["reward"])

    def test_english_info_and_prefix(self):
        txt = " ".join(o["reward"] for e in self.cache if e["isArcana"] for o in e["options"])
        self.assertIn("Journey Buff", txt)
        self.assertIn("Potential ", txt)
        infos = [i for e in self.cache for o in e["options"] for i in o["info"]]
        self.assertTrue(any(i["desc"] and not any("가" <= c <= "힣" for c in i["desc"]) for i in infos))

    def test_html_lang(self):
        e = self.by[jh_db.clean_title("Leisure Time")]
        self.assertIn("Use Old Coin 50", jh_db.format_option_html(e["options"][0], lang="en"))
        auto = {"choice": "", "reward": "Strength +3", "info": []}
        self.assertIn("Automatic", jh_db.format_option_html(auto, lang="en"))
        self.assertIn("자동 진행", jh_db.format_option_html(auto, lang="ko"))

    def test_matching_is_case_insensitive(self):
        best, _ = jh_db.find_best_event(jh_db.clean_title("leisure time"), self.cache)
        self.assertEqual(best["rawTitle"], "Leisure Time")

    def test_store_uses_language_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            s = FakeSession(ok_handler)
            st = jh_db.DbStore(d, session=s, lang="en")
            st.update()
            self.assertTrue(s.calls[0][0].endswith("journey/en/journey.json"))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1_000_000.0]
        self.clock = lambda: self.t[0]

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, handler=ok_handler):
        s = FakeSession(handler)
        return jh_db.DbStore(self.tmp.name, session=s, clock=self.clock), s

    def test_first_fetch_then_skip_within_12h(self):
        st, s = self.store()
        r = st.update()
        self.assertEqual((r.status, r.changed), ("updated", True))
        self.assertEqual(len(s.calls), 2)
        self.assertIn("JourneyNavigator", s.calls[0][1]["User-Agent"])
        self.assertNotIn("If-None-Match", s.calls[0][1])
        self.assertTrue(st.has_cache())
        self.t[0] += 11 * 3600
        st2 = jh_db.DbStore(self.tmp.name, session=s, clock=self.clock)       # 재시작 시뮬레이션
        self.assertEqual(st2.update().status, "skipped_recent")
        self.assertEqual(len(s.calls), 2)

    def test_conditional_get_304_after_12h(self):
        st, s = self.store()
        st.update()
        self.t[0] += 13 * 3600
        r = st.update()
        self.assertEqual((r.status, r.changed), ("not_modified", False))
        self.assertEqual(len(s.calls), 4)
        self.assertEqual(s.calls[2][1]["If-None-Match"], '"v1"')
        self.assertIn("If-Modified-Since", s.calls[2][1])
        j, a = st.load()
        self.assertIsNotNone(j)
        self.assertIsNotNone(a)

    def test_200_with_identical_content_is_not_a_change(self):
        st, s = self.store()
        st.update()
        self.t[0] += 13 * 3600

        def new_etag(url, headers):
            r = ok_handler(url, {})                     # 조건부 헤더 무시하고 200, ETag 만 다름
            r.headers = {"ETag": '"v2"'}
            return r
        st.session.handler = new_etag
        r = st.update()
        self.assertEqual((r.status, r.changed), ("not_modified", False))
        self.assertEqual(st.meta["files"]["journey/ko/journey.json"]["etag"], '"v2"')

    def test_manual_cooldown(self):
        st, s = self.store()
        st.update()
        self.assertEqual(st.update(manual=True).status, "not_modified")
        n = len(s.calls)
        self.t[0] += 300
        self.assertEqual(st.update(manual=True).status, "cooldown")
        self.assertEqual(len(s.calls), n)
        self.t[0] += 301
        self.assertEqual(st.update(manual=True).status, "not_modified")

    def test_offline_uses_cache_silently(self):
        st, s = self.store()
        st.update()
        self.t[0] += 13 * 3600

        def boom(url, headers):
            raise ConnectionError("offline")
        st.session.handler = boom
        r = st.update()
        self.assertEqual(r.status, "offline")
        self.assertIsNotNone(st.load()[0])
        n = len(s.calls)
        self.assertEqual(st.update().status, "backoff")      # 실패 직후엔 두들기지 않음
        self.assertEqual(len(s.calls), n)

    def test_bad_payload_keeps_old_cache(self):
        st, s = self.store()
        st.update()
        before = st.load()[0]
        self.t[0] += 13 * 3600
        st.session.handler = lambda url, h: FakeResp(200, b"<html>oops", {})
        self.assertEqual(st.update().status, "offline")
        self.assertEqual(st.load()[0], before)

    def test_http_error(self):
        st, s = self.store(lambda u, h: FakeResp(503))
        self.assertEqual(st.update().status, "offline")
        self.assertFalse(st.has_cache())


class ItemLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.journey = json.loads(_read("items_events.json"))
        cls.cache = jh_db.build_event_cache(cls.journey, None)
        cls.by = {e["rawTitle"]: e for e in cls.cache}

    @staticmethod
    def _plain(h):
        import re
        return re.sub(r"<[^>]+>", "\n", h)

    def test_roles(self):
        g = self.by["우연한 발견"]["options"][0]
        self.assertIn({"name": "수상한 상자", "role": "grant"}, g["items"])
        c = self.by["교단의 분실물"]["options"][0]
        self.assertIn({"name": "수상한 상자", "role": "consume"}, c["items"])
        self.assertEqual(self.by["교단의 분실물"]["timing"], "9월 하순")
        self.assertEqual(self.by["우연한 발견"]["options"][1]["items"], [])

    def test_grant_section_lists_later_use(self):
        h = jh_db.format_option_html(self.by["우연한 발견"]["options"][0])
        t = self._plain(h)
        self.assertIn("이후 사용처", t)
        self.assertIn("교단의 분실물", t)
        self.assertIn("9월 하순", t)
        self.assertIn("어서 열어보자", t)
        self.assertIn("힘+10", t)
        self.assertNotIn("이후 사용처", jh_db.format_option_html(self.by["우연한 발견"]["options"][0], show_info=False))
        self.assertNotIn("이후 사용처", jh_db.format_option_html(self.by["우연한 발견"]["options"][1]))

    def test_consume_shows_source(self):
        h = self._plain(jh_db.format_option_html(self.by["교단의 분실물"]["options"][0]))
        self.assertIn("획득처", h)
        self.assertIn("우연한 발견", h)
        self.assertIn("5월 중순", h)

    def test_buff_rows_are_not_items(self):
        idx = jh_db.build_item_index(self.cache)
        self.assertFalse([k for k in idx if "여정 버프" in k or "턴" in k])

    def test_no_later_use_and_limit(self):
        o = {"choice": "x", "reward": "a", "item_use": [{"item": "Z", "uses": []}]}
        self.assertNotIn("확인 안 됨", jh_db.format_option_html(o))       # 사용처 없는 아이템은 숨김
        self.assertNotIn("이후 사용처", jh_db.format_option_html(o))
        one = {"event": "E", "timing": "1월 초순", "choice": "c", "result": "r"}
        h = jh_db.format_option_html({"choice": "x", "reward": "a", "item_use": [
            {"item": "스탯 인자 힘 상승 Ⅱ - 힘 15", "uses": [one]}, {"item": "Q", "uses": [one]}]})
        self.assertNotIn("스탯 인자", h)                                  # 스탯 인자 숨김
        self.assertIn(">Q<", h)
        uses = [{"event": f"E{i}", "timing": "1월 초순", "choice": "c", "result": "r"} for i in range(5)]
        t = self._plain(jh_db.format_option_html({"choice": "x", "reward": "a", "item_use": [{"item": "Z", "uses": uses}]}))
        self.assertIn("E2", t)
        self.assertNotIn("E3", t)
        self.assertIn("외 2건", t)
        en = jh_db.format_option_html({"choice": "x", "reward": "a", "item_use": [{"item": "Z", "uses": uses}]}, lang="en")
        self.assertIn("+2 more", en)

    def test_robust_to_bad_rows(self):
        self.assertEqual(jh_db.row_items({}), [])
        self.assertEqual(jh_db.row_items({"text": "노말 힘 300 필요"}), [])
        jh_db.link_items(None)
        jh_db.link_items([{"options": [{"items": [{"name": "A"}]}]}])

if __name__ == "__main__":
    unittest.main()



def test_buff_line_merged_and_described():
    """'여정 버프 획득' 줄은 괄호로 합치고, 새 DB에 없는 버프(힘의 자양분)도 옛 DB 보충 설명이 붙는다."""
    assert jh_db._compact_result(["힘 +20", "여정 버프 획득 힘의 자양분 +3턴"]) == "힘+20\n여정 버프 획득 (힘의 자양분+3턴)"
    assert jh_db._compact_result(["여정 버프 획득", "힘의 자양분 +3턴"]) == "여정 버프 획득 (힘의 자양분+3턴)"
    inf = jh_db.InfoIndex({}, "ko").find_in_text("여정 버프 획득 힘의 자양분 +3턴")
    assert inf and inf[0]["name"] == "힘의 자양분" and "계승" in inf[0]["desc"]


def test_table_event_success_failure_tones_become_blocks():
    """표 형태 이벤트에서 한 선택지에 성공/실패 톤 행이 함께 있으면 [SUCCESS]/[FAIL] 블록으로 나뉜다(오늘의 날씨-안개)."""
    def row(text, region, tone):
        return {"text": text, "region": region, "tone": tone, "fontSize": 13.6, "weight": 400, "segments": []}
    ev = {"title": "T", "rows": [
        {"text": "T", "region": "", "tone": "plain", "fontSize": 16, "weight": 700, "segments": []},
        {"text": "1. 이어간다.", "region": "", "tone": "plain", "fontSize": 14.4, "weight": 700, "segments": []},
        row("집중 +30", "r5", "success"), row("집중 -10", "r6", "failure"),
        {"text": "2. 돌아간다.", "region": "", "tone": "plain", "fontSize": 14.4, "weight": 700, "segments": []},
        row("스태미나 +10", "r7", "success")]}
    _, opts = jh_db.parse_table_event(ev, jh_db.InfoIndex({}, "ko"))
    assert opts[0]["reward"] == "[SUCCESS] 집중 +30\n[FAIL] 집중 -10"
    assert opts[1]["reward"] == "스태미나 +10"


def test_difficulty_header_tone_is_not_failure():
    """'하드 인내 175 필요' 같은 난이도 머리 줄의 빨간(failure) 톤을 실패 결과로 오해하지 않는다(습격)."""
    def row(text, region, tone):
        return {"text": text, "region": region, "tone": tone, "fontSize": 13.6, "weight": 400, "segments": []}
    ev = {"title": "T", "rows": [
        {"text": "T", "region": "", "tone": "plain", "fontSize": 16, "weight": 700, "segments": []},
        {"text": "1. 굳히자.", "region": "", "tone": "plain", "fontSize": 14.4, "weight": 700, "segments": []},
        row("이지 인내 125 필요", "r11", "success"), row("집중 +10", "r13", "success"),
        {"text": "or", "region": "", "tone": "plain", "fontSize": 14.4, "weight": 400, "segments": []},
        row("하드 인내 175 필요", "r17", "failure"), row("집중 +10", "r19", "success")]}
    _, opts = jh_db.parse_table_event(ev, jh_db.InfoIndex({}, "ko"))
    assert "[FAIL]" not in opts[0]["reward"] and "[SUCCESS]" not in opts[0]["reward"]


def test_shrunk_or_restructured_response_does_not_overwrite_cache(tmp_path):
    """새 DB 사이트가 형식을 바꾸거나 내용이 절반 미만으로 줄어든 응답을 주면 정상 캐시를 유지한다."""
    st = jh_db.DbStore(str(tmp_path), session=FakeSession(ok_handler))
    assert st.update(manual=False).status == "updated"
    good = open(st._file_path("journey/ko/journey.json"), "rb").read()
    j = json.loads(good)
    j["events"] = j["events"][:2]                                    # 크게 줄어든 응답
    bad = json.dumps(j, ensure_ascii=False).encode("utf-8")

    def shrink(url, headers):
        if url.endswith("journey/ko/journey.json"):
            return FakeResp(200, bad, {"ETag": '"v2"'})
        return ok_handler(url, headers)
    st.clock = lambda: 10 ** 9
    st._session = FakeSession(shrink)
    res = st.update(manual=True)
    assert res.status == "offline" and "캐시 유지" in res.message
    assert open(st._file_path("journey/ko/journey.json"), "rb").read() == good
    j2 = json.loads(good); j2.pop("events")                          # 구조가 바뀐 응답
    st._session = FakeSession(lambda u, h: FakeResp(200, json.dumps(j2).encode(), {}) if u.endswith("journey/ko/journey.json") else ok_handler(u, h))
    st.meta["last_manual"] = 0; st.meta["last_fail"] = 0
    assert st.update(manual=True).status == "offline"
