# -*- coding: utf-8 -*-
"""
JourneyHelper Lite - 스타세이비어 여정 이벤트 선택지 보상 오버레이 (경량판)

원작: https://github.com/p1021/journeyhelper  (이 파일은 그 코드를 PyQt5 + rapidocr-onnxruntime 로 줄여 다시 쓴 것)
DB  : https://starsavior-guide.github.io/data/

동작: 게임 창에서 '여정/훈련/아르카나 이벤트' 라벨을 확인 -> 이벤트 제목 OCR -> DB 매칭 -> 선택지 위에
      마우스를 올리면 예상 보상(+잠재력/버프 설명)을 왼쪽 툴팁으로 표시. 계산기류 기능은 없다.
"""
import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes

try:
    # 중요: onnxruntime 은 반드시 PyQt5 보다 먼저 import 해야 한다.
    # (Windows 에서 Qt 를 먼저 올리면 onnxruntime DLL 로딩 중 access violation 으로 프로세스가 죽는다)
    import onnxruntime  # noqa: F401
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    RapidOCR = None

import cv2
import numpy as np

import jh_cards
import jh_db

try:
    import mss
    from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QCursor, QIcon, QPainter, QPixmap
    from PyQt5.QtWidgets import (QAction, QActionGroup, QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon,
                                 QVBoxLayout, QWidget)
except ImportError:  # --selftest 는 GUI 없이도 돌 수 있게
    mss = None
    QThread = object
    QWidget = object
    pyqtSignal = lambda *a, **k: None

APP_NAME = "JourneyHelperLite"
APP_VERSION = "1.0.6-lite.1"

DEFAULT_SETTINGS = {
    "language": "ko",               # "ko" | "en"  (화면 문구 + DB 언어, 변경 시 재시작)
    "startup_state": "collapsed",   # 상단바 시작 상태: "collapsed" | "expanded"
    "tooltip_font_scale": 1.0,
    "show_info": True,              # 잠재력/버프 설명 표시
    "low_spec_mode": False,         # OCR 주기를 늘려 CPU 절약
    "status_badge_locked": True,
    "status_badge_offset": None,
    "difficulty": "all",            # 난이도 표시 필터: "all" | "easy" | "normal" | "hard" (난이도 라벨이 있는 이벤트만 해당)
    "show_capture_debug": False,    # True: 상단바/툴팁이 스크린샷에도 보이게 함 (진단용, OCR 방해 가능)
}

# 게임 창 기준 비율 (원작 값 유지). 라벨은 '여정 이벤트' 문구가 있는 작은 띠.
REGION_FRACTIONS = {
    "event_title": {"top": 0.205, "left": 0.110, "width": 0.240, "height": 0.056},
    "event_label": {"top": 0.170, "left": 0.110, "width": 0.200, "height": 0.032},
}
EVENT_LABEL_PHRASES = ("여정이벤트", "훈련이벤트", "아르카나이벤트")
EVENT_LABEL_PHRASES_EN = ("journeyevent", "trainingevent", "arcanaevent")
NOISE_TITLE_WORDS = ("여정이벤트", "훈련이벤트", "아르카나이벤트", "제목", "journeyevent", "trainingevent", "arcanaevent")
_GAME_KEYWORDS = ["스타세이비어", "스타 세이비어", "starsavior", "star savior"]
_BROWSER_CLASSES = {"chrome_widgetwin_1", "mozillawindowclass", "applicationframewindow", "operawindowclass"}


# ----------------------------------------------------------------------------
# 경로 / 설정 / 로그
# ----------------------------------------------------------------------------
def resource_path(*parts):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def get_settings_dir():
    path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(get_settings_dir(), "settings.json"), "r", encoding="utf-8-sig") as f:   # BOM 허용
            saved = json.load(f)
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    if settings.get("language") not in jh_db.SUPPORTED_LANGS:
        settings["language"] = "ko"
    if settings.get("difficulty") not in jh_db.DIFFICULTIES:
        settings["difficulty"] = "all"
    if settings.get("startup_state") not in ("collapsed", "expanded"):
        settings["startup_state"] = "collapsed"
    return settings


LOG_MAX_BYTES = 256 * 1024
_log_lock = threading.Lock()


def log(msg):
    """%APPDATA%\\JourneyHelperLite\\log.txt 에 한 줄 기록 (256KB 에서 log.txt.1 로 회전). 어떤 실패도 앱을 멈추지 않는다."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with _log_lock:
            path = os.environ.get("JH_LOG_PATH") or os.path.join(get_settings_dir(), "log.txt")
            try:
                if os.path.getsize(path) > LOG_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
    except Exception:
        pass


def save_settings(settings):
    try:
        with open(os.path.join(get_settings_dir(), "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[설정 저장 실패] {e}")


# ----------------------------------------------------------------------------
# 표시 언어 (화면에 보이는 문구만; 콘솔 로그는 항상 한글)
# 번역이 없는 문구는 한글 원문이 그대로 나온다 (앱이 깨지지 않음).
# ----------------------------------------------------------------------------
_CURRENT_LANGUAGE = "ko"

_TRANSLATIONS_EN = {
    "🔽 여정 시 펼쳐주세요": "🔽 Expand during Journey",
    "🟡 준비 중...": "🟡 Getting ready...",
    "🟡 [DB 불러오는 중] 이벤트 데이터를 가져오는 중...": "🟡 [Loading DB] Fetching event database...",
    "🟡 [OCR 모델 로딩 중] 처음 한 번만 몇 초 걸려요...": "🟡 [Loading OCR Model] Only takes a few seconds, once...",
    "⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...": "⚪ [Waiting] Looking for a Journey event screen...",
    "🌕 [로딩 중] 다음 화면으로 이동하는 중...": "🌕 [Loading] Moving to the next screen...",
    "🟢 [여정 진행 중] {}": "🟢 [Journey in Progress] {}",
    "🔵 [이벤트 확인 중] {}...": "🔵 [Confirming Event] {}...",
    "⚠️ [추정] {}": "⚠️ [Estimate] {}",
    "⚠️ [후보 {}개] {}": "⚠️ [{} candidates] {}",
    "🟠 [DB에 없는 제목] {}": "🟠 [Title not in DB] {}",
    "🟢 [DB 준비 완료] 이벤트 {}개": "🟢 [Database Ready] {} events loaded",
    "⏳ 수동 새로고침은 잠시 후 다시 가능합니다 (10분 쿨다운)": "⏳ Manual refresh is on cooldown (10 min)",
    "⚠️ 갱신 실패 - 저장된 DB를 사용합니다": "⚠️ Update failed - using the saved DB",
    "🔴 DB 없음 - 인터넷 연결 후 메뉴에서 새로고침": "🔴 No DB - connect to the internet, then refresh from the menu",
    "🔼 상단바 펼치기": "🔼 Expand top bar",
    "🔽 상단바 접기": "🔽 Collapse top bar",
    "🔄 DB 새로고침 (10분 쿨다운)": "🔄 Refresh DB (10 min cooldown)",
    "▶️ 헬퍼 재개": "▶️ Resume helper",
    "⏸️ 헬퍼 일시정지": "⏸️ Pause helper",
    "🛑 프로그램 종료": "🛑 Quit Program",
    "⚙️ 설정": "⚙️ Settings",
    "🔤 툴팁 글자 크기": "🔤 Tooltip font size",
    "작게": "Small", "보통": "Normal", "크게": "Large", "아주 크게": "Extra Large",
    "🚀 시작 시 상단바": "🚀 Top bar on startup",
    "접힘": "Collapsed", "펼침": "Expanded",
    "🌐 언어": "🌐 Language",
    "🎚️ 난이도 표시": "🎚️ Difficulty filter",
    "전체": "All", "쉬움": "Easy", "어려움": "Hard",
    "잠재력/버프 설명 표시": "Show potential/buff descriptions",
    "⚡ 저사양 모드 (OCR 주기 늘림)": "⚡ Low-spec mode (slower OCR polling)",
    "상단바 위치 고정": "Lock top bar position",
    "상단바 위치 초기화": "Reset top bar position",
    "현재 버전: {}": "Current version: {}",
    "JourneyHelper가 실행 중입니다 (우클릭: 메뉴)": "JourneyHelper is running (right-click for menu)",
    "재시작 필요": "Restart Required",
    "언어를 바꾸면 여정 이벤트 데이터를 새 언어로 다시 불러와야 해요.\n지금 재시작할까요? (재시작 안 하면 일부 이벤트 인식이 이전 언어\n기준으로 남아있어 오작동할 수 있어요)":
        "Changing the language requires reloading Journey event data in the new language.\nRestart now? (If you don't, some event recognition may stay on the old language and misbehave)",
}


def set_language(lang):
    global _CURRENT_LANGUAGE
    _CURRENT_LANGUAGE = lang if lang in jh_db.SUPPORTED_LANGS else "ko"


def get_language():
    return _CURRENT_LANGUAGE


def tr(text):
    """현재 언어가 "en"이면 번역표에서 찾아 반환하고, 없으면 한글 원문을 그대로 돌려준다."""
    if _CURRENT_LANGUAGE == "en":
        return _TRANSLATIONS_EN.get(text, text)
    return text



def install_crash_logger():
    path = os.path.join(get_settings_dir(), "crash_log.txt")

    def hook(exc_type, exc, tb):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = hook


# ----------------------------------------------------------------------------
# Windows 헬퍼: 게임 창 찾기 / 좌표 / 캡처 제외
# ----------------------------------------------------------------------------
_cached_hwnd = None


def _window_text(user32, hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _is_browser(user32, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value.lower() in _BROWSER_CLASSES


def _title_is_game(title):
    t = title.lower()
    return any(k in t for k in _GAME_KEYWORDS)


def find_game_hwnd():
    global _cached_hwnd
    try:
        user32 = ctypes.windll.user32
        if _cached_hwnd and user32.IsWindow(_cached_hwnd):
            return _cached_hwnd
        my_pid, found = os.getpid(), {"h": None}
        proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != my_pid and not _is_browser(user32, hwnd) and _title_is_game(_window_text(user32, hwnd)):
                    found["h"] = hwnd
                    return False
            return True

        user32.EnumWindows(proc_t(cb), 0)
        _cached_hwnd = found["h"]
        return _cached_hwnd
    except Exception:
        return None


def get_game_window_rect():
    """게임 클라이언트 영역(화면 좌표). 없으면 None."""
    try:
        hwnd = find_game_hwnd()
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        rc = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
            return None
        w, h = rc.right - rc.left, rc.bottom - rc.top
        if w <= 0 or h <= 0:
            return None
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return {"left": pt.x, "top": pt.y, "width": w, "height": h}
    except Exception:
        return None


def game_window_usable():
    """게임 창이 존재하고 보이는 상태(최소화 아님)인지. 다른 창을 눌러 포커스를 잃은 것은 '있음'으로 본다."""
    try:
        user32 = ctypes.windll.user32
        hwnd = find_game_hwnd()
        return bool(hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd))
    except Exception:
        return False


def is_game_hovered_or_active():
    try:
        user32 = ctypes.windll.user32
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd = user32.WindowFromPoint(pt)
        cands = []
        if hwnd:
            cands.append(user32.GetAncestor(hwnd, 2) or hwnd)
        cands.append(user32.GetForegroundWindow())
        return any(h and not _is_browser(user32, h) and _title_is_game(_window_text(user32, h)) for h in cands)
    except Exception:
        return False


_single_instance_handle = None


def acquire_single_instance():
    global _single_instance_handle
    _single_instance_handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183


def release_single_instance():
    global _single_instance_handle
    try:
        if _single_instance_handle:
            ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
            _single_instance_handle = None
    except Exception:
        pass


def reassert_topmost(widget):
    """게임이 포커스를 되찾은 뒤에도 오버레이가 가려지지 않게 최상단을 다시 지정한다."""
    try:
        # HWND_TOPMOST(-1), SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
        ctypes.windll.user32.SetWindowPos(int(widget.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def exclude_from_capture(widget, hide=True):
    """오버레이가 화면 캡처(OCR)에 찍히지 않도록 한다. hide=False 면 캡처에 보이게 한다(진단용)."""
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11 if hide else 0)
    except Exception:
        pass


def region_to_monitor(frac, rect):
    return {"left": rect["left"] + int(rect["width"] * frac["left"]),
            "top": rect["top"] + int(rect["height"] * frac["top"]),
            "width": max(1, int(rect["width"] * frac["width"])),
            "height": max(1, int(rect["height"] * frac["height"]))}


def is_loading_screen(sct, rect):
    """거의 전체가 밝은 로딩 화면인지 (원작 방식 유지)"""
    try:
        img = np.array(sct.grab(rect))
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY), (96, 54), interpolation=cv2.INTER_AREA)
        return float(np.mean(small > 200)) > 0.50 and float(np.mean(small)) > 180
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 텍스트 판정 (순수 함수 - 테스트 가능)
# ----------------------------------------------------------------------------
def match_event_label(text, cutoff=0.8, lang="ko"):
    """OCR 텍스트가 '여정 이벤트/훈련 이벤트/아르카나 이벤트' 전체 문구와 일치(또는 거의 일치)하면 문구를 반환.
    '이벤트' 같은 일반 단어만으로는 통과시키지 않는다 (장비/스탯 화면 오탐 방지).
    lang == "en" 이면 'Journey Event / Training Event / Arcana Event' 도 허용한다."""
    pairs = [(re.sub(r"[^가-힣]", "", text or ""), EVENT_LABEL_PHRASES, 4)]
    if lang == "en":
        pairs.append((re.sub(r"[^a-z]", "", (text or "").lower()), EVENT_LABEL_PHRASES_EN, 8))
    for t_, phrases, min_len in pairs:
        if len(t_) < min_len:
            continue
        for p in phrases:
            if p in t_:
                return p
        for p in phrases:
            for L in (len(p) - 1, len(p), len(p) + 1):
                for i in range(0, len(t_) - L + 1):
                    if difflib.SequenceMatcher(None, t_[i:i + L], p).ratio() >= cutoff:
                        return p
    return None


def label_kind(phrase):
    """match_event_label 결과 -> 'arcana' | 'general' | None"""
    if phrase in ("아르카나이벤트", "arcanaevent"):
        return "arcana"
    if phrase in ("여정이벤트", "훈련이벤트", "journeyevent", "trainingevent"):
        return "general"
    return None


def clean_ocr_title(text):
    t = re.sub(r"[^가-힣0-9a-zA-Z]", "", text or "")
    for w in NOISE_TITLE_WORDS:
        t = re.sub(w, "", t, flags=re.I)
    return t


def same_text(a, b, ratio=0.82):
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= ratio


# ----------------------------------------------------------------------------
# OCR (rapidocr-onnxruntime + PaddleOCR 한국어 인식 모델)
# 기본 번들 모델은 중국어/영어 전용이라 한국어 rec 모델과 사전을 따로 지정한다.
# 제목/라벨은 한 줄짜리 작은 영역이라 검출(det) 단계 없이 인식(rec)만 쓴다 (60ms 내외).
# ----------------------------------------------------------------------------
class KoreanOCR:
    MODEL = "korean_PP-OCRv4_rec_mobile.onnx"
    KEYS = "korean_dict.txt"

    def __init__(self, model_dir=None):
        if RapidOCR is None:
            raise RuntimeError("rapidocr-onnxruntime 가 설치되어 있지 않습니다")
        d = model_dir or resource_path("models")
        self.engine = RapidOCR(rec_model_path=os.path.join(d, self.MODEL), rec_keys_path=os.path.join(d, self.KEYS),
                               intra_op_num_threads=2, inter_op_num_threads=1)

    def read_line(self, img):
        """한 줄짜리 이미지(gray/BGR) -> (text, confidence)"""
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        img = cv2.copyMakeBorder(img, 6, 6, 12, 12, cv2.BORDER_REPLICATE)
        res, _ = self.engine(img, use_det=False, use_cls=False)
        if not res:
            return "", 0.0
        text, conf = res[0][0], res[0][1]
        return (text or "").strip(), float(conf or 0.0)


DIFFICULTY_CHOICES = (("전체", "all"), ("쉬움", "easy"), ("보통", "normal"), ("어려움", "hard"))
TOOLTIP_MAX_DETAIL = 2      # 툴팁이 화면보다 길 때 jh_db.format_*_html 의 detail 을 이 단계까지 올려 줄인다
TITLE_SCALES = (2.0, 1.6, 1.0)      # 배율마다 오인식이 달라 후보를 모두 모은다 (2.0 이 평균적으로 가장 정확)


def read_title_candidates(ocr, gray, scales=TITLE_SCALES, min_conf=0.4):
    """제목 영역(gray)을 여러 배율(INTER_CUBIC)로 읽어 정리된 후보 문자열들을 중복 없이 돌려준다."""
    out = []
    for sc in scales:
        g = gray if sc == 1.0 else cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
        text, conf = ocr.read_line(g)
        t = clean_ocr_title(text) if conf >= min_conf else ""
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out


class LeaveGrace:
    """라벨이 found<->unseen/other 로 깜빡일 때 '이탈'을 곧바로 확정하지 않기 위한 유예 타이머."""

    def __init__(self, grace, clock=time.monotonic):
        self.grace, self.clock, self._since = grace, clock, None

    def good(self):
        self._since = None

    def bad_long_enough(self):
        now = self.clock()
        if self._since is None:
            self._since = now
        return now - self._since >= self.grace


class TitleMemo:
    """방금 이벤트 화면을 떠난 직후(ttl 초) 같은 제목 이미지가 다시 보이면 OCR 을 다시 하지 않고 결과를 재사용한다."""

    def __init__(self, ttl, clock=time.monotonic):
        self.ttl, self.clock, self._item = ttl, clock, None

    def remember(self, gray, cands, title):
        if gray is not None and cands:
            self._item = (self.clock(), gray, list(cands), title)

    def recall(self, gray, limit=1.0):
        it = self._item
        if it is None or self.clock() - it[0] > self.ttl:
            return None
        if it[1].shape != gray.shape or cv2.absdiff(gray, it[1]).mean() >= limit:
            return None
        return list(it[2]), it[3]


# ----------------------------------------------------------------------------
# 화면 감시 스레드: 라벨 확인 -> (확인된 경우에만) 제목 OCR
# ----------------------------------------------------------------------------
class ScreenWatcher(QThread):
    title_detected = pyqtSignal(object, object)  # (후보 리스트, 라벨 종류 'arcana'/'general'/None)   # OCR 후보 문자열 리스트 (배율별, 가장 믿을 만한 것이 앞)
    journey_status_changed = pyqtSignal(bool)
    model_loading = pyqtSignal()
    model_ready = pyqtSignal()

    LEAVE_AFTER_OTHER = 3     # 라벨 자리에 다른 글자가 읽힘 -> 이벤트 화면이 아니라고 확신 (연속 횟수)
    LEAVE_AFTER_UNSEEN = 8    # 아무것도 못 읽음 -> 스캔 실패일 수 있으니 더 오래 기다림
    LEAVE_GRACE_SEC = 1.5     # 위 횟수를 채워도 이만큼 계속 안 보여야 이탈 확정 (깜빡임 디바운스)
    REENTRY_MEMO_SEC = 4.0    # 이탈 직후 이 시간 안에 같은 제목 이미지가 다시 보이면 OCR 생략

    def __init__(self, pause_getter=None, low_spec_getter=None, lang_getter=None):
        super().__init__()
        self.lang_getter = lang_getter or (lambda: "ko")
        self._last_rect = None
        self.pause_getter = pause_getter or (lambda: False)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self.ocr = None
        self._stop = False
        # (2) 실제로 확인되기 전까지는 항상 False
        self.label_visible = False
        self.on_event_screen = False
        self._reverify = True
        self._was_inactive = False
        self._grace = LeaveGrace(self.LEAVE_GRACE_SEC)
        self._memo = TitleMemo(self.REENTRY_MEMO_SEC)
        self._reset_scan_state()

    def _reset_scan_state(self):
        self._miss = 0
        self._last_label_gray = None
        self._label_state = "unseen"
        self._last_title_gray = None
        self._cached_title = []
        self._label_kind = None
        self.last_title = ""

    def request_reverify(self):
        """(5) 로딩 화면 이후: 라벨을 다시 확인하기 전에는 제목 인식을 하지 않는다."""
        self._reverify = True

    def stop(self):
        self._stop = True

    def _leave_event_screen(self):
        """(1) 라벨이 사라졌거나 이벤트 화면을 떠남 -> 진행 상태 전부 초기화"""
        was = self.on_event_screen
        self._memo.remember(self._last_title_gray, self._cached_title, self.last_title)
        self._grace.good()
        self.label_visible = False
        self.on_event_screen = False
        self._reset_scan_state()
        if was:
            self.journey_status_changed.emit(False)

    def _scan_label(self, sct, rect):
        """작은 라벨 영역만 읽어서 'found' / 'other'(다른 글자) / 'unseen'(못 읽음) 중 하나를 돌려준다. (4)"""
        gray = cv2.cvtColor(np.array(sct.grab(region_to_monitor(REGION_FRACTIONS["event_label"], rect))), cv2.COLOR_BGRA2GRAY)
        last = self._last_label_gray
        if last is not None and last.shape == gray.shape and cv2.absdiff(gray, last).mean() < 1.0:
            return self._label_state
        self._last_label_gray = gray
        text, conf = self.ocr.read_line(gray)
        if conf < 0.3 or not re.sub(r"[^가-힣]", "", text):
            state = "unseen"
        elif match_event_label(text, lang=self.lang_getter()):
            state = "found"
            self._label_kind = label_kind(match_event_label(text, lang=self.lang_getter()))
        else:
            state = "other"
        if state != self._label_state:
            log(f"[라벨] {self._label_state} -> {state} ('{text}' conf={conf:.2f})")
        self._label_state = state
        return state

    def _scan_title(self, sct, rect):
        gray = cv2.cvtColor(np.array(sct.grab(region_to_monitor(REGION_FRACTIONS["event_title"], rect))), cv2.COLOR_BGRA2GRAY)
        last = self._last_title_gray
        limit = 3.0 if self.low_spec_getter() else 1.0
        if last is not None and last.shape == gray.shape and cv2.absdiff(gray, last).mean() < limit:
            return self._cached_title
        self._last_title_gray = gray
        memo = self._memo.recall(gray, limit)
        if memo:
            self._cached_title = memo[0]
            log(f"[OCR] 직전과 같은 제목 이미지 -> 재인식 생략 {memo[0]}")
        else:
            self._cached_title = read_title_candidates(self.ocr, gray)
        return self._cached_title

    def run(self):
        sct = mss.mss()
        while not self._stop:
            try:
                if self.pause_getter():
                    self.msleep(200)
                    continue
                rect = get_game_window_rect() if is_game_hovered_or_active() else None
                if rect is None:
                    self._last_rect = None
                    if game_window_usable():
                        # 게임은 떠 있고 다른 창을 눌러 포커스만 잃은 상태: 다른 창을 OCR 하지 않도록 스캔만 멈추고
                        # 이벤트 상태는 유지한다. 포커스가 돌아오면 캐시만 비워 바로 다시 읽는다.
                        self._was_inactive = True
                        self.msleep(300)
                        continue
                    self._leave_event_screen()      # 게임 창이 없거나 최소화됨 -> 상태 초기화
                    self._was_inactive = False
                    self.msleep(1000)
                    continue
                if self._was_inactive:
                    self._was_inactive = False
                    self._last_label_gray = self._last_title_gray = None
                if self.ocr is None:
                    self.model_loading.emit()
                    log("[지연 로딩] 게임 감지됨 -> 제목/라벨 인식용 OCR 모델 로딩 시작...")
                    self.ocr = KoreanOCR()
                    log("[지연 로딩] 제목/라벨 인식용 OCR 모델 로딩 완료")
                    self.model_ready.emit()
                lr = self._last_rect
                if lr and any(abs(rect[k] - lr[k]) > 5 for k in ("left", "top", "width", "height")):
                    # 게임 창이 이동/리사이즈됨: 영역은 매 주기 창 기준 비율로 다시 계산되므로 캐시만 비우고 재스캔한다
                    log("[게임 창 변경 감지] 위치/크기가 바뀜 -> 캡처 영역 재보정")
                    self._last_label_gray = self._last_title_gray = None
                self._last_rect = dict(rect)
                if self._reverify:
                    self._reverify = False
                    self.label_visible = False
                    self.on_event_screen = False      # 라벨을 다시 확인하면 상태 True 를 새로 알린다
                    self._reset_scan_state()

                state = self._scan_label(sct, rect)
                if state == "found":
                    self._miss = 0
                    self._grace.good()
                    self.label_visible = True
                    if not self.on_event_screen:
                        self.on_event_screen = True
                        self.journey_status_changed.emit(True)
                else:
                    self._miss += 1
                    limit = self.LEAVE_AFTER_OTHER if state == "other" else self.LEAVE_AFTER_UNSEEN
                    long_enough = self._grace.bad_long_enough()      # 처음 안 보인 순간부터 시간을 잰다
                    if self._miss >= limit and long_enough and (self.label_visible or self.on_event_screen):
                        self._leave_event_screen()
                    if state == "other":
                        self.label_visible = False       # 다른 글자가 보이면 즉시 제목 인식 중단 (오탐 방지)

                if self.label_visible:
                    cands = self._scan_title(sct, rect)
                    title = cands[0] if cands else ""
                    if len(title) >= 2 and not same_text(title, self.last_title, 0.9):
                        self.last_title = title
                        log(f"[OCR] 제목 후보: {cands}")
                        self.title_detected.emit(list(cands), self._label_kind)
            except Exception as e:
                log(f"[ScreenWatcher 예외] {e}\n{traceback.format_exc()}")
            self.msleep(350 if self.low_spec_getter() else 200)


def grab_card_crop(sct, rect):
    """게임 창에서 카드 썸네일 영역을 BGR 로 캡처 (jh_cards.CARD_REGION_FRAC)."""
    x0, y0, x1, y1 = jh_cards.CARD_REGION_FRAC
    mon = region_to_monitor({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}, rect)
    return cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)


class CardThread(QThread):
    """제목이 여러 아르카나에 있을 때 카드 썸네일로 어느 아르카나인지 식별 (참조 이미지 다운로드가 있어 별도 스레드)."""
    resolved = pyqtSignal(str, object, str)      # (제목, arcanaId 또는 None, 로그 문구)

    def __init__(self, title, ids):
        super().__init__()
        self.title, self.ids = title, list(ids)

    def run(self):
        aid, info = None, ""
        try:
            sct = mss.mss()

            def grab():
                rect = get_game_window_rect()
                return grab_card_crop(sct, rect) if rect else None

            aid, score, margin, tries = jh_cards.identify_stable(grab, self.ids)
            info = f"score={score:.2f} margin={margin:.2f} tries={tries}"
        except Exception as e:
            info = f"예외 {e}"
        self.resolved.emit(self.title, aid, info)


# ----------------------------------------------------------------------------
# DB 로딩 스레드
# ----------------------------------------------------------------------------
class DbThread(QThread):
    finished_ok = pyqtSignal(object, str, str)      # cache(list), status, message

    def __init__(self, store, manual=False):
        super().__init__()
        self.store, self.manual = store, manual

    def run(self):
        try:
            res = self.store.update(manual=self.manual)
            journey, arcanas = self.store.load()
            cache = jh_db.build_event_cache(journey, arcanas) if journey else []
            self.finished_ok.emit(cache, res.status, res.message)
        except Exception as e:
            log(f"[DB 로딩 예외] {e}\n{traceback.format_exc()}")
            self.finished_ok.emit([], "error", str(e))


# ----------------------------------------------------------------------------
# 오버레이 위젯
# ----------------------------------------------------------------------------
def make_tray_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#00e5ff")))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#0a0e18"))
    f = p.font()
    f.setBold(True)
    f.setPointSize(30)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, "J")
    p.end()
    return QIcon(pix)


class StatusBadge(QWidget):
    """게임 창 위쪽 상태 배지.
    기본은 접힌 상태(시작 시 설정 startup_state). 접혀 있어도 여정/이벤트가 감지되면 잠깐 펼쳐 보여 주고
    FLASH_SEC 뒤에 자동으로 다시 접는다. 좌클릭: 메뉴, 우클릭: 설정, (잠금 해제 시) 드래그 이동."""
    FLASH_SEC = 6.0

    def __init__(self, settings):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.settings = settings
        self.label = QLabel("", self)
        self.label.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)
        self.collapsed = settings.get("startup_state", "collapsed") != "expanded"
        self.on_menu = None            # 좌클릭
        self.on_settings = None        # 우클릭
        self._text, self._color = tr("🟡 준비 중..."), "#ffcc00"
        self._protect_until = 0
        self._flash_until = 0
        self._drag = None
        self._dragging = False
        self.render()
        self.show()
        exclude_from_capture(self)

    # --- 상태 표시 ---
    def is_flash_active(self):
        return time.time() < self._flash_until

    def set_status(self, text, color="#00e5ff", protect_sec=0, flash=False):
        if time.time() < self._protect_until and not protect_sec:
            return
        self._protect_until = time.time() + protect_sec if protect_sec else 0
        self._text, self._color = text, color
        if flash and self.collapsed:
            self._flash_until = time.time() + max(self.FLASH_SEC, protect_sec)
            self._schedule_auto_revert()
        self.render()

    def _schedule_auto_revert(self):
        QTimer.singleShot(int(self.FLASH_SEC * 1000) + 150, self._revert_after_flash)

    def _revert_after_flash(self):
        if self.collapsed and not self.is_flash_active():
            self.render()

    def set_collapsed(self, value):
        self.collapsed = bool(value)
        self._flash_until = 0
        self.render()

    def render(self):
        if self.collapsed and not self.is_flash_active():
            text, color = tr("🔽 여정 시 펼쳐주세요"), "#888888"
        else:
            text, color = self._text, self._color
        self.label.setText(text)
        self.label.setStyleSheet(f"QLabel {{ background-color: rgba(10,14,24,245); border: 2px solid {color}; "
                                 f"border-radius: 15px; color: {color}; font-size: 13px; font-weight: bold; padding: 6px 18px; }}")
        self.adjustSize()
        self.reposition()
        self.show()

    def reposition(self):
        if self._dragging:
            return
        rect = get_game_window_rect()
        left, top, w = (rect["left"], rect["top"], rect["width"]) if rect else (0, 0, QApplication.desktop().screenGeometry().width())
        off = self.settings.get("status_badge_offset")
        if off:
            self.move(int(left + off[0]), int(top + off[1]))
        else:
            self.move(int(left + w / 2 - self.width() / 2), top + 4)

    # --- 마우스 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton and self.on_settings:
            self.on_settings(e.globalPos())
        elif e.button() == Qt.LeftButton:
            self._drag, self._start, self._dragging = e.globalPos(), self.pos(), False

    def mouseMoveEvent(self, e):
        if self._drag is not None and not self.settings.get("status_badge_locked", True) and e.buttons() & Qt.LeftButton:
            d = e.globalPos() - self._drag
            if self._dragging or d.manhattanLength() > 6:
                self._dragging = True
                self.move(self._start + d)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._drag is None:
            return
        if self._dragging:
            rect = get_game_window_rect() or {"left": 0, "top": 0}
            self.settings["status_badge_offset"] = [self.x() - rect["left"], self.y() - rect["top"]]
            save_settings(self.settings)
        elif self.on_menu:
            self.on_menu(e.globalPos())
        self._drag, self._dragging = None, False


class FixedLeftTooltip(QWidget):
    BASE_W, BASE_PX = 440, 13

    def __init__(self, scale=1.0):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.label = QLabel("", self)
        self.label.setTextFormat(Qt.RichText)
        self.label.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label)
        self._src = None
        self.set_scale(scale)
        exclude_from_capture(self)

    def set_scale(self, scale):
        self.scale = scale
        self.label.setStyleSheet(
            f"QLabel {{ background-color: rgba(10,12,18,250); border: 2px solid #00e5ff; border-radius: 12px; "
            f"color: #ffffff; font-size: {max(9, round(self.BASE_PX * scale))}px; font-weight: bold; padding: 14px 18px; }}")
        self.setFixedWidth(round(self.BASE_W * scale))

    def _fit(self, html):
        self.label.setText(html)
        self.label.adjustSize()
        self.adjustSize()
        self.resize(self.sizeHint())

    def show_html(self, html):
        """html: 문자열 또는 detail(0~MAX_DETAIL) -> 문자열 함수. 함수면 화면에 들어올 때까지 단계적으로 줄여 본다."""
        rect = get_game_window_rect()
        screen = QApplication.desktop().screenGeometry()
        if callable(html):
            fn = html
            html = fn(0)
            if html != self._src:                       # 원본이 같으면 이미 맞춰 둔 크기/내용을 그대로 쓴다
                self._src = html
                self._fit(html)
                for d in range(1, TOOLTIP_MAX_DETAIL + 1):
                    if self.height() <= screen.height() - 40:
                        break
                    self._fit(fn(d))
        elif html != self._src:
            self._src = html
            self._fit(html)
        x, y = (rect["left"] + 25, rect["top"] + int(rect["height"] * 0.30)) if rect else (25, int(screen.height() * 0.30))
        y = max(0, min(y, screen.height() - self.height() - 10))      # 내용이 길면 위로 밀어 화면 안에 유지
        self.move(x, y)
        self.show()

    def hide_and_clear(self):
        if self.isVisible():
            self._src = None
            self.label.setText("")
            self.move(-10000, -10000)
            self.hide()


# ----------------------------------------------------------------------------
# 매니저: 상태, 매칭, 호버 판정, 메뉴
# ----------------------------------------------------------------------------
class MouseHoverManager(QWidget):
    HOVER_X = (0.64, 0.95)        # 선택지 버튼의 가로 범위(게임 창 비율)

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.lang = self.settings["language"]
        self.screen_w = QApplication.desktop().screenGeometry().width()
        self.db_event_cache = []
        self.store = jh_db.DbStore(lang=self.lang,
                                   user_agent=f"{APP_NAME}/{APP_VERSION} (+https://github.com/QooReche/starsavior-journey-lite)")
        self._hidden = False
        self._loading_screen = False
        self._load_hits = self._load_miss = 0
        self._game_focus_was_active = False
        self._db_threads = []
        self._sct = None
        self.reset_event_state()

        self.status = StatusBadge(self.settings)
        self.status.on_menu = self.show_main_menu
        self.status.on_settings = self.show_settings_menu
        self.tooltip = FixedLeftTooltip(self.settings["tooltip_font_scale"])
        self._apply_capture_visibility()
        self._build_tray()

        self.watcher = ScreenWatcher(pause_getter=lambda: self._hidden, low_spec_getter=lambda: self.settings["low_spec_mode"],
                                     lang_getter=lambda: self.lang)
        self.watcher.title_detected.connect(self.on_title)
        self.watcher.journey_status_changed.connect(self.on_journey_status)
        self.watcher.model_loading.connect(
            lambda: self.status.set_status(tr("🟡 [OCR 모델 로딩 중] 처음 한 번만 몇 초 걸려요..."), "#ffcc00", flash=True))
        self.watcher.model_ready.connect(lambda: self.status.set_status(self._idle_text(), "#888888"))
        self.watcher.start()
        log(f"[시작] {APP_NAME} {APP_VERSION} lang={self.lang} low_spec={self.settings['low_spec_mode']}")

        for interval, fn in ((60, self.check_hover), (300, self.poll_loading), (1000, self.maintain_overlays)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(interval)

        self.status.set_status(tr("🟡 [DB 불러오는 중] 이벤트 데이터를 가져오는 중..."), "#ffcc00")
        self.start_db(manual=False)

    # --- 상태 ---
    def reset_event_state(self):
        self.in_journey = False
        self.current_title = ""
        self.matched = None
        self.not_found = ""
        self.ambig = []
        self.zones = []
        self.options = []
        self.card_id = None
        self._card_threads = getattr(self, "_card_threads", [])
        if hasattr(self, "tooltip"):
            self.tooltip.hide_and_clear()

    def _idle_text(self):
        return tr("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...")

    def _journey_text(self):
        if self.matched:
            return tr("🟢 [여정 진행 중] {}").format(self.matched["rawTitle"])
        return tr("🔵 [이벤트 확인 중] {}...").format(self.current_title)

    # --- DB ---
    def start_db(self, manual):
        t = DbThread(self.store, manual=manual)
        t.finished_ok.connect(lambda c, s, m, man=manual: self.on_db_loaded(c, s, m, man))
        self._db_threads.append(t)
        t.start()

    def on_db_loaded(self, cache, status, message, manual):
        if cache:
            self.db_event_cache = cache
            log(f"[DB] 이벤트 {len(cache)}개 ({status}: {message})")
            text = tr("🟢 [DB 준비 완료] 이벤트 {}개").format(len(cache))
            if manual and status == "cooldown":
                text = tr("⏳ 수동 새로고침은 잠시 후 다시 가능합니다 (10분 쿨다운)")
            elif manual and status == "offline":
                text = tr("⚠️ 갱신 실패 - 저장된 DB를 사용합니다")
            self.status.set_status(text, "#3ddc84", protect_sec=4, flash=True)
            QTimer.singleShot(4200, lambda: self.status.set_status(self._journey_text() if self.in_journey else self._idle_text(), "#888888"))
        else:
            log(f"[DB] 없음 ({status}: {message})")
            self.status.set_status(tr("🔴 DB 없음 - 인터넷 연결 후 메뉴에서 새로고침"), "#ff5c5c", protect_sec=8 if manual else 0, flash=True)

    def refresh_db(self):
        self.start_db(manual=True)

    # --- 워처 콜백 ---
    def on_journey_status(self, active):
        if active and self.in_journey:
            return                                      # 이미 진입 상태: 중복 알림 무시
        self.in_journey = active
        log(f"[상태] 이벤트 화면 {'진입' if active else '이탈'}")
        if active:
            self.status.set_status(tr("🔵 [이벤트 확인 중] {}...").format(self.current_title or "..."), "#00aaff", flash=True)
        else:                                           # (1) 이벤트 화면 이탈 -> 오래된 상태 제거
            self.reset_event_state()
            self.status.set_status(self._idle_text(), "#888888")

    def on_title(self, titles, kind=None):
        """titles: OCR 후보 문자열 리스트(또는 문자열 하나), kind: 라벨 종류 'arcana'/'general'/None"""
        cands_txt = [titles] if isinstance(titles, str) else list(titles or [])
        title = cands_txt[0] if cands_txt else ""
        if title == self.current_title:
            return
        self.current_title, self.matched, self.zones, self.options, self.ambig = title, None, [], [], []
        self.not_found = ""
        self.tooltip.hide_and_clear()
        if len(jh_db.clean_title(title)) < 2 or not self.db_event_cache:
            return
        cands, amb = jh_db.match_title(cands_txt, self.db_event_cache, kind)
        if not cands:
            log(f"[매칭] {cands_txt} (라벨={kind}) -> DB에 없음")
            self.not_found = title
            self.status.set_status(tr("🟠 [DB에 없는 제목] {}").format(title), "#ff9800", flash=True)
            return
        best, score = cands[0]
        self.matched, self.options = best, best.get("options", [])
        self.ambig = amb if len(amb) > 1 else []
        weak = score < 0.65
        if self.ambig:
            text = tr("⚠️ [후보 {}개] {}").format(len(self.ambig), " / ".join(e["rawTitle"] for e in self.ambig))
        else:
            text = (tr("⚠️ [추정] {}") if weak else tr("🟢 [여정 진행 중] {}")).format(best["rawTitle"])
        self.status.set_status(text, "#ff9800" if (weak or self.ambig) else "#00e5ff", flash=True)
        run = f", 차점 '{cands[1][0]['rawTitle']}' ({cands[1][1]:.2f})" if len(cands) > 1 else ""
        log(f"[매칭] {cands_txt} -> '{best['rawTitle']}' ({score:.2f}){run}" + (f" 모호:{[e['rawTitle'] for e in self.ambig]}" if self.ambig else ""))
        self.zones = self.estimate_zones(len(self.options)) if len(self.options) >= 2 else []
        ids = jh_db.arcana_ids_of(best)
        if len(ids) >= 2 and not self.ambig:
            log(f"[카드] '{best['rawTitle']}' 아르카나 {len(ids)}곳에 있음 {ids} -> 카드 썸네일로 식별 시도 (그동안 전부 표시)")
            t = CardThread(title, ids)
            t.resolved.connect(self.on_card_resolved)
            self._card_threads = [x for x in self._card_threads if x.isRunning()] + [t]
            t.start()

    def on_card_resolved(self, title, aid, info):
        """카드 식별 결과: 아직 같은 제목이면 해당 아르카나 변형만 남긴다. 실패하면 지금처럼 전부 표시."""
        if title != self.current_title or not self.matched:
            return
        if aid is None:
            log(f"[카드] 식별 실패/모호 ({info}) -> 모든 아르카나 표시")
            return
        self.card_id = aid
        self.matched = jh_db.filter_event_by_arcana(self.matched, aid)
        self.options = self.matched.get("options", [])
        self.zones = self.estimate_zones(len(self.options)) if len(self.options) >= 2 else []
        log(f"[카드] 식별 성공: arcanaId={aid} ({info}) -> {self.matched.get('sources')} 만 표시")

    def _option_html(self, idx, option, scale, show_info):
        """선택지 idx 의 툴팁 HTML 생성 함수(detail -> html). 제목이 모호하면 후보 이벤트마다 같은 번호의 선택지를 제목 머리글과 함께 보여준다."""
        diff = self.settings.get("difficulty", "all")
        if self.ambig:
            items = [(e["rawTitle"], e["options"][idx]) for e in self.ambig if idx < len(e.get("options") or [])]
            if len(items) > 1:
                return lambda d=0: jh_db.format_ambiguous_html(items, scale, show_info, self.lang, diff, d)
        return lambda d=0: jh_db.format_option_html(option, scale, show_info, self.lang, diff, d)

    def estimate_zones(self, count):
        """선택지 버튼의 Y 범위를 게임 창 비율로 추정 (아래 기준선에서 위로 쌓임)."""
        rect = get_game_window_rect() or {"top": 0, "height": 1080}
        bottom, pitch, btn_h = rect["top"] + int(rect["height"] * 0.724), int(rect["height"] * 0.07037), int(rect["height"] * 0.063)
        zones = []
        for i, opt in enumerate(self.options[:count]):
            cy = bottom - (count - 1 - i) * pitch
            zones.append({"y_min": cy - btn_h // 2, "y_max": cy + btn_h // 2, "option": opt, "idx": i})
        return zones

    # --- 로딩 화면: 상태 초기화 후, 끝나면 라벨부터 다시 확인 (5) ---
    def poll_loading(self):
        if self._hidden or not is_game_hovered_or_active():
            return
        rect = get_game_window_rect()
        if not rect:
            return
        if self._sct is None:
            self._sct = mss.mss()
        now = is_loading_screen(self._sct, rect)
        self._load_hits = self._load_hits + 1 if now else 0
        self._load_miss = self._load_miss + 1 if not now else 0
        if not self._loading_screen and self._load_hits >= 2:
            self._loading_screen = True
            self.reset_event_state()
            self.status.set_status(tr("🌕 [로딩 중] 다음 화면으로 이동하는 중..."), "#ffcc00")
        elif self._loading_screen and self._load_miss >= 2:
            self._loading_screen = False
            self.watcher.request_reverify()
            self.status.set_status(self._idle_text(), "#888888")

    # --- 오버레이 유지: 게임이 다시 포커스를 받으면 최상단 재지정, 창이 움직였으면 위치 보정 ---
    def maintain_overlays(self):
        active = is_game_hovered_or_active()
        if active and not self._game_focus_was_active:
            reassert_topmost(self.status)
            reassert_topmost(self.tooltip)
        self._game_focus_was_active = active
        if not self._hidden:
            self.status.reposition()

    # --- 호버 ---
    def check_hover(self):
        if (self.not_found and not self._hidden and not self._loading_screen and self.in_journey
                and is_game_hovered_or_active()):
            self.tooltip.show_html(jh_db.format_not_found_html(self.not_found, self.settings["tooltip_font_scale"], self.lang))
            return
        if self._hidden or self._loading_screen or not self.in_journey or not self.matched or not is_game_hovered_or_active():
            self.tooltip.hide_and_clear()
            return
        scale, show_info = self.settings["tooltip_font_scale"], self.settings["show_info"]
        if len(self.options) == 1:                      # 선택지가 하나(자동 진행)면 호버 없이 바로 표시
            self.tooltip.show_html(self._option_html(0, self.options[0], scale, show_info))
            return
        rect, pos = get_game_window_rect(), QCursor.pos()
        if rect:
            x0, x1 = rect["left"] + int(rect["width"] * self.HOVER_X[0]), rect["left"] + int(rect["width"] * self.HOVER_X[1])
        else:
            x0, x1 = int(self.screen_w * self.HOVER_X[0]), int(self.screen_w * self.HOVER_X[1])
        if x0 <= pos.x() <= x1:
            for z in self.zones:
                if z["y_min"] <= pos.y() <= z["y_max"]:
                    self.tooltip.show_html(self._option_html(z["idx"], z["option"], scale, show_info))
                    return
        self.tooltip.hide_and_clear()

    # --- 메뉴: 좌클릭(상단바 조작) / 우클릭(설정) / 트레이(둘 다) ---
    def _toggle_collapsed(self):
        self.status.set_collapsed(not self.status.collapsed)

    def _toggle_hidden(self):
        self._hidden = not self._hidden
        if self._hidden:
            self.reset_event_state()
            self.status.hide()
        else:
            self.status.show()
            self.status.render()

    def build_main_menu(self):
        m = QMenu()
        m.addAction(tr("🔼 상단바 펼치기") if self.status.collapsed else tr("🔽 상단바 접기"), self._toggle_collapsed)
        m.addAction(tr("🔄 DB 새로고침 (10분 쿨다운)"), self.refresh_db)
        m.addAction(tr("▶️ 헬퍼 재개") if self._hidden else tr("⏸️ 헬퍼 일시정지"), self._toggle_hidden)
        m.addSeparator()
        m.addAction(tr("🛑 프로그램 종료"), self.quit_app)
        return m

    def build_settings_menu(self):
        m = QMenu()
        m.addAction(tr("⚙️ 설정")).setEnabled(False)
        scale_menu = m.addMenu(tr("🔤 툴팁 글자 크기"))
        group = QActionGroup(m)
        for label, val in ((tr("작게"), 0.85), (tr("보통"), 1.0), (tr("크게"), 1.25), (tr("아주 크게"), 1.5)):
            a = QAction(label, m, checkable=True)
            a.setChecked(abs(self.settings["tooltip_font_scale"] - val) < 0.01)
            a.triggered.connect(lambda _, v=val: self.set_scale(v))
            group.addAction(a)
            scale_menu.addAction(a)
        start_menu = m.addMenu(tr("🚀 시작 시 상단바"))
        g2 = QActionGroup(m)
        for label, val in ((tr("접힘"), "collapsed"), (tr("펼침"), "expanded")):
            a = QAction(label, m, checkable=True)
            a.setChecked(self.settings["startup_state"] == val)
            a.triggered.connect(lambda _, v=val: self.set_value("startup_state", v))
            g2.addAction(a)
            start_menu.addAction(a)
        diff_menu = m.addMenu(tr("🎚️ 난이도 표시"))
        g4 = QActionGroup(m)
        for label, val in DIFFICULTY_CHOICES:
            a = QAction(tr(label), m, checkable=True)
            a.setChecked(self.settings.get("difficulty", "all") == val)
            a.triggered.connect(lambda _, v=val: self.set_value("difficulty", v))
            g4.addAction(a)
            diff_menu.addAction(a)
        lang_menu = m.addMenu(tr("🌐 언어"))
        g3 = QActionGroup(m)
        for label, val in (("한국어", "ko"), ("English", "en")):
            a = QAction(label, m, checkable=True)
            a.setChecked(self.lang == val)
            a.triggered.connect(lambda _, v=val: self.on_language_selected(v))
            g3.addAction(a)
            lang_menu.addAction(a)
        for text, key in ((tr("잠재력/버프 설명 표시"), "show_info"), (tr("⚡ 저사양 모드 (OCR 주기 늘림)"), "low_spec_mode"),
                          (tr("상단바 위치 고정"), "status_badge_locked"),
                          (tr("🐛 상단바/툴팁을 스크린샷에 보이게 하기"), "show_capture_debug")):
            a = QAction(text, m, checkable=True)
            a.setChecked(bool(self.settings[key]))
            a.triggered.connect(lambda checked, k=key: self.set_value(k, bool(checked)))
            m.addAction(a)
        m.addAction(tr("상단바 위치 초기화"), self.reset_badge_pos)
        m.addSeparator()
        m.addAction(tr("현재 버전: {}").format(APP_VERSION)).setEnabled(False)
        return m

    def show_main_menu(self, pos):
        self.build_main_menu().exec_(pos)

    def show_settings_menu(self, pos):
        self.build_settings_menu().exec_(pos)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(tr("JourneyHelper가 실행 중입니다 (우클릭: 메뉴)"))
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._refresh_tray_menu)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.show()

    def _refresh_tray_menu(self):
        m = self._tray_menu
        m.clear()
        main, settings = self.build_main_menu(), self.build_settings_menu()
        self._tray_keep = [main, settings]            # 액션의 소유자(메뉴)를 살려 둬야 한다 (GC 되면 크래시)
        for a in main.actions():
            m.addAction(a)
        sub = m.addMenu(tr("⚙️ 설정"))
        for a in settings.actions():
            sub.addAction(a)

    def set_scale(self, v):
        self.set_value("tooltip_font_scale", v)
        self.tooltip.set_scale(v)

    def set_value(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)
        if key == "show_capture_debug":
            self._apply_capture_visibility()

    def _apply_capture_visibility(self):
        hide = not self.settings.get("show_capture_debug", False)
        for w in (self.status, self.tooltip):
            exclude_from_capture(w, hide)

    def reset_badge_pos(self):
        self.set_value("status_badge_offset", None)
        self.status.reposition()

    def on_language_selected(self, lang):
        """언어 변경: DB 캐시를 새 언어로 다시 만들어야 하므로 재시작 여부를 묻는다."""
        if lang == self.lang:
            return
        ans = QMessageBox.question(None, tr("재시작 필요"), tr(
            "언어를 바꾸면 여정 이벤트 데이터를 새 언어로 다시 불러와야 해요.\n지금 재시작할까요? (재시작 안 하면 일부 이벤트 인식이 이전 언어\n기준으로 남아있어 오작동할 수 있어요)"))
        if ans == QMessageBox.Yes:
            self.set_value("language", lang)
            log("[언어 변경] 새 언어로 다시 시작합니다.")
            self.restart_app()

    def restart_app(self):
        release_single_instance()
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        try:
            subprocess.Popen(cmd + [a for a in sys.argv[1:] if a.startswith("--")])
        except Exception as e:
            log(f"[재시작 실패] {e}")
            return
        self.quit_app()

    def quit_app(self):
        self.watcher.stop()
        self.watcher.wait(1500)
        self.tray.hide()
        QApplication.quit()


# ----------------------------------------------------------------------------
# Qt 플러그인 경로 우회
# 개발 환경(Windows 11, PyQt5 5.15.2)에서 설치 경로에 한글이 있으면 QApplication 생성이 멈추는 것을 확인했다.
# (예: C:\Users\...\바탕 화면\...) 이 경우 platforms/styles 플러그인을 ASCII 경로로 복사해서 거기서 읽게 한다.
# ----------------------------------------------------------------------------
def prepare_qt_plugins():
    try:
        import shutil
        import PyQt5
        # (Qt 쪽 QLibraryInfo 는 한글 경로를 '??' 로 깨뜨려 돌려주므로 파이썬 쪽 경로로 판단한다)
        src = os.path.join(getattr(sys, "_MEIPASS", None) or os.path.dirname(PyQt5.__file__), "PyQt5", "Qt5", "plugins") \
            if getattr(sys, "_MEIPASS", None) else os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
        if src.isascii() or not os.path.isdir(src):
            return None
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PUBLIC"), os.environ.get("ProgramData"),
                     (os.environ.get("SystemDrive") or "C:") + "\\"):
            if not base or not base.isascii():
                continue
            dst = os.path.join(base, APP_NAME, "qt_plugins")
            try:
                for sub in ("platforms", "styles"):
                    if os.path.isdir(os.path.join(src, sub)) and not os.path.isdir(os.path.join(dst, sub)):
                        shutil.copytree(os.path.join(src, sub), os.path.join(dst, sub))
                os.environ["QT_PLUGIN_PATH"] = dst
                os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(dst, "platforms")
                return dst
            except Exception:
                continue
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# 셀프테스트 / 진입점
# ----------------------------------------------------------------------------
def run_selftest(no_update=False, lang="ko"):
    """GUI/게임 없이: DB 로드(필요 시 갱신) -> 캐시 구성 -> 이벤트 수와 샘플 툴팁 HTML 출력."""
    store = jh_db.DbStore(user_agent=f"{APP_NAME}/{APP_VERSION} (selftest)", lang=lang)
    if not no_update:
        res = store.update()
        print(f"[selftest] update: {res.status} - {res.message}")
    journey, arcanas = store.load()
    if not journey:
        print("[selftest] 실패: 캐시된 DB가 없습니다")
        return 1
    cache = jh_db.build_event_cache(journey, arcanas, lang)
    arc = sum(1 for e in cache if e["isArcana"])
    print(f"[selftest] 이벤트 {len(cache)}개 (아르카나 {arc} / 일반 {len(cache) - arc}), 캐시 위치: {store.cache_dir}")
    sample = next((e for e in cache if len(e["options"]) >= 2 and any(o["info"] for o in e["options"])), cache[0])
    opt = next((o for o in sample["options"] if o["info"]), sample["options"][0])
    print(f"[selftest] 샘플 이벤트: {sample['rawTitle']}")
    print(jh_db.format_option_html(opt, lang=lang))
    try:                          # OCR 모델 로딩/추론 점검 (빈 이미지 -> 예외 없이 끝나면 OK)
        ocr = KoreanOCR()
        ocr.read_line(np.full((48, 300), 255, np.uint8))
        print("[selftest] OCR 모델 로딩 OK")
    except Exception as e:
        print(f"[selftest] OCR 모델 로딩 실패: {e}")
        return 2
    return 0


def main(argv):
    if "--out" in argv and argv.index("--out") + 1 < len(argv):      # 콘솔이 없는 exe 에서 결과 확인용
        sys.stdout = open(argv[argv.index("--out") + 1], "w", encoding="utf-8", buffering=1)
    if "--selftest" in argv:
        lang = argv[argv.index("--lang") + 1] if "--lang" in argv and argv.index("--lang") + 1 < len(argv) else "ko"
        sys.exit(run_selftest(no_update="--no-update" in argv, lang=lang if lang in jh_db.SUPPORTED_LANGS else "ko"))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # mss 물리 좌표와 Qt 좌표를 일치시킨다
    except Exception:
        pass
    if not acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(0, f"{APP_NAME}가 이미 실행 중입니다.\n트레이 아이콘을 확인하세요.", "이미 실행 중", 0x40)
        return 0
    install_crash_logger()
    set_language(load_settings()["language"])
    prepare_qt_plugins()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    manager = MouseHoverManager()      # noqa: F841 (참조 유지)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
