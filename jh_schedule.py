"""여정 일정(턴별 고정/확률/랜덤 이벤트) 보충 데이터와 조회 함수.

데이터는 옛 DB(star-savior-arcana-db) 백업(2026-10-15)의 schedules.json 에서 추출했다. 새 DB 에는 없는 정보라 앱에 내장한다.
각 항목: (turn, time, title, befores, currents, afters), 이벤트 = (지역, 발생형태 고정|확률|랜덤, 종류, [내용...]).
befores = 그 턴 훈련 전, currents = 그 턴(평가전), afters = 훈련 후.
"""
import html as _html
import re

SCHEDULE = [(0, '시작', '기초 평가전', [], [], [('NOA', '고정', '인자 계승', ['인자 계승'])]),
 (1,
  '3월 초순',
  '기초 평가전',
  [('NOA', '고정', '여정', ['기초 평가전 준비'])],
  [],
  [('NOA', '고정', '구원자', ['구원자 이벤트(버프/디버프)']),
   ('NOA', '확률', '아르카나', ['아르카나 이벤트']),
   ('NOA', '고정', '여정', ['(어려움) 훈련의 방향성'])]),
 (2,
  '3월 중순',
  '기초 평가전',
  [],
  [],
  [('NOA', '랜덤', '여정', ['여가 시간', '구원단 보급', '신비한 석상']), ('NOA', '고정', '여정', ['(어려움) 고대 구원자의 유물'])]),
 (3, '3월 하순', '기초 평가전', [], [], [('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (4, '4월 초순', '기초 평가전', [], [], [('NOA', '고정', '여정', ['여정의 선물']), ('NOA', '확률', '아르카나', ['미편성 아르카나 이벤트'])]),
 (5, '4월 중순', '기초 평가전', [], [], []),
 (6,
  '4월 하순',
  '기초 평가전',
  [],
  [('NOA', '고정', '토벌', ['(쉬움,보통) 약탈자 토벌 의뢰', '(어려움) 슬라임 토벌 의뢰'])],
  [('NOA', '고정', '여정', ['토벌의 전리품', '리세트의 일과']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (7, '평가전', '기초 평가전', [], [('NOA', '고정', '평가전', ['기초 평가전 승리'])], []),
 (8, '5월 초순', '토벌 평가전', [], [], [('NOA', '고정', '구원자', ['구원자 이벤트']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (9, '5월 중순', '토벌 평가전', [], [], [('NOA', '랜덤', '여정', ['공업소의 홍보 전략', '우연한 발견', '비밀의 지옥 특훈', '시작형 강화제', '정체불명의 상인'])]),
 (10,
  '5월 하순',
  '토벌 평가전',
  [],
  [],
  [('NOA', '랜덤', '여정', ['날씨', '예상 못 한 격려', '사고는 일어난다']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (11, '6월 초순', '토벌 평가전', [], [], [('NOA', '고정', '여정', ['친절의 선물']), ('NOA', '고정', '구원자', ['구원자 이벤트'])]),
 (12, '6월 중순', '토벌 평가전', [], [], [('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (13,
  '6월 하순',
  '토벌 평가전',
  [],
  [('NOA', '고정', '토벌', ['(쉬움,보통) 슬라임 토벌 의뢰', '(어려움) 남작 토벌 의뢰'])],
  [('NOA', '고정', '여정', ['토벌의 전리품', '리세트의 유혹']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (14, '평가전', '토벌 평가전', [], [('NOA', '고정', '평가전', ['토벌 평가전 승리'])], [('NOA', '고정', '인자 계승', ['동료 합류 & 인자 계승'])]),
 (15, '7월 초순', '원정 평가전', [], [], []),
 (16, '7월 중순', '원정 평가전', [('아가논', '고정', '여정', ['미궁 탐사'])], [], [('아가논', '고정', '여정', ['날씨'])]),
 (17, '7월 하순', '원정 평가전', [], [], []),
 (18, '8월 초순', '원정 평가전', [('아가논', '고정', '여정', ['습격'])], [], []),
 (19, '8월 중순', '원정 평가전', [], [], [('아가논', '고정', '여정', ['구명의 선물']), ('아가논', '확률', '여정', ['갑작스러운 조우 - 슬라임'])]),
 (20, '8월 하순', '원정 평가전', [], [], [('아가논', '랜덤', '여정', ['단원과의 식사', '단원과의 산책', '단원과의 대화', '단원과의 관람'])]),
 (21, '평가전', '원정 평가전', [], [('아가논', '고정', '평가전', ['원정 평가전 승리'])], []),
 (22, '9월 초순', '경쟁 평가전', [], [], [('NOA', '고정', '구원자', ['구원자 이벤트']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (23, '9월 중순', '경쟁 평가전', [], [], [('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (24,
  '9월 하순',
  '경쟁 평가전',
  [],
  [],
  [('NOA', '랜덤', '여정', ['리세트의 행운', '교단의 제안', '교단의 분실물', '시작형 강화제', '힘 특훈의 성과', '체력 특훈의 성과', '미래를 위한 선택']),
   ('NOA', '랜덤', '여정', ['날씨', '아르카나 이벤트'])]),
 (25, '10월 초순', '경쟁 평가전', [], [], [('NOA', '고정', '구원자', ['구원자 이벤트']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (26, '10월 중순', '경쟁 평가전', [], [], [('NOA', '고정', '여정', ['모범의 선물'])]),
 (27,
  '10월 하순',
  '경쟁 평가전',
  [],
  [('NOA', '고정', '토벌', ['(쉬움,보통) 탈영병 토벌 의뢰', '(어려움) 인형 토벌 의뢰'])],
  [('NOA', '고정', '여정', ['토벌의 전리품', '리세트의 휴가']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (28, '평가전', '경쟁 평가전', [], [('NOA', '고정', '평가전', ['경쟁 평가전 승리'])], [('NOA', '고정', '인자 계승', ['동료 합류 & 인자 계승'])]),
 (29, '11월 초순', '합숙훈련', [], [], [('플로라', '고정', '여정', ['비치발리볼 대회 예선전']), ('칼라이드', '고정', '여정', ['온천 축제 준비'])]),
 (30, '11월 중순', '합숙훈련', [], [], [('', '랜덤', '여정', ['날씨', '사고는 일어난다'])]),
 (31, '11월 하순', '합숙훈련', [], [], []),
 (32, '12월 초순', '합숙훈련', [('칼라이드', '고정', '여정', ['축제의 불청객'])], [], [('플로라', '고정', '여정', ['비치발리볼 대회 준결승전'])]),
 (33,
  '12월 중순',
  '합숙훈련',
  [],
  [],
  [('플로라', '고정', '여정', ['행복의 선물']),
   ('칼라이드', '고정', '여정', ['인연의 선물']),
   ('플로라', '확률', '여정', ['갑작스러운 조우 - 약탈자']),
   ('칼라이드', '확률', '여정', ['갑작스러운 조우 - 강도 기사'])]),
 (34, '12월 하순', '합숙훈련', [], [], [('', '랜덤', '여정', ['단원과의 식사', '단원과의 산책', '단원과의 대화', '단원과의 관람'])]),
 (35, '평가전', '합숙훈련', [], [('플로라', '고정', '평가전', ['비치발리볼 대회 결승전 승리']), ('칼라이드', '고정', '평가전', ['물길 개척단 호위 승리'])], []),
 (36, '1월 초순', '공허 평가전', [], [], [('NOA', '고정', '구원자', ['구원자 이벤트']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (37, '1월 중순', '공허 평가전', [], [], [('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (38,
  '1월 하순',
  '공허 평가전',
  [],
  [],
  [('NOA', '고정', '여정', ['리세트의 우울']),
   ('NOA', '랜덤', '여정', ['신설 부서는 노력 중!', '간단한 테스트', '리세트의 행운', '수집가가 온다', '칼에 숨겨진 힘', '꽃에 숨겨진 힘'])]),
 (39, '평가전', '공허 평가전', [], [('NOA', '고정', '평가전', ['공허 평가전 승리'])], []),
 (40, '2월 초순', '최종 평가전', [], [], [('NOA', '고정', '구원자', ['구원자 이벤트(스태미나)']), ('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (41, '2월 중순', '최종 평가전', [], [], [('NOA', '확률', '아르카나', ['아르카나 이벤트'])]),
 (42, '2월 하순', '최종 평가전', [], [], [('NOA', '고정', '여정', ['(어려움) 대마녀의 부름', '리세트의 친구'])]),
 (43, '평가전', '최종 평가전', [], [('NOA', '고정', '평가전', ['최종 평가전 승리'])], [('NOA', '고정', '구원자', ['별빛의 인도'])])]



_DATE_RE = re.compile(r"(1[0-2]|[1-9])\s*월\s*([초중하])")      # 앞에 잡음 숫자가 붙어도(예: 97월 -> 7월) 월 숫자만 잡는다
_PHASE_WORD = {"초": "초순", "중": "중순", "하": "하순"}
_BY_TIME = {row[1]: row for row in SCHEDULE}
TOTAL_TURNS = max(row[0] for row in SCHEDULE)
OCCUR_STYLE = {"고정": ("#7CFC9A", "고정"), "확률": ("#ffd27f", "확률"), "랜덤": ("#b0bec5", "랜덤")}


def parse_date(text):
    """OCR 문자열('3월초순' 등) -> '3월 초순' 표준형. 읽을 수 없으면 None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    return f"{int(m.group(1))}월 {_PHASE_WORD[m.group(2)]}"


def find_turn(text):
    """OCR 문자열 -> 일정 항목(tuple) 또는 None. 날짜(월/순)는 턴마다 한 번씩만 나온다."""
    d = parse_date(text)
    return _BY_TIME.get(d) if d else None


def _events(lst):
    return [(r, o, t, c) for r, o, t, c in lst]


def _next_turn_to_goal(row):
    """이번 구간의 평가전 턴(time == '평가전' 이면서 같은 title)까지 남은 턴 수. 없으면 None."""
    for r in SCHEDULE:
        if r[0] > row[0] and r[1] == "평가전" and r[2] == row[2]:
            return r[0] - row[0]
    return None


def upcoming(row, limit=3):
    """이번 턴 이후 '고정' 이벤트가 있는 턴 최대 limit 개 -> [(남은 턴 수, 항목)]"""
    out = []
    for r in SCHEDULE:
        if r[0] <= row[0]:
            continue
        if any(e[1] == "고정" for e in r[3] + r[4] + r[5]):
            out.append((r[0] - row[0], r))
            if len(out) >= limit:
                break
    return out


_COST_RE = re.compile(r"(사용\s*\d|필요$|소모$|제거$|^or$|^없음$|^(이지|노말|하드|쉬움|보통|어려움)\s)", re.I)
_DIFF_HEAD_RE = re.compile(r"(이지|노말|하드|Easy|Normal|Hard)(\s+\D*)?")      # 난이도 단어만 있거나 숫자 없는 난이도 머리 줄
_OR_RE = re.compile(r"\s+or(?:\s+|$)")                                         # 줄 중간/끝의 택1 구분자
_STAT_RE = re.compile(r"^(힘|체력|인내|집중|보호|스태미나|컨디션|오래된 (동전|주화))\s*[+-]")
_PROMO_PREFIX = re.compile(r"^\([^)]*\)\s*")
MAX_KEY_REWARDS = 3
STAR_TRAIL_PREFIX = "별빛의 궤적-"                     # 특수 잠재력(AX1호~XX…) 지급 줄: 호수마다 한 줄씩 수백 줄이라 요약한다
STAR_TRAIL_SUMMARY = "별빛의 궤적 특수 잠재력 지급(AX~XX, 호수 랜덤)"


_IDX = (None, -1, {})        # (리스트 자체, 길이, 색인) 마지막 한 번만 기억 (id 만 기억하면 해제된 옛 캐시 주소를 재사용할 수 있다)


def _event_index(events):
    """이벤트 캐시 -> {cleanTitle: event}. 같은 리스트면 다시 만들지 않는다."""
    global _IDX
    if not events:
        return {}
    if _IDX[0] is not events or _IDX[1] != len(events):
        _IDX = (events, len(events), {e.get("cleanTitle"): e for e in events if not e.get("isArcana")})
    return _IDX[2]


def _clean_reward_line(line):
    """보상 한 줄 -> 표시할 문구 또는 None(비용/요구/난이도 머리/스탯/숨김 아이템/빈 줄). 'A or B' 택1 은 'A 등' 으로 줄인다."""
    line = re.sub(r"^\[(SUCCESS|FAIL)\]\s*", "", line.strip())
    if not line or _DIFF_HEAD_RE.fullmatch(line) and not re.search(r"\d", line):        # '이지', '이지 토벌 평가전' 같은 난이도 머리 줄
        return None
    line = re.sub(r"^(이지|노말|하드|Easy|Normal|Hard)\s+", "", line)
    chained = bool(_OR_RE.search(line))
    line = _OR_RE.split(line)[0].strip() if chained else line
    if not line or _COST_RE.search(line) or _STAT_RE.match(line) or line.startswith("스탯 인자"):
        return None
    return line + " 등" if chained else line


def key_rewards(event, limit=MAX_KEY_REWARDS):
    """이벤트의 선택지 보상 중 '주요 보상'만 요약: 비용/요구/난이도/스탯 줄은 빼고 아이템·버프·유물을 앞에, 잠재력 포인트를 뒤에."""
    keys, poten, star = [], [], False
    for o in event.get("options") or []:
        for v in (o.get("variants") or [o])[:1]:
            for raw in (v.get("reward") or "").split("\n"):
                line = _clean_reward_line(raw)
                if not line:
                    continue
                if line.startswith(STAR_TRAIL_PREFIX):              # '대마녀의 부름': 호수별 200줄을 한 문구로
                    star = True
                elif line.startswith("잠재력 포인트"):
                    if line not in poten:
                        poten.append(line)
                elif line not in keys:
                    keys.append(line)
    if star:
        keys.insert(0, STAR_TRAIL_SUMMARY)
    return (keys + poten[:1])[:limit]


# 일정 이름 -> DB 에서는 '<접두> - <종류>' 로 종류별 이벤트로 나뉜 것 (그 시기에 나올 수 있는 것 중 랜덤)
GROUP_PREFIX = {"날씨": "오늘의 날씨"}
_MAIN_STAT_RE = re.compile(r"^(힘|체력|인내|집중|보호)\s*[+-]")


def _group_summary(prefix, events, time_label):
    """'오늘의 날씨 - ○○' 같은 종류별 이벤트 -> ['돌풍 힘', '쾌청 잠재력 포인트 +30', ...] (그 시기에 나오는 것만, 없으면 전부)"""
    evs = [e for e in events if not e.get("isArcana") and (e.get("rawTitle") or "").startswith(prefix + " - ")]
    pat = re.compile(r"(?<!\d)" + re.escape(time_label))
    now = [e for e in evs if any(pat.search(s or "") for s in e.get("sources") or [])]
    out = []
    for e in now or evs:
        name = e["rawTitle"].split(" - ", 1)[1]
        kr = key_rewards(e, 1)
        if not kr:                                           # 보상이 스탯뿐이면 올라가는 주요 스탯 이름만
            stats = []
            for o in e.get("options") or []:
                for line in ((o.get("variants") or [o])[0].get("reward") or "").split("\n"):
                    m = _MAIN_STAT_RE.match(re.sub(r"^\[(SUCCESS|FAIL)\]\s*", "", line.strip()))
                    if m and m.group(1) not in stats:
                        stats.append(m.group(1))
            kr = ["/".join(stats[:2])] if stats else []
        short = kr[0].replace("잠재력 포인트 ", "잠재력 ").replace("여정 버프 획득 ", "").replace("여정 버프 ", "")
        out.append(f"{name} {short}".strip())
    return out


def _choice_dependent(event):
    """보상이 있는 선택지가 둘 이상이면 어떤 선택을 하느냐에 따라 보상이 갈린다."""
    return sum(1 for o in event.get("options") or [] if (o.get("reward") or "").strip() not in ("", "없음")) >= 2


def _reward_line(contents, events, size, time_label="", named=False):
    """고정 이벤트 항목 아래에 붙일 '주요 보상' 줄 HTML 목록"""
    idx = _event_index(events)
    out = []
    if not idx:
        return out
    import jh_db
    span = lambda t, col: f'<span style="color:{col}; font-size:{size}px;">{_html.escape(t)}</span>'
    for c in contents:
        name = _PROMO_PREFIX.sub("", c)
        if "평가전" in name:                                         # 평가전은 앱이 툴팁을 띄우지 않는 이벤트라 보상 요약도 하지 않는다
            continue
        who = f"{name}: " if (len(contents) > 1 or named) else ""      # 한 줄에 이벤트가 여럿이면 이름을 붙인다
        ev = idx.get(jh_db.clean_title(name))
        if ev:
            kr = key_rewards(ev)
            if kr:
                dep = "(선택에 따라)" if _choice_dependent(ev) else ""
                label = f"{name}{dep}: " if who else f"주요 보상{dep}: "
                out.append(span(f"  └ {label}", "#a5d6a7") + span(" · ".join(kr), "#e8f5e9"))
        elif name in GROUP_PREFIX:
            sm = _group_summary(GROUP_PREFIX[name], events, time_label)
            if sm:
                out.append(span(f"  └ {who}랜덤 {len(sm)}종 - ", "#a5d6a7") + span(" · ".join(sm), "#e8f5e9"))
    return out


_DELTA_RE = re.compile(r"(힘|체력|인내|집중|보호|스태미나|컨디션|잠재력 포인트)\s*([+-]\d+)(?:~([+-]?\d+))?(?!\d|\s*턴)")
_DELTA_ORDER = ("힘", "체력", "인내", "집중", "보호", "스태미나", "컨디션", "잠재력 포인트")
MAX_EFFECT_TOKENS = 5


def effect_summary(event):
    """이벤트 보상 전체(모든 선택지·or 갈래)에서 스탯/스태미나/컨디션/잠재력 증감 범위를 모은다: ['힘 +10~+20', '체력 -20~+20', ...]
    한 줄이 'A +10 or B +10' 처럼 택1이면 '힘/체력 +10 중 1' 로 묶는다. 아이템·버프 같은 주요 보상은 앞에 붙인다(최대 2개)."""
    lo, hi, groups = {}, {}, []
    for o in event.get("options") or []:
        for v in (o.get("variants") or [o])[:1]:
            for line in (v.get("reward") or "").split("\n"):
                if _COST_RE.search(line.strip()) or (_DIFF_HEAD_RE.fullmatch(line.strip()) and not re.search(r"\d", line)):
                    continue
                found = [(m.group(1), [int(m.group(2))] + ([int(m.group(3))] if m.group(3) else [])) for m in _DELTA_RE.finditer(line)]
                if not found:
                    continue
                names = list(dict.fromkeys(k for k, _ in found))
                if " or " in line and len(names) > 1:                      # 택1 묶음
                    per = {}
                    for k, vs in found:
                        a0, b0 = per.get(k, (10 ** 6, -10 ** 6))
                        per[k] = (min(a0, min(vs)), max(b0, max(vs)))
                    g = tuple((k, a0, b0) for k, (a0, b0) in per.items())
                    if g not in groups:
                        groups.append(g)
                    continue
                for k, vs in found:
                    lo[k] = min(lo.get(k, 10 ** 6), min(vs))
                    hi[k] = max(hi.get(k, -10 ** 6), max(vs))
    tokens = []
    for k in _DELTA_ORDER:
        if k in lo:
            name = "잠재력" if k == "잠재력 포인트" else k
            tokens.append(f"{name} {lo[k]:+d}" if lo[k] == hi[k] else f"{name} {lo[k]:+d}~{hi[k]:+d}")
    short = lambda n: "잠재력" if n == "잠재력 포인트" else n
    rng = lambda a, b: f"{a:+d}" + (f"~{b:+d}" if a != b else "")
    for g in groups:
        if len({(a, b) for _, a, b in g}) == 1:                              # 값이 모두 같으면 '힘/체력/인내 +10 중 1'
            tokens.append("/".join(short(k) for k, _, _ in g) + f" {rng(g[0][1], g[0][2])} 중 1")
        else:                                                                # 다르면 항목별로 '컨디션 +1 / 잠재력 +15 중 1'
            tokens.append(" / ".join(f"{short(k)} {rng(a, b)}" for k, a, b in g) + " 중 1")
    items = []
    for x in key_rewards(event, 3):
        if x.startswith("잠재력 포인트"):
            continue
        items.append(x)                                                        # 택1 문구는 key_rewards 가 이미 '앞부분 등' 으로 줄였다
    return items[:2] + tokens[:MAX_EFFECT_TOKENS]


def _random_block(ev, events, px, small):
    """[랜덤] 이벤트: 머리줄 + 이벤트마다 한 줄(이름 / 예상 증감 요약)을 표로 정렬"""
    region, occur, typ, contents = ev
    color, label = OCCUR_STYLE.get(occur, ("#b0bec5", occur))
    where = "" if region in ("", "NOA") else f"[{_html.escape(region)}] "
    head = (f'<span style="color:{color}; font-size:{px}px;">[{label}]</span> '
            f'<span style="color:#ffffff; font-size:{px}px;">{where}<b>아래 중 하나</b></span> '
            f'<span style="color:#90a4ae; font-size:{px}px;">({_html.escape(typ)})</span>')
    idx = _event_index(events)
    trs = []
    for c in contents:
        import jh_db
        e = idx.get(jh_db.clean_title(_PROMO_PREFIX.sub("", c))) if idx else None
        eff = " · ".join(effect_summary(e)) if e else ""
        trs.append(f'<tr><td valign="top"><span style="color:#ffffff; font-size:{small}px;">• {_html.escape(c)}</span></td>'
                   f'<td valign="top">' + (f'<span style="color:#a5d6a7; font-size:{small}px;">&nbsp;&nbsp;{_html.escape(eff)}</span>' if eff else "") + '</td></tr>')
    return head + '<table cellspacing="0" cellpadding="1">' + "".join(trs) + "</table>"


def _line(ev, size):
    region, occur, typ, contents = ev
    color, label = OCCUR_STYLE.get(occur, ("#b0bec5", occur))
    where = "" if region in ("", "NOA") else f"[{_html.escape(region)}] "
    text = " / ".join(_html.escape(c) for c in contents)
    return (f'<span style="color:{color}; font-size:{size}px;">[{label}]</span> '
            f'<span style="color:#ffffff; font-size:{size}px;">{where}<b>{text}</b></span> '
            f'<span style="color:#90a4ae; font-size:{size}px;">({_html.escape(typ)})</span>')


def format_schedule_html(row, scale=1.0, events=None):
    """현재 턴의 일정 툴팁 HTML. events(이벤트 캐시)가 있으면 고정 이벤트 아래에 주요 보상을 붙인다."""
    esc = _html.escape
    px = max(10, round(13 * scale))
    small = max(9, round(11 * scale))
    turn, time_label, title, befores, currents, afters = row
    goal = _next_turn_to_goal(row)
    head = f'📅 <b>{esc(time_label)}</b> <span style="color:#ffcc00; font-size:{small}px;">[베타]</span>'
    sub = f'{esc(title)} · 턴 {turn}/{TOTAL_TURNS}' + (f' · 평가전까지 {goal}턴' if goal else '')
    parts = [head + f'<br><span style="color:#90a4ae; font-size:{small}px;">{sub}</span>']
    for label, evs in (("이번 턴 훈련 전", befores), ("이번 턴", currents), ("이번 턴 훈련 후", afters)):
        if evs:
            rows = []
            for e in evs:
                if e[1] == "랜덤" and len(e[3]) > 1:
                    rows.append(_random_block(e, events, px, small))
                    continue
                rows.append(_line(e, px))
                if e[1] == "고정":
                    rows.extend(_reward_line(e[3], events, small, time_label))
            parts.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>{label}</b></span><br>' + "<br>".join(rows))
    if not (befores or currents or afters):
        parts.append(f'<span style="color:#888888; font-size:{small}px;">이번 턴에는 알려진 고정 일정이 없습니다</span>')
    nxt = upcoming(row)
    if nxt:
        rows = []
        for dist, r in nxt:
            fixed = [e for e in r[3] + r[4] + r[5] if e[1] == "고정"]
            names = " · ".join(esc(c) for e in fixed for c in e[3])
            rows.append(f'<span style="color:#ffd27f; font-size:{small}px;">{dist}턴 뒤 ({esc(r[1])})</span> '
                        f'<span style="color:#ffffff; font-size:{small}px;">{names}</span>')
            for e in fixed:
                rows.extend(_reward_line(e[3], events, small, r[1], named=True))
        parts.append(f'<span style="color:#9fe8ff; font-size:{small}px;"><b>다음 고정 일정</b></span><br>' + "<br>".join(rows))
    note = "일정 출처: 옛 DB(턴 기준). 훈련 횟수와는 다를 수 있음"
    if "└" in "".join(parts) or "color:#a5d6a7" in "".join(parts):                # 보상/증감 요약이 실제로 들어갔을 때만
        note += "<br>※ 주요 보상·증감은 이벤트 DB를 자동 요약한 참고용입니다. 선택지·확률에 따라 달라지고 일부는 생략되니 정확한 내용은 게임에서 확인하세요"
    parts.append(f'<span style="color:#78909c; font-size:{small}px;">{note}</span>')
    sep = '<hr style="border:none;border-top:1px solid #444444;margin:6px 0;">'
    return sep.join(parts)
