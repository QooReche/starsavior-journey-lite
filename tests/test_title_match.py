# -*- coding: utf-8 -*-
"""제목 OCR 후보 -> 이벤트 매칭 (순서를 보는 점수식) 테스트. 실제 OCR 테스트는 모델이 없으면 건너뛴다."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import jh_db  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "titles")
with open(os.path.join(FIX, "events.json"), encoding="utf-8") as _f:
    EVENTS = [{"rawTitle": e["raw"], "cleanTitle": e["clean"]} for e in json.load(_f)]
with open(os.path.join(FIX, "truth.json"), encoding="utf-8") as _f:
    TRUTH = json.load(_f)
IN_DB = {e["cleanTitle"] for e in EVENTS}


def load_gray(name):      # cv2.imread 는 한글 경로를 못 읽는다
    return cv2.imdecode(np.fromfile(os.path.join(FIX, name), np.uint8), cv2.IMREAD_GRAYSCALE)


def amb(queries):
    cands, a = jh_db.match_title(queries, EVENTS)
    return cands, [e["cleanTitle"] for e in a]


class ScorerTests(unittest.TestCase):
    def test_exact_is_confident(self):
        for t in ("오늘의날씨순풍", "오늘의날씨돌풍", "머리사냥", "전설의새를찾아서"):
            cands, a = amb([t])
            self.assertEqual(cands[0][0]["cleanTitle"], t)
            self.assertEqual(a, [t], t)

    def test_noisy_variants_top1(self):
        cases = {"짝생일파티:9": "깜짝생일파티", "리세트의일과주-": "리세트의일과", "전설의새를찾아서ㅣ": "전설의새를찾아서",
                 "ㅣ머리사냥": "머리사냥", "머리사냥1": "머리사냥", "머리사낭": "머리사냥"}
        for q, truth in cases.items():
            cands, a = amb([q])
            self.assertTrue(cands, q)
            self.assertEqual(cands[0][0]["cleanTitle"], truth, q)
            self.assertEqual(a, [truth], q)

    def test_weather_noise_keeps_truth_visible(self):
        sunpung = "오늘의날씨순풍"
        for q in (["오늘의날씨스", "오늘의날씨소풍", "오늘의날씨순풍"], ["오늘의날씨소풍"], ["오늘의날씨스"], ["오늘의날씨손풍"]):
            cands, a = amb(q)
            self.assertIn(sunpung, a, q)
            self.assertLessEqual(len(a), 3)
        # 여러 배율에서 한 번이라도 맞게 읽히면 확신
        self.assertEqual(amb(["오늘의날씨스", "오늘의날씨소풍", "오늘의날씨순풍"])[1], [sunpung])
        self.assertEqual(amb(["오늘의날씨손풍"])[1][0], sunpung)

    def test_weather_confusables_are_listed_not_guessed(self):
        # 앞부분만 읽힌 경우 하나로 찍지 않고 후보를 보여준다
        cands, a = amb(["오늘의날씨"])
        self.assertGreaterEqual(len(a), 2)

    def test_unknown_title_rejected(self):
        cands, a = amb(["전혀다른문장입니다"])
        self.assertEqual(a, [])

    def test_find_best_event_accepts_list_and_str(self):
        best, s = jh_db.find_best_event("오늘의날씨순풍", EVENTS)
        self.assertEqual(best["cleanTitle"], "오늘의날씨순풍")
        best, s = jh_db.find_best_event(["x", "머리사냥"], EVENTS)
        self.assertEqual(best["cleanTitle"], "머리사냥")
        self.assertEqual(jh_db.find_best_event("", EVENTS), (None, 0.0))


class LogTests(unittest.TestCase):
    def test_log_writes_rotates_and_survives_lock(self):
        import tempfile
        import journey_helper as jhm
        d = tempfile.mkdtemp()
        path = os.path.join(d, "log.txt")
        os.environ["JH_LOG_PATH"] = path
        try:
            jhm.log("가나다 테스트")
            raw = open(path, "rb").read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn("가나다 테스트", raw.decode("utf-8"))
            with open(path, "ab") as f:
                f.write(b"x" * (jhm.LOG_MAX_BYTES + 10))
            jhm.log("after")
            self.assertTrue(os.path.exists(path + ".1"))
            self.assertLess(os.path.getsize(path), 1000)
            os.environ["JH_LOG_PATH"] = d       # 디렉터리를 파일처럼 열면 실패 -> 예외 없이 무시
            jhm.log("무시됨")
        finally:
            os.environ.pop("JH_LOG_PATH", None)

class NotFoundAndKindTests(unittest.TestCase):
    def test_short_title_not_matched_inside_long_query(self):
        evs = EVENTS + [{"rawTitle": "운명", "cleanTitle": "운명", "isArcana": True}]
        for q in ("운명이이어준첫번째인연", "운명이이어준첫번째인연ㅣ"):
            cands, a = jh_db.match_title([q], evs)
            self.assertEqual(cands, [], q)
            self.assertEqual(a, [])
        self.assertEqual(jh_db.match_title(["운명"], evs)[1][0]["cleanTitle"], "운명")

    def test_kind_filter(self):
        evs = [{"rawTitle": "머리사냥", "cleanTitle": "머리사냥", "isArcana": True},
               {"rawTitle": "머리사냥 A", "cleanTitle": "머리사냥a", "isArcana": False}]
        self.assertEqual(jh_db.match_title(["머리사냥"], evs, "arcana")[1][0]["isArcana"], True)
        self.assertEqual(jh_db.match_title(["머리사냥a"], evs, "general")[1][0]["isArcana"], False)
        self.assertEqual(jh_db.match_title(["머리사냥"], [evs[0]], "general")[0], [])      # 종류가 다르면 후보 없음
        self.assertEqual(len(jh_db.match_title(["머리사냥"], evs, None)[0]), 2)

    def test_label_kind(self):
        import journey_helper as jhm
        self.assertEqual(jhm.label_kind("아르카나이벤트"), "arcana")
        self.assertEqual(jhm.label_kind("여정이벤트"), "general")
        self.assertEqual(jhm.label_kind("훈련이벤트"), "general")
        self.assertIsNone(jhm.label_kind(None))

    def test_not_found_note(self):
        for lang in ("ko", "en"):
            h = jh_db.format_not_found_html("운명<이>", 1.0, lang)
            self.assertIn("운명&lt;이&gt;", h)
            self.assertIn("📭", h)

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")


@unittest.skipUnless(os.path.exists(os.path.join(MODEL_DIR, "korean_PP-OCRv4_rec_mobile.onnx")), "OCR 모델 없음")
class RealOcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import journey_helper as jhm
        cls.jhm = jhm
        cls.ocr = jhm.KoreanOCR(MODEL_DIR)

    def test_crops(self):
        top1 = contained = total = 0
        rows = []
        for t in TRUTH:
            truth = jh_db.clean_title(t["truth"])
            if truth not in IN_DB:         # DB 에 없는 제목(예: 토벌 평가전 준비)은 정답 비교 대상이 아니다
                continue
            gray = load_gray(t["file"])
            texts = self.jhm.read_title_candidates(self.ocr, gray)
            cands, a = amb(texts)
            total += 1
            ok1 = bool(cands) and cands[0][0]["cleanTitle"] == truth
            okc = truth in a
            top1 += ok1
            contained += okc
            rows.append((t["truth"], texts, ok1, a))
        print("\n".join(str(r) for r in rows))
        self.assertEqual(total, 5)
        self.assertEqual(contained, total, rows)
        self.assertGreaterEqual(top1, total - 1, rows)
        sun = next(r for r in rows if "순풍" in r[0])
        self.assertTrue(sun[2] or "오늘의날씨순풍" in sun[3], sun)

    def test_unknown_title_is_not_confident_wrong(self):
        t = next(t for t in TRUTH if "토벌 평가전 준비" in t["truth"])
        gray = load_gray(t["file"])
        _, a = amb(self.jhm.read_title_candidates(self.ocr, gray))
        self.assertNotIn("토벌평가전승리", a[:1])


if __name__ == "__main__":
    unittest.main()


def test_label_ocr_variants_of_arcana():
    import journey_helper as jhm
    assert jhm.match_event_label("알카나이벤트") == "아르카나이벤트"
    assert jhm.match_event_label("아드가나이벤트") == "아르카나이벤트"
    assert jhm.match_event_label("아르가나 이벤트") == "아르카나이벤트"
    assert jhm.match_event_label("알카나 장비") is None            # '이벤트' 가 없으면 변형 처리 안 함
    assert jhm.match_event_label("이벤트") is None
