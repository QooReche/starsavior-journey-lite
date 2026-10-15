# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import journey_helper as jhm  # noqa: E402

MODEL = os.path.join(os.path.dirname(__file__), "..", "models", jhm.KoreanOCR.MODEL)


class LabelTests(unittest.TestCase):
    def test_full_phrases(self):
        for t in ("여정 이벤트", "훈련이벤트", "아르카나 이벤트", "제목 여정 이벤트"):
            self.assertIsNotNone(jhm.match_event_label(t), t)

    def test_fuzzy(self):
        self.assertIsNotNone(jhm.match_event_label("여정이멘트"))
        self.assertIsNotNone(jhm.match_event_label("아르카냐 이벤트"))

    def test_generic_words_rejected(self):
        for t in ("이벤트", "이벤트 보상", "장비", "능력치", "스킬 이벤트", "", "캐릭터 정보", "일일 이벤트"):
            self.assertIsNone(jhm.match_event_label(t), t)

    def test_english_labels_only_in_en_mode(self):
        self.assertIsNone(jhm.match_event_label("Journey Event"))
        self.assertIsNotNone(jhm.match_event_label("Journey Event", lang="en"))
        self.assertIsNotNone(jhm.match_event_label("Arcana  Event", lang="en"))
        self.assertIsNotNone(jhm.match_event_label("Trainlng Event", lang="en"))
        for t in ("Event", "Daily Event", "Equipment", "Stats", "Event Shop"):
            self.assertIsNone(jhm.match_event_label(t, lang="en"), t)

    def test_tr(self):
        jhm.set_language("en")
        try:
            self.assertEqual(jhm.tr("🔽 여정 시 펼쳐주세요"), "🔽 Expand during Journey")
            self.assertEqual(jhm.tr("번역 없는 문구"), "번역 없는 문구")
        finally:
            jhm.set_language("ko")
        self.assertEqual(jhm.tr("🔽 여정 시 펼쳐주세요"), "🔽 여정 시 펼쳐주세요")

    def test_defaults(self):
        self.assertEqual(jhm.DEFAULT_SETTINGS["startup_state"], "collapsed")
        self.assertEqual(jhm.DEFAULT_SETTINGS["language"], "ko")

    def test_clean_title(self):
        self.assertEqual(jhm.clean_ocr_title("제목 살롱 인 더 애프터눈!"), "살롱인더애프터눈")


@unittest.skipUnless(os.path.isfile(MODEL), "한국어 모델 없음")
class OcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image, ImageDraw, ImageFont
        cls.Image, cls.ImageDraw = Image, ImageDraw
        font = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "malgun.ttf")
        if not os.path.isfile(font):
            raise unittest.SkipTest("malgun.ttf 없음")
        cls.font = ImageFont.truetype(font, 24)
        cls.ocr = jhm.KoreanOCR()

    def render(self, text):
        im = self.Image.new("RGB", (460, 60), (60, 70, 100))
        self.ImageDraw.Draw(im).text((10, 14), text, font=self.font, fill=(250, 250, 250))
        return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2GRAY)

    def test_titles(self):
        for t in ("서류 작업은 언제나 맑은 뒤 흐림", "리세트의 일과", "슬라포칼립스 승리", "살롱 인 더 애프터눈"):
            text, conf = self.ocr.read_line(self.render(t))
            self.assertGreater(conf, 0.7, (t, text))
            self.assertGreaterEqual(
                __import__("difflib").SequenceMatcher(None, jhm.clean_ocr_title(text), t.replace(" ", "")).ratio(), 0.85, (t, text))

    def test_english_text(self):             # 한국어 rec 모델의 사전에는 영문자도 포함되어 있다
        text, conf = self.ocr.read_line(self.render("Journey Event"))
        self.assertEqual(jhm.match_event_label(text, lang="en"), "journeyevent", text)
        text, _ = self.ocr.read_line(self.render("Leisure Time"))
        self.assertIn("leisuretime", jhm.clean_ocr_title(text).lower(), text)

    def test_label(self):
        text, _ = self.ocr.read_line(self.render("아르카나 이벤트"))
        self.assertEqual(jhm.match_event_label(text), "아르카나이벤트")
        text, _ = self.ocr.read_line(self.render("장비 강화"))
        self.assertIsNone(jhm.match_event_label(text))


if __name__ == "__main__":
    unittest.main()
