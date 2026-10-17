"""아르카나 카드 썸네일 식별 (lite).

게임 '아르카나 이벤트' 라벨 패널 좌측의 기울어진 카드 썸네일이 어떤 아르카나인지
후보(arcana id) 집합 안에서 판별한다.  순수 numpy/cv2/PIL 사용.

흐름
  1. crop_card_region(frame_bgr)      : 클라이언트 프레임에서 카드 영역을 비율로 자름
  2. identify(region_bgr, candidates) : 기울기(약 12도)를 고정 사각형으로 펴고
                                        Lab 격자 상관 + HSV 히스토그램으로 후보와 비교
  3. CardStore                        : 후보 카드 이미지를 지연 다운로드/디스크 캐시
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import urllib.request

import cv2
import numpy as np

import jh_net
import jh_paths
from jh_version import APP_VERSION

CARD_URL = "https://starsavior-guide.github.io/data/arcana-assets/cards/{id}.webp"
USER_AGENT = f"JourneyHelperLite/{APP_VERSION} (personal)"

# 카드 영역(프레임 대비 비율): x0, y0, x1, y1  (1616x939 클라이언트 기준 실측)
CARD_REGION_FRAC = (60 / 1610, 135 / 940, 190 / 1610, 295 / 940)

# 위 영역 안에서 카드 프레임 바깥 네 모서리 (TL, TR, BR, BL), 영역 크기 대비 비율.
# 영역 = 130x160px 기준: 샘플 3장 모두 동일 사각형으로 정렬됨(기울기 약 12.6도).
_REGION_REF = (130.0, 160.0)
_QUAD_PX = ((22, 29), (87.5, 14.5), (113.75, 137.5), (48.75, 151))
CARD_QUAD_FRAC = tuple((x / _REGION_REF[0], y / _REGION_REF[1]) for x, y in _QUAD_PX)

# 정규화 아트 크기 / 프레임 안쪽 여백(비율)
_W, _H = 52, 96
_INSET_X, _INSET_Y = 0.09, 0.06
# 위치 지터 탐색(정규화 영역 px) - UI 스케일/반올림 오차 흡수
_JITTER = (-2.0, 0.0, 2.0)

# 판정 규칙
MIN_SCORE = 0.50      # 1위 점수 하한
MIN_MARGIN = 0.08     # 2위와의 격차 하한 (후보 2개 이상일 때)
# 실기(2026-10-16): 정답이 뚜렷해도(격차 0.28) 점수가 0.37 로 나와 0.50 기준에 걸려 식별 실패 -> 격차가 크면 점수 하한을 낮춘다
STRONG_MARGIN = 0.25
MIN_SCORE_STRONG = 0.33
LAST_SCORES = {}      # 마지막 identify 의 후보별 점수 (로그/진단용)

FAIL_RETRY_SEC = 20.0  # 참조 이미지 다운로드가 실패한 id 는 이 시간 동안 다시 시도하지 않음
_REF_W = 176          # 캐시에 저장하는 참조 이미지 폭


# ---------------------------------------------------------------- 영역 자르기
def crop_card_region(frame_bgr, frac=CARD_REGION_FRAC):
    """게임 클라이언트 프레임(BGR)에서 카드 영역을 잘라 돌려준다."""
    h, w = frame_bgr.shape[:2]
    x0, y0 = int(round(frac[0] * w)), int(round(frac[1] * h))
    x1, y1 = int(round(frac[2] * w)), int(round(frac[3] * h))
    return frame_bgr[max(0, y0):y1, max(0, x0):x1]


# ---------------------------------------------------------------- 특징
def _descriptor(art_bgr):
    """정규화된 아트(_H x _W BGR) -> 특징 dict."""
    art = cv2.GaussianBlur(art_bgr, (3, 3), 0)
    lab = cv2.cvtColor(art, cv2.COLOR_BGR2LAB).astype(np.float32)
    grid = cv2.resize(lab, (_W // 2, _H // 2), interpolation=cv2.INTER_AREA)
    chans = []
    for c in range(3):
        v = grid[:, :, c].ravel()
        v = v - v.mean()
        n = np.linalg.norm(v)
        chans.append(v / n if n > 1e-6 else v)
    hsv = cv2.cvtColor(art, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [12, 6], [0, 180, 0, 256])
    hist = cv2.normalize(hist, None, 1.0, 0.0, cv2.NORM_L1).ravel()
    return {"lab": chans, "hist": hist}


def _similarity(a, b):
    lab = 0.6 * float(a["lab"][0] @ b["lab"][0]) + 0.2 * float(a["lab"][1] @ b["lab"][1]) \
        + 0.2 * float(a["lab"][2] @ b["lab"][2])
    hist = float(np.minimum(a["hist"], b["hist"]).sum())
    return 0.7 * lab + 0.3 * hist


def _reference_descriptor(ref_bgr):
    h, w = ref_bgr.shape[:2]
    # 원본 카드는 게임 내 카드보다 약간 넓게 보이므로 동일 비율 안쪽 여백만 사용
    x0, y0 = int(w * 0.03), int(h * 0.02)
    art = ref_bgr[y0:h - y0, x0:w - x0]
    return _descriptor(cv2.resize(art, (_W, _H), interpolation=cv2.INTER_AREA))


def _warp_art(region_bgr, dx=0.0, dy=0.0):
    """영역 이미지에서 카드 아트 부분을 (_H x _W)로 편다(기울기 보정)."""
    rw, rh = _REGION_REF
    reg = cv2.resize(region_bgr, (int(rw), int(rh)), interpolation=cv2.INTER_AREA
                     if region_bgr.shape[1] >= rw else cv2.INTER_CUBIC)
    q = np.float32([[x * rw + dx, y * rh + dy] for x, y in CARD_QUAD_FRAC])
    tl, tr, br, bl = q
    # 프레임 안쪽 여백만큼 안으로
    def lerp(p, r, t):
        return p + (r - p) * t
    top_l, top_r = lerp(tl, tr, _INSET_X), lerp(tl, tr, 1 - _INSET_X)
    bot_l, bot_r = lerp(bl, br, _INSET_X), lerp(bl, br, 1 - _INSET_X)
    src = np.float32([lerp(top_l, bot_l, _INSET_Y), lerp(top_r, bot_r, _INSET_Y),
                      lerp(top_r, bot_r, 1 - _INSET_Y), lerp(top_l, bot_l, 1 - _INSET_Y)])
    M = cv2.getPerspectiveTransform(src, np.float32([[0, 0], [_W, 0], [_W, _H], [0, _H]]))
    return cv2.warpPerspective(reg, M, (_W, _H), flags=cv2.INTER_AREA,
                               borderMode=cv2.BORDER_REPLICATE)


def extract_card_features(region_bgr, dx=0.0, dy=0.0):
    return _descriptor(_warp_art(region_bgr, dx, dy))


# ---------------------------------------------------------------- 카드 저장소
def default_cache_dir():
    return jh_paths.ensure_dir(os.path.join(jh_paths.cache_dir(), "cards"))


class CardStore:
    """후보 카드 이미지를 지연 다운로드하고 디스크(PNG, 폭 176px)에 캐시한다."""

    def __init__(self, cache_dir=None, download=True, sleep=0.3, timeout=5, total_timeout=10,
                 breaker=None, limiter=None, clock=time.monotonic, on_event=None):
        self.cache_dir = cache_dir or default_cache_dir()
        self.download = download
        self.sleep = sleep
        self.timeout = timeout                  # 연결/읽기 1회 제한
        self.total_timeout = total_timeout      # 이미지 1장 전체 제한
        self.breaker = breaker or jh_net.get_breaker()       # 호스트 단위 서킷브레이커(프로세스 공유)
        self.limiter = limiter or jh_net.get_limiter("card_dl", jh_net.IMAGES_PER_HOUR, 3600)
        self.clock = clock
        self.on_event = on_event or (lambda msg: None)       # 상태 전환 로그용 콜백
        self._desc = {}
        self._fail = {}                 # aid -> 실패 시각: 잠시 재다운로드를 쉰다 (음성 캐시)
        self._lock = threading.Lock()

    def _path(self, aid):
        return os.path.join(self.cache_dir, f"{int(aid)}.png")

    def _fetch(self, aid):
        req = urllib.request.Request(CARD_URL.format(id=int(aid)),
                                     headers={"User-Agent": USER_AGENT})
        t0 = time.monotonic()
        chunks = []
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            while True:
                b = r.read(65536)
                if not b:
                    break
                chunks.append(b)
                if time.monotonic() - t0 > self.total_timeout:
                    raise TimeoutError("image download too slow")
        data = b"".join(chunks)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:  # opencv webp 미지원 빌드 대비
            from PIL import Image
            import io
            pil = Image.open(io.BytesIO(data)).convert("RGB")
            img = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        h, w = img.shape[:2]
        img = cv2.resize(img, (_REF_W, max(1, int(h * _REF_W / w))), interpolation=cv2.INTER_AREA)
        os.makedirs(self.cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tmp.png", prefix=f"{int(aid)}.", dir=self.cache_dir)   # 스레드마다 고유 (경합 방지)
        os.close(fd)
        try:
            if not cv2.imwrite(tmp, img):
                raise OSError("imwrite failed")
            os.replace(tmp, self._path(aid))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        time.sleep(self.sleep)
        return img

    def get_image(self, aid):
        """캐시된 참조 이미지(BGR) 또는 None. 캐시에 없으면 다운로드(허용 시)."""
        p = self._path(aid)
        if os.path.exists(p):
            data = np.fromfile(p, np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        if not self.download:
            return None
        if time.monotonic() - self._fail.get(aid, -1e9) < FAIL_RETRY_SEC:
            return None
        now = self.clock()
        if not self.breaker.allow(now):          # 사이트 응답 없음: 서킷이 열려 있는 동안 시도하지 않음
            return None
        if not self.limiter.allow(now):
            if not self.limiter.capped_logged:
                self.limiter.capped_logged = True
                self.on_event(f"[네트워크] 이미지 다운로드 시간당 상한({jh_net.IMAGES_PER_HOUR}장) 도달, 잠시 중단")
            return None
        try:
            img = self._fetch(aid)
            self.breaker.success()
            return img
        except Exception:
            self._fail[aid] = time.monotonic()
            if self.breaker.failure(now):
                st = self.breaker.state(now)
                self.on_event(f"[네트워크] 이미지 서버 연속 실패 -> {st['minutes']}분간 시도 중단")
            return None

    def get_descriptor(self, aid):
        with self._lock:
            d = self._desc.get(aid)
            if d is not None:
                return d
        img = self.get_image(aid)
        if img is None:
            return None
        d = _reference_descriptor(img)
        with self._lock:
            self._desc[aid] = d
        return d


_default_store = None


def default_store():
    return _store()


def cached_path(aid, store=None):
    """캐시된 참조 이미지(PNG) 경로, 없으면 None (다운로드하지 않음). 툴팁 썸네일용."""
    p = (store or _store())._path(aid)
    return p if os.path.exists(p) else None


def _store():
    global _default_store
    if _default_store is None:
        _default_store = CardStore()
    return _default_store


# ---------------------------------------------------------------- 식별
def score_candidates(region_bgr, candidate_ids, store=None):
    """{arcana_id: score} (참조를 못 구한 후보는 제외). 위치 지터 중 최고 점수 사용."""
    store = store or _store()
    feats = [extract_card_features(region_bgr, dx, dy) for dx in _JITTER for dy in _JITTER]
    out = {}
    for aid in candidate_ids:
        ref = store.get_descriptor(aid)
        if ref is None:
            continue
        out[aid] = max(_similarity(f, ref) for f in feats)
    return out


def identify(card_bgr_crop, candidate_arcana_ids, store=None,
             min_score=MIN_SCORE, min_margin=MIN_MARGIN):
    """-> (arcana_id | None, score, margin)

    card_bgr_crop: crop_card_region() 결과.
    후보가 1개면 점수만 검사(마진은 1위 점수 자체로 반환), 2개 이상이면 1-2위 격차도 검사.
    모호하면 id=None (점수/마진은 참고용으로 그대로 반환).
    """
    ids = list(dict.fromkeys(candidate_arcana_ids or []))
    if card_bgr_crop is None or getattr(card_bgr_crop, "size", 0) == 0 or not ids:
        return None, 0.0, 0.0
    sc = score_candidates(card_bgr_crop, ids, store)
    if not sc:
        return None, 0.0, 0.0
    if len(ids) > 1 and len(sc) < len(ids):
        # 참조 이미지를 하나라도 못 구했으면 남은 후보 하나를 그대로 채택하지 않는다 (호출 측은 전부 표시로 폴백)
        return None, max(sc.values()), 0.0
    ranked = sorted(sc.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best = ranked[0]
    margin = best - ranked[1][1] if len(ranked) > 1 else best
    LAST_SCORES.clear()
    LAST_SCORES.update(sc)
    strong = len(ranked) > 1 and margin >= STRONG_MARGIN
    if best < (min(min_score, MIN_SCORE_STRONG) if strong else min_score):
        return None, best, margin
    if len(ranked) > 1 and margin < min_margin:
        return None, best, margin
    return best_id, best, margin



def identify_stable(grab, candidate_arcana_ids, store=None, need=2, max_tries=8, sleep=time.sleep, interval=0.25):
    """grab() -> 카드 영역 BGR (실패 시 None). 같은 id 가 연속 need 번 나오면 채택 (라벨 패널 애니메이션 중 오판 방지).
    -> (arcana_id | None, score, margin, tries). 끝까지 안정되지 않으면 id=None (호출 측은 전부 표시로 폴백)."""
    last, streak, score, margin = None, 0, 0.0, 0.0
    for n in range(1, max_tries + 1):
        try:
            img = grab()
        except Exception:
            img = None
        aid, score, margin = identify(img, candidate_arcana_ids, store)
        if aid is not None and aid == last:
            streak += 1
        else:
            streak = 1 if aid is not None else 0
        last = aid
        if aid is not None and streak >= need:
            return aid, score, margin, n
        if n < max_tries:
            sleep(interval)
    return None, score, margin, max_tries
