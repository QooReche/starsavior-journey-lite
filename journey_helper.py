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
import sys
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

import jh_db

try:
    import mss
    from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QCursor, QIcon, QPainter, QPixmap
    from PyQt5.QtWidgets import (QAction, QActionGroup, QApplication, QLabel, QMenu, QSystemTrayIcon,
                                 QVBoxLayout, QWidget)
except ImportError:  # --selftest 는 GUI 없이도 돌 수 있게
    mss = None
    QThread = object
    QWidget = object
    pyqtSignal = lambda *a, **k: None

APP_NAME = "JourneyHelperLite"
APP_VERSION = "1.0.6-lite.1"

DEFAULT_SETTINGS = {
    "tooltip_font_scale": 1.0,
    "show_info": True,              # 잠재력/버프 설명 표시
    "low_spec_mode": False,         # OCR 주기를 늘려 CPU 절약
    "status_badge_locked": True,
    "status_badge_offset": None,
}

# 게임 창 기준 비율 (원작 값 유지). 라벨은 '여정 이벤트' 문구가 있는 작은 띠.
REGION_FRACTIONS = {
    "event_title": {"top": 0.205, "left": 0.110, "width": 0.240, "height": 0.056},
    "event_label": {"top": 0.170, "left": 0.110, "width": 0.200, "height": 0.032},
}
EVENT_LABEL_PHRASES = ("여정이벤트", "훈련이벤트", "아르카나이벤트")
NOISE_TITLE_WORDS = ("여정이벤트", "훈련이벤트", "아르카나이벤트", "제목")
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
        with open(os.path.join(get_settings_dir(), "settings.json"), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return settings


def save_settings(settings):
    try:
        with open(os.path.join(get_settings_dir(), "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[설정 저장 실패] {e}")


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


def exclude_from_capture(widget):
    """오버레이가 화면 캡처(OCR)에 찍히지 않도록 한다."""
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)
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
def match_event_label(text, cutoff=0.8):
    """OCR 텍스트가 '여정 이벤트/훈련 이벤트/아르카나 이벤트' 전체 문구와 일치(또는 거의 일치)하면 문구를 반환.
    '이벤트' 같은 일반 단어만으로는 통과시키지 않는다 (장비/스탯 화면 오탐 방지)."""
    t = re.sub(r"[^가-힣]", "", text or "")
    if len(t) < 4:
        return None
    for p in EVENT_LABEL_PHRASES:
        if p in t:
            return p
    for p in EVENT_LABEL_PHRASES:
        for L in (len(p) - 1, len(p), len(p) + 1):
            for i in range(0, len(t) - L + 1):
                if difflib.SequenceMatcher(None, t[i:i + L], p).ratio() >= cutoff:
                    return p
    return None


def clean_ocr_title(text):
    t = re.sub(r"[^가-힣0-9a-zA-Z]", "", text or "")
    for w in NOISE_TITLE_WORDS:
        t = t.replace(w, "")
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


# ----------------------------------------------------------------------------
# 화면 감시 스레드: 라벨 확인 -> (확인된 경우에만) 제목 OCR
# ----------------------------------------------------------------------------
class ScreenWatcher(QThread):
    title_detected = pyqtSignal(str)
    journey_status_changed = pyqtSignal(bool)
    model_loading = pyqtSignal()
    model_ready = pyqtSignal()

    LEAVE_AFTER_OTHER = 3     # 라벨 자리에 다른 글자가 읽힘 -> 이벤트 화면이 아니라고 확신 (연속 횟수)
    LEAVE_AFTER_UNSEEN = 8    # 아무것도 못 읽음 -> 스캔 실패일 수 있으니 더 오래 기다림

    def __init__(self, pause_getter=None, low_spec_getter=None):
        super().__init__()
        self.pause_getter = pause_getter or (lambda: False)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self.ocr = None
        self._stop = False
        # (2) 실제로 확인되기 전까지는 항상 False
        self.label_visible = False
        self.on_event_screen = False
        self._reverify = True
        self._reset_scan_state()

    def _reset_scan_state(self):
        self._miss = 0
        self._last_label_gray = None
        self._label_state = "unseen"
        self._last_title_gray = None
        self._cached_title = ""
        self.last_title = ""

    def request_reverify(self):
        """(5) 로딩 화면 이후: 라벨을 다시 확인하기 전에는 제목 인식을 하지 않는다."""
        self._reverify = True

    def stop(self):
        self._stop = True

    def _leave_event_screen(self):
        """(1) 라벨이 사라졌거나 이벤트 화면을 떠남 -> 진행 상태 전부 초기화"""
        was = self.on_event_screen
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
        elif match_event_label(text):
            state = "found"
        else:
            state = "other"
        self._label_state = state
        return state

    def _scan_title(self, sct, rect):
        gray = cv2.cvtColor(np.array(sct.grab(region_to_monitor(REGION_FRACTIONS["event_title"], rect))), cv2.COLOR_BGRA2GRAY)
        last = self._last_title_gray
        limit = 3.0 if self.low_spec_getter() else 1.0
        if last is not None and last.shape == gray.shape and cv2.absdiff(gray, last).mean() < limit:
            return self._cached_title
        self._last_title_gray = gray
        text, conf = self.ocr.read_line(gray)
        self._cached_title = clean_ocr_title(text) if conf >= 0.4 else ""
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
                    self._leave_event_screen()
                    self.msleep(1000)
                    continue
                if self.ocr is None:
                    self.model_loading.emit()
                    self.ocr = KoreanOCR()
                    self.model_ready.emit()
                if self._reverify:
                    self._reverify = False
                    self.label_visible = False
                    self.on_event_screen = False      # 라벨을 다시 확인하면 상태 True 를 새로 알린다
                    self._reset_scan_state()

                state = self._scan_label(sct, rect)
                if state == "found":
                    self._miss = 0
                    self.label_visible = True
                    if not self.on_event_screen:
                        self.on_event_screen = True
                        self.journey_status_changed.emit(True)
                else:
                    self._miss += 1
                    limit = self.LEAVE_AFTER_OTHER if state == "other" else self.LEAVE_AFTER_UNSEEN
                    if self._miss >= limit and (self.label_visible or self.on_event_screen):
                        self._leave_event_screen()
                    if state == "other":
                        self.label_visible = False       # 다른 글자가 보이면 즉시 제목 인식 중단 (오탐 방지)

                if self.label_visible:
                    title = self._scan_title(sct, rect)
                    if len(title) >= 2 and not same_text(title, self.last_title):
                        self.last_title = title
                        print(f"[OCR] 제목: {title}")
                        self.title_detected.emit(title)
            except Exception as e:
                print(f"[ScreenWatcher 예외] {e}")
            self.msleep(350 if self.low_spec_getter() else 200)


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
            print(f"[DB 로딩 예외] {e}")
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
    """게임 창 위쪽 상태 배지. 좌클릭: 접기/펼치기, 우클릭: 메뉴, (잠금 해제 시) 드래그 이동."""

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
        self.collapsed = False
        self.on_menu = None
        self.on_toggle = None
        self._text, self._color = "준비 중...", "#ffcc00"
        self._protect_until = 0
        self._drag = None
        self._dragging = False
        self.render()
        self.show()
        exclude_from_capture(self)

    def set_status(self, text, color="#00e5ff", protect_sec=0):
        if time.time() < self._protect_until and not protect_sec:
            return
        self._protect_until = time.time() + protect_sec if protect_sec else 0
        self._text, self._color = text, color
        self.render()

    def render(self):
        text, color = ("🔽 (클릭하여 활성화)", "#888888") if self.collapsed else (self._text, self._color)
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

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton and self.on_menu:
            self.on_menu(e.globalPos())
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
        else:
            self.collapsed = not self.collapsed
            self.render()
            if self.on_toggle:
                self.on_toggle()
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
        self.set_scale(scale)
        exclude_from_capture(self)

    def set_scale(self, scale):
        self.scale = scale
        self.label.setStyleSheet(
            f"QLabel {{ background-color: rgba(10,12,18,250); border: 2px solid #00e5ff; border-radius: 12px; "
            f"color: #ffffff; font-size: {max(9, round(self.BASE_PX * scale))}px; font-weight: bold; padding: 14px 18px; }}")
        self.setFixedWidth(round(self.BASE_W * scale))

    def show_html(self, html):
        if self.label.text() != html:
            self.label.setText(html)
            self.label.adjustSize()
            self.adjustSize()
            self.resize(self.sizeHint())
        rect = get_game_window_rect()
        screen = QApplication.desktop().screenGeometry()
        x, y = (rect["left"] + 25, rect["top"] + int(rect["height"] * 0.30)) if rect else (25, int(screen.height() * 0.30))
        y = max(0, min(y, screen.height() - self.height() - 10))      # 내용이 길면 위로 밀어 화면 안에 유지
        self.move(x, y)
        self.show()

    def hide_and_clear(self):
        if self.isVisible():
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
        self.screen_w = QApplication.desktop().screenGeometry().width()
        self.db_event_cache = []
        self.store = jh_db.DbStore(user_agent=f"{APP_NAME}/{APP_VERSION} (+https://github.com/QooReche/starsavior-journey-lite)")
        self._hidden = False
        self._loading_screen = False
        self._load_hits = self._load_miss = 0
        self._db_threads = []
        self.reset_event_state()

        self.status = StatusBadge(self.settings)
        self.status.on_menu = self.show_menu
        self.status.on_toggle = self._on_badge_toggle
        self.tooltip = FixedLeftTooltip(self.settings["tooltip_font_scale"])
        self._build_menu()
        self._build_tray()

        self.watcher = ScreenWatcher(pause_getter=self._is_paused, low_spec_getter=lambda: self.settings["low_spec_mode"])
        self.watcher.title_detected.connect(self.on_title)
        self.watcher.journey_status_changed.connect(self.on_journey_status)
        self.watcher.model_loading.connect(lambda: self.status.set_status("🟡 OCR 모델 로딩 중... (처음 한 번)", "#ffcc00"))
        self.watcher.model_ready.connect(lambda: self.status.set_status(self._idle_text(), "#888888"))
        self.watcher.start()

        self.hover_timer = QTimer(self)
        self.hover_timer.timeout.connect(self.check_hover)
        self.hover_timer.start(60)
        self.loading_timer = QTimer(self)
        self.loading_timer.timeout.connect(self.poll_loading)
        self.loading_timer.start(300)
        self._sct = None

        self.status.set_status("🟡 DB 불러오는 중...", "#ffcc00")
        self.start_db(manual=False)

    # --- 상태 ---
    def reset_event_state(self):
        self.in_journey = False
        self.current_title = ""
        self.matched = None
        self.zones = []
        self.options = []
        if hasattr(self, "tooltip"):
            self.tooltip.hide_and_clear()

    def _on_badge_toggle(self):
        self.reset_event_state()
        self.watcher.request_reverify()

    def _idle_text(self):
        return "⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다..."

    def _is_paused(self):
        return self.status.collapsed or self._hidden

    # --- DB ---
    def start_db(self, manual):
        t = DbThread(self.store, manual=manual)
        t.finished_ok.connect(lambda c, s, m, man=manual: self.on_db_loaded(c, s, m, man))
        self._db_threads.append(t)
        t.start()

    def on_db_loaded(self, cache, status, message, manual):
        if cache:
            self.db_event_cache = cache
            print(f"[DB] 이벤트 {len(cache)}개 ({status}: {message})")
            text = f"✅ DB {len(cache)}개 이벤트" + (" (갱신됨)" if status == "updated" else "")
            if manual and status == "cooldown":
                text = "⏳ " + message
            elif manual and status == "offline":
                text = "⚠️ 갱신 실패 - 저장된 DB 사용 중"
            self.status.set_status(text, "#3ddc84", protect_sec=4)
            QTimer.singleShot(4200, lambda: self.status.set_status(self._idle_text() if not self.in_journey else self._journey_text(), "#888888"))
        else:
            self.status.set_status("🔴 DB 없음 - 인터넷 연결 후 메뉴에서 새로고침", "#ff5c5c", protect_sec=8 if manual else 0)

    def refresh_db(self):
        self.start_db(manual=True)

    # --- 워처 콜백 ---
    def on_journey_status(self, active):
        self.in_journey = active
        if active:
            self.status.set_status(f"🔵 [이벤트 확인 중] {self.current_title or '...'}", "#00aaff")
        else:                                           # (1) 이벤트 화면 이탈 -> 오래된 상태 제거
            self.reset_event_state()
            self.status.set_status(self._idle_text(), "#888888")

    def _journey_text(self):
        return f"🟢 [여정 진행 중] {self.matched['rawTitle']}" if self.matched else f"🔵 [이벤트 확인 중] {self.current_title}"

    def on_title(self, title):
        if title == self.current_title:
            return
        self.current_title, self.matched, self.zones, self.options = title, None, [], []
        self.tooltip.hide_and_clear()
        q = re.sub(r"[^가-힣0-9]", "", title)
        if len(q) < 2 or not self.db_event_cache:
            return
        best, score = jh_db.find_best_event(q, self.db_event_cache)
        if not best:
            self.status.set_status(f"🟠 [DB에 없는 제목] {title}", "#ff9800")
            return
        self.matched, self.options = best, best.get("options", [])
        weak = score < 0.5
        self.status.set_status(("⚠️ [추정] " if weak else "🟢 [여정 진행 중] ") + best["rawTitle"], "#ff9800" if weak else "#00e5ff")
        print(f"[매칭] '{title}' -> '{best['rawTitle']}' ({score:.2f})")
        self.zones = self.estimate_zones(len(self.options)) if len(self.options) >= 2 else []

    def estimate_zones(self, count):
        """선택지 버튼의 Y 범위를 게임 창 비율로 추정 (아래 기준선에서 위로 쌓임)."""
        rect = get_game_window_rect() or {"top": 0, "height": 1080}
        bottom, pitch, btn_h = rect["top"] + int(rect["height"] * 0.724), int(rect["height"] * 0.07037), int(rect["height"] * 0.063)
        zones = []
        for i, opt in enumerate(self.options[:count]):
            cy = bottom - (count - 1 - i) * pitch
            zones.append({"y_min": cy - btn_h // 2, "y_max": cy + btn_h // 2, "option": opt})
        return zones

    # --- 로딩 화면: 상태 초기화 후, 끝나면 라벨부터 다시 확인 (5) ---
    def poll_loading(self):
        if self._is_paused() or not is_game_hovered_or_active():
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
            self.status.set_status("🌕 [로딩 중] 다음 화면으로 이동하는 중...", "#ffcc00")
        elif self._loading_screen and self._load_miss >= 2:
            self._loading_screen = False
            self.watcher.request_reverify()
            self.status.set_status(self._idle_text(), "#888888")

    # --- 호버 ---
    def check_hover(self):
        if self._is_paused() or self._loading_screen or not self.in_journey or not self.matched:
            self.tooltip.hide_and_clear()
            return
        if not is_game_hovered_or_active():
            self.tooltip.hide_and_clear()
            return
        scale, show_info = self.settings["tooltip_font_scale"], self.settings["show_info"]
        if len(self.options) == 1:                      # 선택지가 하나(자동 진행)면 호버 없이 바로 표시
            self.tooltip.show_html(jh_db.format_option_html(self.options[0], scale, show_info))
            return
        rect, pos = get_game_window_rect(), QCursor.pos()
        if rect:
            x0, x1 = rect["left"] + int(rect["width"] * self.HOVER_X[0]), rect["left"] + int(rect["width"] * self.HOVER_X[1])
        else:
            x0, x1 = int(self.screen_w * self.HOVER_X[0]), int(self.screen_w * self.HOVER_X[1])
        if x0 <= pos.x() <= x1:
            for z in self.zones:
                if z["y_min"] <= pos.y() <= z["y_max"]:
                    self.tooltip.show_html(jh_db.format_option_html(z["option"], scale, show_info))
                    return
        self.tooltip.hide_and_clear()

    # --- 메뉴 / 트레이 ---
    def _build_menu(self):
        m = QMenu()
        m.addAction("DB 새로고침 (10분 쿨다운)", self.refresh_db)
        scale_menu = m.addMenu("툴팁 글자 크기")
        group = QActionGroup(m)
        for label, val in (("작게", 0.85), ("보통", 1.0), ("크게", 1.25), ("아주 크게", 1.5)):
            a = QAction(label, m, checkable=True)
            a.setChecked(abs(self.settings["tooltip_font_scale"] - val) < 0.01)
            a.triggered.connect(lambda _, v=val: self.set_scale(v))
            group.addAction(a)
            scale_menu.addAction(a)
        for text, key in (("잠재력/버프 설명 표시", "show_info"), ("저사양 모드 (OCR 주기 늘림)", "low_spec_mode"),
                          ("상단 배지 위치 고정", "status_badge_locked")):
            a = QAction(text, m, checkable=True)
            a.setChecked(bool(self.settings[key]))
            a.triggered.connect(lambda checked, k=key: self.set_flag(k, checked))
            m.addAction(a)
        m.addAction("배지 위치 초기화", self.reset_badge_pos)
        m.addSeparator()
        m.addAction(f"{APP_NAME} {APP_VERSION}").setEnabled(False)
        m.addAction("종료", self.quit_app)
        self.menu = m

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
        self.tray.setContextMenu(self.menu)
        self.tray.show()

    def show_menu(self, global_pos):
        self.menu.exec_(global_pos)

    def set_scale(self, v):
        self.settings["tooltip_font_scale"] = v
        self.tooltip.set_scale(v)
        save_settings(self.settings)

    def set_flag(self, key, value):
        self.settings[key] = bool(value)
        save_settings(self.settings)

    def reset_badge_pos(self):
        self.settings["status_badge_offset"] = None
        save_settings(self.settings)
        self.status.reposition()

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
def run_selftest(no_update=False):
    """GUI/게임 없이: DB 로드(필요 시 갱신) -> 캐시 구성 -> 이벤트 수와 샘플 툴팁 HTML 출력."""
    store = jh_db.DbStore(user_agent=f"{APP_NAME}/{APP_VERSION} (selftest)")
    if not no_update:
        res = store.update()
        print(f"[selftest] update: {res.status} - {res.message}")
    journey, arcanas = store.load()
    if not journey:
        print("[selftest] 실패: 캐시된 DB가 없습니다")
        return 1
    cache = jh_db.build_event_cache(journey, arcanas)
    arc = sum(1 for e in cache if e["isArcana"])
    print(f"[selftest] 이벤트 {len(cache)}개 (아르카나 {arc} / 일반 {len(cache) - arc}), 캐시 위치: {store.cache_dir}")
    sample = next((e for e in cache if len(e["options"]) >= 2 and any(o["info"] for o in e["options"])), cache[0])
    opt = next((o for o in sample["options"] if o["info"]), sample["options"][0])
    print(f"[selftest] 샘플 이벤트: {sample['rawTitle']}")
    print(jh_db.format_option_html(opt))
    return 0


def main(argv):
    if "--selftest" in argv:
        sys.exit(run_selftest(no_update="--no-update" in argv))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # mss 물리 좌표와 Qt 좌표를 일치시킨다
    except Exception:
        pass
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{APP_NAME}_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.user32.MessageBoxW(0, f"{APP_NAME}가 이미 실행 중입니다.\n트레이 아이콘을 확인하세요.", "이미 실행 중", 0x40)
        return 0
    install_crash_logger()
    prepare_qt_plugins()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    manager = MouseHoverManager()      # noqa: F841 (참조 유지)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
