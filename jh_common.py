"""jh_db / jh_schedule / jh_v2 / jh_relic 이 함께 쓰는 보상 줄 정규식과 툴팁 HTML 조각.

같은 규칙이 여러 파일에 복사돼 한쪽만 고치는 일을 막기 위해 한 곳에 둔다.
"""
import re

# ----------------------------------------------------------------------------
# 보상 줄 정규식
# ----------------------------------------------------------------------------
TAG_PREFIX = re.compile(r"^\[(SUCCESS|FAIL)\]\s*")                    # '[SUCCESS] ' / '[FAIL] ' 결과 태그
OR_SPLIT = re.compile(r"\s+or(?:\s+|$)")                              # 줄 중간/끝의 택1 구분자
DIFF_WORD = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)\b")        # 줄 머리의 난이도 단어
DIFF_LINE = re.compile(r"^(이지|노말|하드|Easy|Normal|Hard)(?:\s+(.*))?$")   # 난이도 단어 + 나머지
MAIN_STAT = re.compile(r"^(힘|체력|인내|집중|보호)\s*[+-]")             # 주요 스탯 증감 줄


def strip_tag(line):
    """'[SUCCESS] 힘 +3' -> '힘 +3' (앞뒤 공백도 정리)"""
    return TAG_PREFIX.sub("", (line or "").strip())


# ----------------------------------------------------------------------------
# 툴팁 HTML 조각 (Qt 리치 텍스트용: 색과 글자 크기를 인라인 스타일로)
# ----------------------------------------------------------------------------
COL_GOLD = "#ffd27f"       # 제목/강조
COL_CYAN = "#9fe8ff"       # 항목 이름
COL_GREEN = "#a5d6a7"      # 보상/추천
COL_WHITE = "#ffffff"      # 본문
COL_GRAY = "#90a4ae"       # 부가 설명
COL_DIM = "#888888"        # 없음/생략


def span(text, color, size, bold=False):
    """글자색·크기를 지정한 <span>. text 는 이미 이스케이프된 HTML 이어야 한다."""
    body = f"<b>{text}</b>" if bold else text
    return f'<span style="color:{color}; font-size:{size}px;">{body}</span>'
