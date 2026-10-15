import unittest
import jh_db


def ev(title):
    return {"cleanTitle": jh_db.clean_title(title), "isArcana": False}


class IgnoredTitles(unittest.TestCase):
    events = [ev("경쟁 평가전 승리"), ev("토벌 평가전 승리"), ev("대마녀의 부름")]

    def test_pyeonggajeon_never_matches(self):
        for q in (["원정평가전승리"], ["경쟁평가전승리"], "토평가전준비"):
            self.assertEqual(jh_db.match_title(q, self.events), ([], []))

    def test_other_titles_still_match(self):
        cands, amb = jh_db.match_title(["대마녀의부름"], self.events)
        self.assertEqual(cands[0][0]["cleanTitle"], "대마녀의부름")


if __name__ == "__main__":
    unittest.main()
