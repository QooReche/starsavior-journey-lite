# -*- coding: utf-8 -*-
"""
jh_net.py - 네트워크 부하 제한 공통 규칙 (GUI/OCR 의존성 없음)

* backoff_sec(n): 연속 실패 n회 -> 다음 시도까지 대기(초). DB/V2 는 meta.json 에 저장해 재시작으로 우회되지 않는다.
* CircuitBreaker: 호스트 단위(메모리, 프로세스 단위) 연속 실패 차단기. 카드 이미지용.
* RateLimiter: 슬라이딩 윈도우 횟수 상한(하루 DB 갱신 횟수, 시간당 이미지 다운로드 수).
시간은 항상 호출자가 now 로 넘긴다(테스트에서 가짜 시계 사용).
"""
import math
import threading
from collections import deque

# 연속 실패 1,2,3,4+ 회 -> 10분, 30분, 2시간, 6시간(상한)
BACKOFF_STEPS = (10 * 60, 30 * 60, 2 * 3600, 6 * 3600)
# 서킷브레이커: 열릴 때마다(1,2,3,4+ 번째) 5분, 15분, 1시간, 6시간(상한)
BREAKER_STEPS = (5 * 60, 15 * 60, 3600, 6 * 3600)
BREAKER_THRESHOLD = 3
DB_ATTEMPTS_PER_DAY = 8
IMAGES_PER_HOUR = 200
CARD_HOST = "starsavior-guide.github.io"


def backoff_sec(n):
    """연속 실패 n회 뒤 대기 시간(초). n<=0 이면 0."""
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        return 0
    return BACKOFF_STEPS[min(n, len(BACKOFF_STEPS)) - 1]


def clean_num(v, default=0):
    """meta 값 방어: 숫자(음수 아님)가 아니면 default"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v != v:
        return default
    return v


def minutes_left(until, now):
    return int(math.ceil((until - now) / 60.0)) if until > now else 0


class RateLimiter:
    """window 초 안에 limit 회까지만 허용. allow() 가 허용하면 1회로 센다."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._t = deque()
        self._lock = threading.Lock()
        self.capped_logged = False

    def _trim(self, now):
        while self._t and now - self._t[0] >= self.window:
            self._t.popleft()

    def allow(self, now):
        with self._lock:
            self._trim(now)
            if len(self._t) >= self.limit:
                return False
            self._t.append(now)
            self.capped_logged = False if len(self._t) < self.limit else self.capped_logged
            return True

    def used(self, now):
        with self._lock:
            self._trim(now)
            return len(self._t)

    def reset(self):
        with self._lock:
            self._t.clear()
            self.capped_logged = False


class CircuitBreaker:
    """연속 실패가 threshold 에 이르면 열려서 open_until 까지 차단. 재개 후 첫 시도가 실패하면 더 길게 다시 열린다. 성공하면 리셋."""

    def __init__(self, threshold=BREAKER_THRESHOLD, steps=BREAKER_STEPS):
        self.threshold, self.steps = threshold, steps
        self.fails, self.trips, self.open_until = 0, 0, 0.0
        self._lock = threading.Lock()

    def allow(self, now):
        with self._lock:
            return now >= self.open_until

    def success(self):
        with self._lock:
            self.fails = self.trips = 0
            self.open_until = 0.0

    def failure(self, now):
        """-> 이번 실패로 새로 열렸으면 True (로그용)"""
        with self._lock:
            self.fails += 1
            if self.fails >= self.threshold:
                self.trips += 1
                self.open_until = now + self.steps[min(self.trips, len(self.steps)) - 1]
                return True
            return False

    def reset(self):
        self.success()

    def state(self, now):
        with self._lock:
            return {"open": now < self.open_until, "fails": self.fails, "trips": self.trips,
                    "minutes": minutes_left(self.open_until, now)}


# 프로세스 단위 공유 상태
_breakers = {}
_limiters = {}
_glock = threading.Lock()


def get_breaker(host=CARD_HOST):
    with _glock:
        return _breakers.setdefault(host, CircuitBreaker())


def get_limiter(name, limit, window):
    with _glock:
        lim = _limiters.get(name)
        if lim is None:
            lim = _limiters[name] = RateLimiter(limit, window)
        return lim


def reset_for_tests():
    with _glock:
        _breakers.clear()
        _limiters.clear()
