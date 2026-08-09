import sys
import os
import traceback

# ⚠️ [PyTorch + OpenCV OpenMP 충돌 방지] torch와 opencv가 각자 다른 OpenMP 런타임 DLL을
# 들고 있어서, exe로 패키징하면 둘 다 같은 폴더에 들어가며 충돌해 "DLL 초기화 루틴을
# 실행할 수 없습니다" 에러가 나는 경우가 많다. torch/cv2를 import하기 전에 이 환경변수를
# 설정해두면 중복 로드를 허용해서 이 문제를 막을 수 있다. (아래 import보다 반드시 먼저 와야 함)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# 🔐 [SSL 인증서 경로 고정] 일부 유저 PC(회사망/백신의 SSL 검사, 손상된 루트 인증서 저장소 등)에서
# exe가 시스템 인증서 저장소를 제대로 못 찾아 "certificate verify failed: unable to get local
# issuer certificate" 에러로 OCR 모델 다운로드(easyocr, requests 등 모든 HTTPS 요청)가 실패하는
# 사례가 있다. certifi가 제공하는 인증서 번들 경로를 SSL/requests가 보게 될 환경변수에 명시적으로
# 심어두면, 시스템 인증서 저장소 상태와 무관하게 항상 검증에 성공한다.
# (반드시 easyocr/requests 등 네트워크 관련 모듈을 import하기 전에 와야 함)
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    # certifi가 없는 극히 예외적인 환경이면 그냥 기존 방식(시스템 인증서)으로 폴백
    pass

import time
import datetime
import random
import cv2
import numpy as np
import easyocr
import re
import json
import html as html_escape
import ctypes
from ctypes import wintypes
import mss
import warnings

import difflib
import statistics
import requests
import io
import urllib.parse
import concurrent.futures
from PIL import Image
import imagehash
import torch
import builtins

_original_print = builtins.print

def _timestamped_print(*args, **kwargs):
    current_time = datetime.datetime.now().strftime("[%H:%M:%S.%f]")[:-3] + "]"
    _original_print(current_time, *args, **kwargs)

builtins.print = _timestamped_print

# 불필요한 시스템 경고 콘솔 난립 억제 필터
warnings.filterwarnings('ignore')

# CPU 모드 전용 스레드 수 제어 (기본 코어의 절반 이하로 제한하여 시스템 먹통 방지)
torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
# ⚡ [최적화 모드 연동] OpenCV도 자체 스레드풀을 쓰므로 torch와 함께 제한.
# 실제 값은 set_low_spec_mode()에서 최적화 모드 on/off에 따라 다시 조절됨.
cv2.setNumThreads(max(1, (os.cpu_count() or 4) // 2))

from PyQt5.QtCore import QUrl, Qt, QThread, pyqtSignal, QTimer, QRect, QPoint, QPropertyAnimation, QEasingCurve
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSystemTrayIcon,
                              QMenu, QAction, QDialog, QCheckBox, QPushButton, QFrame, QComboBox, QLineEdit, QTabWidget,
                              QMessageBox)
from PyQt5.QtGui import QCursor, QImage, QPixmap, QPainter, QPen, QColor, QFont, QIcon, QBrush, QFontMetrics

# ----------------------------------------------------
# 1. 3D 카드 원근 정밀 보정 (Perspective Transformation)
# ----------------------------------------------------
def _order_corner_points(pts):
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).flatten()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return np.float32([tl, tr, br, bl])

def _detect_card_corners(cv_bgr_img):
    try:
        hsv = cv2.cvtColor(cv_bgr_img, cv2.COLOR_BGR2HSV)
        lower = np.array([8, 60, 30])
        upper = np.array([35, 255, 200])
        mask = cv2.inRange(hsv, lower, upper)
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
        mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours: return None, "윤곽선 자체가 하나도 없음"

        h, w = mask.shape[:2]
        img_area = w * h
        best, best_area = None, 0
        rejected_by_area, rejected_by_aspect = 0, 0
        rejected_aspect_values = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < img_area * 0.08 or area > img_area * 0.75:
                rejected_by_area += 1
                continue
            rect = cv2.minAreaRect(c)
            rw, rh = rect[1]
            if rw == 0 or rh == 0: continue
            aspect = max(rw, rh) / min(rw, rh)
            if not (1.2 <= aspect <= 2.6):
                rejected_by_aspect += 1
                rejected_aspect_values.append(round(aspect, 2))
                continue
            fill_ratio = area / (rw * rh) if (rw * rh) > 0 else 0
            if fill_ratio < 0.45: continue
            if rw > w * 0.97 or rh > h * 0.97: continue

            if area > best_area:
                best_area = area
                best = rect
        if best is None:
            reason = f"조건 통과한 후보 없음 (면적탈락:{rejected_by_area}, 비율탈락:{rejected_by_aspect} {rejected_aspect_values}, 전체윤곽선:{len(contours)})"
            return None, reason
        box = cv2.boxPoints(best)
        raw_min_x, raw_max_x = box[:, 0].min(), box[:, 0].max()
        raw_min_y, raw_max_y = box[:, 1].min(), box[:, 1].max()
        diag = f"rect중심={best[0]}, 크기={best[1]}, 각도={best[2]:.1f}, 클램프전 범위=x[{raw_min_x:.1f}~{raw_max_x:.1f}] y[{raw_min_y:.1f}~{raw_max_y:.1f}] (이미지 크기={w}x{h})"

        box[:, 0] = np.clip(box[:, 0], 0, w - 1)
        box[:, 1] = np.clip(box[:, 1], 0, h - 1)
        return _order_corner_points(box), diag
    except Exception as e:
        return None, f"예외 발생: {e}"

_last_deskew_log_time = 0

def _analytic_fallback_corners(w, h):
    native_w, native_h = 100.0, 150.0
    theta = np.radians(-12.0)  
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    local_corners = np.array([
        [-native_w / 2, -native_h / 2],
        [ native_w / 2, -native_h / 2],
        [ native_w / 2,  native_h / 2],
        [-native_w / 2,  native_h / 2],
    ])
    rot = np.array([[cos_t, -sin_t], [sin_t, cos_t]])
    rotated = local_corners @ rot.T
    rb_w = rotated[:, 0].max() - rotated[:, 0].min()
    rb_h = rotated[:, 1].max() - rotated[:, 1].min()
    scale = min(w / rb_w, h / rb_h) * 0.94  
    scaled = rotated * scale
    center = np.array([w / 2.0, h / 2.0])
    return np.float32(scaled + center)

def deskew_tilted_card(cv_bgr_img):
    global _last_deskew_log_time
    h, w = cv_bgr_img.shape[:2]

    detected, info = _detect_card_corners(cv_bgr_img)
    now = time.time()
    if detected is not None:
        src_pts = detected
        if now - _last_deskew_log_time > 5:
            _last_deskew_log_time = now
            print(f"🟢 [보정] 색상 기반 카드 모서리 감지 성공 — {info}")
    else:
        if now - _last_deskew_log_time > 5:
            _last_deskew_log_time = now
            print(f"🟡 [보정] 색상 기반 감지 실패({info}) → 수학적 추정 좌표로 폴백")
        src_pts = _analytic_fallback_corners(w, h)
    
    dst_pts = np.float32([
        [0, 0],
        [100, 0],
        [100, 150],
        [0, 150]
    ])
    
    M = cv2.getPerspectiveTransform(src_pts, dst_pts)
    warped = cv2.warpPerspective(cv_bgr_img, M, (100, 150))
    return warped

def extract_card_features(cv_bgr_img, apply_deskew=True):
    if apply_deskew:
        flat_card = deskew_tilted_card(cv_bgr_img)
    else:
        h0, w0 = cv_bgr_img.shape[:2]
        target_aspect = 100 / 150
        crop_h = int(w0 / target_aspect)
        if 0 < crop_h < h0:
            cv_bgr_img = cv_bgr_img[0:crop_h, :]
        flat_card = cv2.resize(cv_bgr_img, (100, 150), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(flat_card, cv2.COLOR_BGR2GRAY)
    eq_gray = cv2.equalizeHist(gray)
    phash_val = imagehash.phash(Image.fromarray(eq_gray))

    hsv = cv2.cvtColor(flat_card, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [18, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)

    return phash_val, hist, flat_card

KNOWN_NON_CARD_HASHES = [
    imagehash.ImageHash(np.array([
        [True, True, False, False, False, False, False, False],
        [False, True, True, False, False, True, True, False],
        [True, True, True, False, True, False, False, False],
        [False, False, True, True, True, False, True, True],
        [False, False, True, True, True, True, True, True],
        [True, False, False, True, False, True, True, False],
        [True, False, False, True, False, False, True, True],
        [True, True, False, False, False, False, False, True],
    ], dtype=bool))
]
NON_CARD_HASH_THRESHOLD = 8
EXCLUDED_CARD_NAMES = {"별빛의 인도자"}


# ----------------------------------------------------
# 2. OCR 범용 오차 보정 사전
# ----------------------------------------------------
def dedupe_repeated_chars(text):
    if not text: return text
    return re.sub(r'(.)\1+', r'\1', text)

def fix_ocr_typos(text):
    if not text:
        return ""
    
    typo_map = {
        "살롱인 어프터눈": "살롱 인 더 애프터눈", "살롱인 애프터눈": "살롱 인 더 애프터눈",
        "살롱인어프터눈": "살롱 인 더 애프터눈", "살롱인애프터눈": "살롱 인 더 애프터눈",
        "살롱 인 애프터눈": "살롱 인 더 애프터눈", "살롱 인 어프터눈": "살롱 인 더 애프터눈",
        
        "서류 작업은 언제나 맑은 뒤 흐름": "서류 작업은 언제나 맑은 뒤 흐림",
        "서류작업은언제나맑은뒤흐름": "서류작업은언제나맑은뒤흐림",
        "맑음뒤흔림": "맑은 뒤 흐림", "맑은뒤흔림": "맑은 뒤 흐림", "맑음뒤흐름": "맑은 뒤 흐림",
        "맑은뒤흐름": "맑은 뒤 흐림", "맑음 뒤 흔림": "맑은 뒤 흐림", "맑은 뒤 흔림": "맑은 뒤 흐림",
        "서류 작업은 언제나 맑음뒤흔림": "서류 작업은 언제나 맑은 뒤 흐림",
        
        "정체불명의상인": "정체불명의 상인", "정체불명상인": "정체불명의 상인",
        "공격에 도움이 되는 비술서": "공격에 도움이 되는 비술서.",
        "생존에 도움이 되는 비술서": "생존에 도움이 되는 비술서.",
        "모든 상황에 도움이 되는 비술서도 있어": "모든 상황에 도움이 되는 비술서도 있어?",
        
        "리세트의일과": "리세트의 일과", "리세트일과": "리세트의 일과",
        
        "없드": "없다", "없트": "없다", "없디": "없다", "없더": "없다",
        "있드": "있다", "있트": "있다", "있디": "있다",
        "했드": "했다", "됐드": "됐다", "왔드": "왔다", "갔드": "갔다",
        "보드": "보다", "주드": "주다", "한드": "한다", "된드": "된다",
        "선택한드": "선택한다", "맡긴드": "맡긴다", "받는드": "받는다",
        
        "살렁": "살롱", "실렁": "살롱", "샇롱": "살롱", "샬롱": "살롱",
        "훌름": "흐림", "예기하고": "얘기하고", "예기": "얘기", 
        "잘못햇": "잘못했", "이렇계": "이렇게", "붚쌍한": "불쌍한",
        "불상한": "불쌍한", "무보자": "가보자", "해조자": "해보자",
        "이려냐간": "이어나간다", "훈련올": "훈련을", "척이랴도": "척이라도",
        "티타임올": "티타임을", "중짓다": "좋겠다", "좋짓다": "좋겠다"
    }
    
    for wrong, right in typo_map.items():
        text = text.replace(wrong, right)
        
    text = re.sub(r'([가-힣]{2,})드(\s|$)', r'\1다\2', text)
    return text


# ----------------------------------------------------
# 3. 게임 창 확인 및 UI 오버레이
# ----------------------------------------------------
NOISE_EVENT_WORDS = {
    "여정", "이벤트", "이멘트", "이벳트", "이멘드", "이벤드",
    "노아", "NOA", "아르카나", "알카나", "알카", "아드가나", "아드카나", "아르가나"
}
TOOLTIP_NOISE = ["보상 정보", "찾는 중", "선택지", "마우스를", "고르자..."]

_GAME_KEYWORDS = ["스타세이비어", "스타 세이비어", "starsavior", "star savior"]
_cached_game_hwnd = None

_BROWSER_WINDOW_CLASSES = {
    "chrome_widgetwin_1",      
    "mozillawindowclass",       
    "applicationframewindow",   
    "operawindowclass",         
}

def _find_game_hwnd():
    global _cached_game_hwnd
    try:
        user32 = ctypes.windll.user32
        if _cached_game_hwnd and user32.IsWindow(_cached_game_hwnd):
            return _cached_game_hwnd

        my_pid = os.getpid()
        found = {"hwnd": None}
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0 and user32.IsWindowVisible(hwnd):
                owner_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                if owner_pid.value == my_pid:
                    return True

                cls_buff = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buff, 256)
                if cls_buff.value.lower() in _BROWSER_WINDOW_CLASSES:
                    return True

                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.lower()
                if any(k.lower() in title for k in _GAME_KEYWORDS):
                    found["hwnd"] = hwnd
                    return False
            return True

        user32.EnumWindows(EnumWindowsProc(_callback), 0)
        _cached_game_hwnd = found["hwnd"]
        return _cached_game_hwnd
    except Exception:
        return None

WDA_EXCLUDEFROMCAPTURE = 0x00000011
WDA_NONE = 0x00000000

def exclude_window_from_capture(widget):
    try:
        hwnd = int(widget.winId())
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        if not ok:
            pass
    except Exception:
        pass

def set_window_capture_visible_for_debug(widget, visible_in_capture):
    try:
        hwnd = int(widget.winId())
        affinity = WDA_NONE if visible_in_capture else WDA_EXCLUDEFROMCAPTURE
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity)
    except Exception:
        pass

def get_game_window_rect():
    try:
        hwnd = _find_game_hwnd()
        if not hwnd: return None
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)): return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0: return None
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return {"left": pt.x, "top": pt.y, "width": w, "height": h}
    except Exception:
        return None

def _create_tray_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QBrush(QColor("#00e5ff")))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 56, 56)
    painter.setPen(QColor("#0a0e18"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(30)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignCenter, "J")
    painter.end()
    return QIcon(pix)

_SINGLE_INSTANCE_MUTEX_NAME = "Global\\JourneyHelper_SingleInstance_Mutex"
_single_instance_mutex_handle = None

def ensure_single_instance():
    global _single_instance_mutex_handle
    try:
        _single_instance_mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        ERROR_ALREADY_EXISTS = 183
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        return True

# ----------------------------------------------------
# ⚙️ 설정 저장/불러오기 (상단 배지 우클릭 → 설정창)
# ----------------------------------------------------
APP_VERSION = "1.0.6"

DEFAULT_SETTINGS = {
    "low_spec_mode": False,  # ⚡ [최적화/CPU 절약 모드] 기본값 OFF
    "file_logging": False,
    "show_card_preview": False,
    "show_region_debug": False,
    "card_region_override": None,
    "tooltip_font_scale": 1.0,
    "hidden_difficulties": [],
    "startup_state": "expanded",
    "birthday_notify_enabled": True,
    "announce_no_birthday": True,
    "greeting_enabled": True,
    "custom_greetings": [],
    "user_birthday_month": 0,
    "user_birthday_day": 1,
    "custom_birthday_messages": [],
    "try_gpu_ocr": True,  # 기존 방식(GPU 우선 시도, 실패 시 CPU 폴백) 유지가 기본값.
                            # ⚠️ 드물게 게임이 GPU를 붙잡은 타이밍과 겹쳐 드라이버 레벨에서
                            # 죽는(=프로그램이 조용히 종료되는) 사례가 보고됨 — 그런 증상이
                            # 있으면 설정에서 이 옵션을 꺼서 CPU 강제로 우회 가능.
    "status_badge_locked": True,
    "auto_expand_on_launch": False,
    "auto_expand_include_hidden": False,
}

AVAILABLE_DIFFICULTIES = ["이지", "노말", "하드"]

GREETING_TEMPLATES = [
    "👋 안녕하세요 단장님!",
    "👋 오늘도 화이팅이에요, 단장님!",
    "👋 단장님, 어서오세요!",
    "👋 반가워요 단장님~",
    "👋 좋은 하루예요, 단장님!",
]
MY_BIRTHDAY_TEMPLATES = [
    "🎉 오늘은 단장님의 생일이에요! 축하드려요!",
    "🎂 생일 축하드려요 단장님! 오늘 하루 행복하세요~",
    "🎉 단장님, 태어나주셔서 감사해요! 생일 축하합니다!",
    "🎂 짜잔! 오늘은 단장님 생일이네요. 축하해요!",
    "🎉 오늘 같은 날엔 단장님이 주인공! 생일 축하드려요~",
]

def get_settings_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "JourneyHelper")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path

def get_settings_path():
    return os.path.join(get_settings_dir(), "settings.json")

def get_log_path():
    return os.path.join(get_settings_dir(), "log.txt")

def get_crash_log_path():
    return os.path.join(get_settings_dir(), "crash_log.txt")

# 🩹 [크래시 로깅] PyQt5는 5.5부터 슬롯(타이머 콜백 등) 안에서 잡히지 않은 예외가 나면
# 기본적으로 크래시 창도 없이 프로세스를 즉시 abort() 시켜버린다. sys.excepthook을 직접
# 설치해두면 이 기본 동작이 비활성화되어, 앱이 죽는 대신 예외 내용이 crash_log.txt에 남고
# 프로그램은 계속 살아있게 된다. "file_logging" 설정과 무관하게 항상 켜둔다 —
# 죽는 순간의 원인을 못 남기면 진단 자체가 불가능하기 때문.
def install_crash_logger():
    crash_log_path = get_crash_log_path()

    def _log_uncaught_exception(exc_type, exc_value, exc_tb):
        try:
            with open(crash_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== [예외 발생] {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _log_uncaught_exception
    return crash_log_path

def load_settings():
    path = get_settings_path()
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            settings.update(saved)
    except Exception:
        pass
    return settings

def save_settings(settings):
    try:
        with open(get_settings_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ [설정 저장 실패] {e}")
        return False


class _TeeStream:
    def __init__(self, file_path):
        self._file = open(file_path, "a", encoding="utf-8", buffering=1)
    def write(self, s):
        try:
            self._file.write(s)
        except Exception:
            pass
    def flush(self):
        try:
            self._file.flush()
        except Exception:
            pass

def enable_file_logging():
    try:
        log_path = get_log_path()
        stream = _TeeStream(log_path)
        sys.stdout = stream
        sys.stderr = stream
        print(f"\n===== [로그 시작] {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
        return log_path
    except Exception as e:
        return None

def compute_region(frac, screen_w, screen_h):
    base = get_game_window_rect()
    if base:
        return {
            "top": base["top"] + int(base["height"] * frac["top"]),
            "left": base["left"] + int(base["width"] * frac["left"]),
            "width": max(1, int(base["width"] * frac["width"])),
            "height": max(1, int(base["height"] * frac["height"])),
        }
    return {
        "top": int(screen_h * frac["top"]),
        "left": int(screen_w * frac["left"]),
        "width": int(screen_w * frac["width"]),
        "height": int(screen_h * frac["height"]),
    }

def is_loading_screen_visible(game_rect=None, screen_w=1920, screen_h=1080, sct=None):
    try:
        if game_rect:
            monitor = {"top": game_rect["top"], "left": game_rect["left"], "width": game_rect["width"], "height": game_rect["height"]}
        else:
            monitor = {"top": 0, "left": 0, "width": screen_w, "height": screen_h}
        if sct is not None:
            img = np.array(sct.grab(monitor))
        else:
            with mss.mss() as _sct:
                img = np.array(_sct.grab(monitor))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        small = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA)
        bright_ratio = float(np.mean(small > 200))
        mean_brightness = float(np.mean(small))
        return bright_ratio > 0.50 and mean_brightness > 180
    except Exception:
        return False

#영역 기본값
REGION_FRACTIONS = {
    "card_art":    {"top": 0.120, "left": 0.050, "width": 0.052, "height": 0.163},
    "event_title": {"top": 0.205, "left": 0.110, "width": 0.240, "height": 0.056},
    "arcana_label":{"top": 0.170, "left": 0.110, "width": 0.200, "height": 0.032},
}
_DEFAULT_CARD_ART_FRACTION = dict(REGION_FRACTIONS["card_art"])
_DEFAULT_ARCANA_LABEL_FRACTION = dict(REGION_FRACTIONS["arcana_label"])
_DEFAULT_EVENT_TITLE_FRACTION = dict(REGION_FRACTIONS["event_title"])
_card_region_manual_override = False
_auto_calibration_done = False
_auto_calibration_attempts = 0
_last_calibrated_rect = None  
_calibration_samples = []  
_CALIBRATION_SAMPLES_NEEDED = 1

# 🛑 [보정 결과 검증] 자동 보정이 계산한 영역이 상식적인 범위를 벗어나면
# (예: 화면 속 엉뚱한 텍스트를 라벨/제목으로 오인식해서 좌표가 통째로 틀어진 경우)
# 적용하지 않고 거부한다. 기준은 일부러 타이트하게 잡음 — 정상적인 해상도/창
# 배치 차이는 이 정도로 크게 벌어지지 않고, 오히려 잘못된 값이 그대로 적용돼서
# 화면 전체를 뒤덮는 부작용이 훨씬 크기 때문.
_REGION_SANITY_POS_TOLERANCE = 0.06      # 기본 위치 대비 ±6%p까지만 허용
_REGION_SANITY_SIZE_RATIO = (0.7, 1.35)  # 기본 크기 대비 70%~135%까지만 허용 (OCR 실측 바운딩박스 기반이라 텍스트 내용에 따라 약간의 흔들림이 있음)
# 🏷️ [라벨 폭 전용 완화] arcana_label의 width만은 실제 라벨 글자 수(아르카나=4자 vs 여정/훈련=2자)에
# 직접 비례해서, 어떤 라벨이 떴는지에 따라 정상적으로도 폭이 최대 2배 가까이 달라질 수 있다.
# (반면 event_title의 width는 글자 높이에서만 파생되고, card_art의 width는 아예 고정값이라
# 텍스트 내용과 무관하게 항상 일정하므로 이 둘은 기존의 타이트한 기준을 그대로 유지한다.)
_REGION_SANITY_LABEL_WIDTH_RATIO = (0.35, 1.3)

def _is_region_frac_sane(frac, default_frac, size_ratio=None):
    top, left = frac.get("top", -1), frac.get("left", -1)
    width, height = frac.get("width", -1), frac.get("height", -1)

    if not (0.0 <= top <= 1.0 and 0.0 <= left <= 1.0):
        return False, f"top/left가 화면 범위를 벗어남 (top={top:.3f}, left={left:.3f})"
    if width <= 0 or height <= 0:
        return False, f"width/height가 0 이하 (width={width:.3f}, height={height:.3f})"
    if top + height > 1.05 or left + width > 1.05:
        return False, f"영역이 화면 하단/우측 밖으로 벗어남 (top+height={top + height:.3f}, left+width={left + width:.3f})"

    dt, dl = default_frac["top"], default_frac["left"]
    dw, dh = default_frac["width"], default_frac["height"]
    width_min, width_max = size_ratio or _REGION_SANITY_SIZE_RATIO
    height_min, height_max = _REGION_SANITY_SIZE_RATIO

    if abs(top - dt) > _REGION_SANITY_POS_TOLERANCE or abs(left - dl) > _REGION_SANITY_POS_TOLERANCE:
        return False, (f"기본 위치 대비 편차가 너무 큼 (top: {dt:.3f}→{top:.3f}, "
                        f"left: {dl:.3f}→{left:.3f}, 허용치 ±{_REGION_SANITY_POS_TOLERANCE})")
    if not (dw * width_min <= width <= dw * width_max) or not (dh * height_min <= height <= dh * height_max):
        return False, (f"기본 크기 대비 편차가 너무 큼 (width: {dw:.3f}→{width:.3f}, "
                        f"height: {dh:.3f}→{height:.3f}, 허용 비율 width {width_min}~{width_max}배 / height {height_min}~{height_max}배)")

    return True, None

_KNOWN_JOURNEY_LABELS = ("아르카나 이벤트", "여정 이벤트", "훈련 이벤트")
_LABEL_FUZZY_CUTOFF = 0.55

def _label_fuzzy_score(text):
    """OCR이 읽은 텍스트가 3종 라벨(아르카나/여정/훈련 이벤트) 중 하나와 얼마나 비슷한지 계산.
    보정 단계에선 어떤 라벨인지 정확히 구분할 필요가 없고 위치만 알면 되므로,
    셋 중 가장 비슷한 값과의 유사도만 반환한다. 단, '이벤트'처럼 세 라벨에 공통으로
    들어가는 짧은 조각만 읽혔을 땐 실제로 뭘 오인식한 건지 알 수 없어 오히려 화면의
    다른 엉뚱한 '이벤트' 텍스트를 잘못 붙잡을 수 있으므로, 공백 제외 4자 이하는 애초에
    비교하지 않는다 (실제 라벨은 짧아도 6자 이상이라 안전한 하한선)."""
    text = text.strip()
    if len(text.replace(" ", "")) < 5:
        return 0.0
    return max(difflib.SequenceMatcher(None, text, cand).ratio() for cand in _KNOWN_JOURNEY_LABELS)

def try_auto_calibrate_regions(reader, game_rect):
    global _auto_calibration_done, REGION_FRACTIONS, _last_calibrated_rect, _calibration_samples
    try:
        sct = mss.MSS()
        # 🪶 [스캔 낭비 축소] 상단 HUD("목표까지", "평가전 승리", "N월 초순" 등)는 라벨보다
        # 훨씬 위쪽에 고정적으로 나오는 요소라 매번 같이 읽을 필요가 없음. 다만 창 크기가
        # 다양할 수 있으니 기본 라벨 위치 추정치(arcana_label 기본 top=0.170)보다 확실히
        # 위쪽인 12%까지만 건너뛰어 안전 마진을 둔다.
        _SCAN_TOP_SKIP = 0.12
        scan_region = {
            "top": game_rect["top"] + int(game_rect["height"] * _SCAN_TOP_SKIP),
            "left": game_rect["left"],
            "width": int(game_rect["width"] * 0.45),
            "height": int(game_rect["height"] * (0.42 - _SCAN_TOP_SKIP)),
        }
        img = np.array(sct.grab(scan_region))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        results = reader.readtext(gray, detail=1)

        label_box = None
        for bbox, text, conf in results:
            is_exact = "아르카나" in text or "여정" in text or "훈련" in text
            fuzzy_score = 0.0 if is_exact else _label_fuzzy_score(text)
            if is_exact or fuzzy_score >= _LABEL_FUZZY_CUTOFF:
                bx1, by1 = bbox[0]
                if by1 < scan_region["height"] * 0.06:
                    print(f"🪟 [자동 보정] '{text}' 후보는 화면 맨 위쪽이라 저희 앱 자신의 UI로 의심 → 건너뜀")
                    continue
                if not is_exact:
                    print(f"🪟 [자동 보정] '{text}' 텍스트가 정확히 일치하진 않지만 라벨과 유사해 인정함 (유사도 {fuzzy_score:.2f})")
                label_box = bbox
                break
        if not label_box:
            if results:
                _raw_texts = ", ".join(f"'{t}'({c:.2f})" for _, t, c in results)
                print(f"🪟 [자동 보정] 화면에서 '아르카나 이벤트'/'여정 이벤트'/'훈련 이벤트' 글자를 못 찾음 (대신 인식된 것: {_raw_texts}) → 기본값 사용")
            else:
                print("🪟 [자동 보정] 스캔 영역에서 텍스트가 아예 하나도 인식되지 않음 → 기본값 사용")
            return False

        lx1, ly1 = label_box[0]
        lx2, ly2 = label_box[2]
        label_h = ly2 - ly1
        label_w = lx2 - lx1

        title_box = None
        for bbox, text, conf in results:
            bx1, by1 = bbox[0]
            bx2, by2 = bbox[2]
            bh = by2 - by1
            # ⚠️ [임계값 근접 실패 보정] 실측 UI 비율은 label_h 대비 title_h가 약 1.14배 정도라
            # 기존 1.15배 기준은 OCR 측정 오차 몇 픽셀 차이로도 계속 턱걸이 탈락하는 문제가 있었음.
            # 1.05배로 낮춰서 여유를 두되, 라벨과 같은 크기의 텍스트(오탐)는 여전히 걸러낸다.
            if by1 > ly1 and by1 < ly2 + label_h * 5 and bh > label_h * 1.05:
                if title_box is None or by1 < title_box[0][1]:
                    title_box = bbox
        if not title_box:
            if len(results) > 1:
                _raw_texts = ", ".join(
                    f"'{t}'(y={bbox[0][1]:.0f}, h={(bbox[2][1]-bbox[0][1]):.0f})" for bbox, t, c in results
                )
                print(f"🪟 [자동 보정] 라벨은 찾았음(label_h={label_h:.0f}, 제목 조건: label_h*1.15={label_h*1.15:.0f} 이상) "
                      f"이지만 조건에 맞는 큰 글씨가 없음 (스캔된 전체 텍스트: {_raw_texts}) → 기본값 사용")
            else:
                print("🪟 [자동 보정] 라벨은 찾았지만 스캔 영역에서 그 외 텍스트가 아예 인식되지 않음 → 기본값 사용")
            return False

        tx1, ty1 = title_box[0]
        tx2, ty2 = title_box[2]
        title_h = ty2 - ty1

        base_left, base_top = scan_region["left"], scan_region["top"]
        gw, gh = game_rect["width"], game_rect["height"]

        _calibration_samples.append({
            "lx1_px": base_left + lx1 - game_rect["left"],
            "ly1_px": base_top + ly1 - game_rect["top"],
            "label_w_px": label_w,
            "label_h_px": label_h,
            "title_h_px": title_h,
        })
        if len(_calibration_samples) < _CALIBRATION_SAMPLES_NEEDED:
            print(f"🪟 [자동 보정] 측정 {len(_calibration_samples)}/{_CALIBRATION_SAMPLES_NEEDED}회 완료, 안정화를 위해 조금 더 측정 중...")
            return False

        med = lambda key: statistics.median(s[key] for s in _calibration_samples)
        lx1_px, ly1_px = med("lx1_px"), med("ly1_px")
        label_w_px, label_h_px = med("label_w_px"), med("label_h_px")
        title_h_px = med("title_h_px")

        def px_to_frac(x_px, y_px, w_px, h_px):
            return {"top": max(0, y_px / gh), "left": max(0, x_px / gw), "width": w_px / gw, "height": h_px / gh}

        label_region_frac = px_to_frac(
            lx1_px - label_h_px * 0.4, ly1_px - label_h_px * 0.5,
            label_w_px + label_h_px * 1.6, label_h_px * 1.5)
        title_region_frac = px_to_frac(
            lx1_px - title_h_px * 0.3, ly1_px + label_h_px * 1.1,
            title_h_px * 11.0, title_h_px * 1.9)

        card_left_px = lx1_px - gw * 0.070
        card_top_px = ly1_px - gh * 0.0556
        card_region_frac = px_to_frac(card_left_px, card_top_px, gw * 0.051, gh * 0.160)

        _sanity_checks = [
            ("arcana_label", label_region_frac, _DEFAULT_ARCANA_LABEL_FRACTION),
            ("event_title", title_region_frac, _DEFAULT_EVENT_TITLE_FRACTION),
        ]
        if not _card_region_manual_override:
            _sanity_checks.append(("card_art", card_region_frac, _DEFAULT_CARD_ART_FRACTION))

        for _region_name, _region_frac, _region_default in _sanity_checks:
            _width_ratio = _REGION_SANITY_LABEL_WIDTH_RATIO if _region_name == "arcana_label" else None
            _ok, _reason = _is_region_frac_sane(_region_frac, _region_default, size_ratio=_width_ratio)
            if not _ok:
                print(f"🛑 [자동 보정 거부] '{_region_name}' 계산 결과가 상식적인 범위를 벗어나 적용을 취소함 — {_reason}")
                _calibration_samples = []
                return False

        REGION_FRACTIONS["arcana_label"] = label_region_frac
        REGION_FRACTIONS["event_title"] = title_region_frac
        if not _card_region_manual_override:
            REGION_FRACTIONS["card_art"] = card_region_frac
        else:
            print("🃏 [카드 영역] 사용자가 직접 지정한 영역이 있어 자동 보정을 건너뜀")
        _auto_calibration_done = True
        _last_calibrated_rect = dict(game_rect)
        _calibration_samples = []
        print(f"🪟 [자동 보정 성공, {_CALIBRATION_SAMPLES_NEEDED}회 측정 중간값 사용] 라벨/제목/카드 캡처 영역 재계산 완료")
        return True
    except Exception as e:
        print(f"🪟 [자동 보정 실패] 예외 발생: {e} → 기본값 사용")
        return False

def is_game_hovered_or_active():
    try:
        user32 = ctypes.windll.user32
        game_keywords = _GAME_KEYWORDS

        def _is_browser(hwnd):
            cls_buff = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buff, 256)
            return cls_buff.value.lower() in _BROWSER_WINDOW_CLASSES

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd = user32.WindowFromPoint(pt)
        if hwnd:
            root_hwnd = user32.GetAncestor(hwnd, 2)
            target = root_hwnd if root_hwnd else hwnd
            length = user32.GetWindowTextLengthW(target)
            if length > 0 and not _is_browser(target):
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(target, buff, length + 1)
                title = buff.value.lower()
                if any(k in title for k in game_keywords): return True

        fg_hwnd = user32.GetForegroundWindow()
        if fg_hwnd and not _is_browser(fg_hwnd):
            length = user32.GetWindowTextLengthW(fg_hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(fg_hwnd, buff, length + 1)
                title = buff.value.lower()
                if any(k in title for k in game_keywords): return True
    except Exception: pass
    return False

CARD_ART_REGION = REGION_FRACTIONS["card_art"]

def is_tooltip_noise(text):
    return any(k in text for k in TOOLTIP_NOISE)

class CardPreviewOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(214, 154)

        self.raw_label = QLabel("원본(꽉채움)\n대기 중...", self)
        self.raw_label.setFixedSize(100, 150)
        self.raw_label.setAlignment(Qt.AlignCenter)
        self.raw_label.setScaledContents(True)
        self.raw_label.setStyleSheet("background-color: rgba(10, 14, 24, 240); border: 2px solid #ffcc4d; border-radius: 8px; color: #ffcc4d; font-size: 10px; font-weight: bold;")

        self.warped_label = QLabel("보정 후\n대기 중...", self)
        self.warped_label.setFixedSize(100, 150)
        self.warped_label.setAlignment(Qt.AlignCenter)
        self.warped_label.setScaledContents(True)
        self.warped_label.setStyleSheet("background-color: rgba(10, 14, 24, 240); border: 2px solid #00e5ff; border-radius: 8px; color: #00e5ff; font-size: 10px; font-weight: bold;")

        layout = QHBoxLayout()
        layout.addWidget(self.raw_label)
        layout.addWidget(self.warped_label)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.setLayout(layout)

        self.reposition()
        self.show()
        exclude_window_from_capture(self)

    def reposition(self):
        game_rect = get_game_window_rect()
        if game_rect:
            base_left, base_top, base_w, base_h = game_rect["left"], game_rect["top"], game_rect["width"], game_rect["height"]
        else:
            desktop_geo = QApplication.desktop().screenGeometry()
            base_left, base_top, base_w, base_h = 0, 0, desktop_geo.width(), desktop_geo.height()
        self.move(int(base_left + base_w * 0.040), int(base_top + base_h * 0.310))

    def _to_pixmap(self, cv_bgr_img, target_w, target_h):
        rgb = cv2.cvtColor(cv_bgr_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        return QPixmap.fromImage(q_img).scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _to_pixmap_stretched(self, cv_bgr_img, target_w, target_h):
        rgb = cv2.cvtColor(cv_bgr_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        return QPixmap.fromImage(q_img).scaled(target_w, target_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def update_raw(self, cv_bgr_img):
        self.raw_label.setPixmap(self._to_pixmap_stretched(cv_bgr_img, 100, 150))

    def update_image(self, cv_bgr_img):
        self.warped_label.setPixmap(self._to_pixmap(cv_bgr_img, 100, 150))

class RegionDebugOverlay(QWidget):
    def __init__(self, screen_w, screen_h):
        super().__init__()
        self.screen_w, self.screen_h = screen_w, screen_h
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        desktop = QApplication.desktop()
        full_rect = desktop.geometry()
        self.setGeometry(full_rect)
        self.regions = {}  
        self.show()
        exclude_window_from_capture(self)

    def refresh(self):
        colors = {
            "event_title": ("#00e5ff", "제목"),
            "arcana_label": ("#ffcc4d", "라벨"),
            "card_art": ("#ff5599", "카드"),
        }
        self.regions = {}
        for key, (color, label) in colors.items():
            frac = REGION_FRACTIONS.get(key)
            if not frac: continue
            rect = compute_region(frac, self.screen_w, self.screen_h)
            self.regions[key] = (rect, color, label)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(QFont("맑은 고딕", 10, QFont.Bold))
        for key, (rect, color, label) in self.regions.items():
            pen = QPen(QColor(color))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawRect(rect["left"], rect["top"], rect["width"], rect["height"])
            painter.setPen(QColor(color))
            painter.drawText(rect["left"] + rect["width"] + 6, rect["top"] + 14, label)

class CardRegionAdjustOverlay(QWidget):
    HANDLE_SIZE = 10

    def __init__(self, on_save=None, on_cancel=None):
        super().__init__()
        self._on_save = on_save
        self._on_cancel = on_cancel
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)

        game_rect = get_game_window_rect()
        if game_rect:
            self._base_left, self._base_top = game_rect["left"], game_rect["top"]
            self._base_w, self._base_h = game_rect["width"], game_rect["height"]
        else:
            desktop_geo = QApplication.desktop().screenGeometry()
            self._base_left, self._base_top = 0, 0
            self._base_w, self._base_h = desktop_geo.width(), desktop_geo.height()

        self.setGeometry(self._base_left, self._base_top, self._base_w, self._base_h)

        rect = compute_region(REGION_FRACTIONS["card_art"], self._base_w, self._base_h)
        self._box = QRect(rect["left"] - self._base_left, rect["top"] - self._base_top, rect["width"], rect["height"])
        self._drag_mode = None
        self._drag_start_pos = None
        self._drag_start_box = None

        self._build_controls()
        self.show()

    def _build_controls(self):
        info = QLabel("박스를 드래그해서 옮기거나, 모서리 점을 끌어 크기를 조절하세요.\n조정 중에는 게임 클릭이 막힙니다.", self)
        info.setStyleSheet("background-color: rgba(10,14,24,230); color:#00e5ff; font-size:12px; padding:8px 12px; border-radius:6px;")
        info.move(20, 20)
        info.adjustSize()

        btn_save = QPushButton("✅ 저장", self)
        btn_save.setStyleSheet("QPushButton { background-color:#1f5c33; color:white; border-radius:6px; padding:6px 14px; font-weight:bold; } QPushButton:hover { background-color:#28753f; }")
        btn_save.clicked.connect(self._save)
        btn_save.move(20, 75)
        btn_save.adjustSize()

        btn_reset = QPushButton("↩ 기본값으로 고정", self)
        btn_reset.setStyleSheet("QPushButton { background-color:#3a4453; color:white; border-radius:6px; padding:6px 14px; } QPushButton:hover { background-color:#4a5568; }")
        btn_reset.clicked.connect(self._reset)
        btn_reset.move(110, 75)
        btn_reset.adjustSize()

        btn_cancel = QPushButton("✕ 취소", self)
        btn_cancel.setStyleSheet("QPushButton { background-color:#4a1f24; color:white; border-radius:6px; padding:6px 14px; } QPushButton:hover { background-color:#6b2a30; }")
        btn_cancel.clicked.connect(self._cancel)
        btn_cancel.move(280, 75)
        btn_cancel.adjustSize()

    def _handle_points(self):
        r = self._box
        return [(r.left(), r.top()), (r.right(), r.top()), (r.left(), r.bottom()), (r.right(), r.bottom())]

    def _hit_handle(self, pos):
        for i, (hx, hy) in enumerate(self._handle_points()):
            if abs(pos.x() - hx) <= self.HANDLE_SIZE and abs(pos.y() - hy) <= self.HANDLE_SIZE:
                return i
        return None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#ff5599"))
        pen.setWidth(3)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(255, 85, 153, 40)))
        painter.drawRect(self._box)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#ff5599")))
        for hx, hy in self._handle_points():
            painter.drawEllipse(QPoint(int(hx), int(hy)), self.HANDLE_SIZE // 2, self.HANDLE_SIZE // 2)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.pos()
        handle = self._hit_handle(pos)
        if handle is not None:
            self._drag_mode = ('resize', handle)
        elif self._box.contains(pos):
            self._drag_mode = ('move', None)
        else:
            self._drag_mode = None
            return
        self._drag_start_pos = pos
        self._drag_start_box = QRect(self._box)

    def mouseMoveEvent(self, event):
        if self._drag_mode is None:
            return
        pos = event.pos()
        delta = pos - self._drag_start_pos
        mode, handle = self._drag_mode
        r = QRect(self._drag_start_box)
        if mode == 'move':
            r.translate(delta)
        else:
            if handle == 0:
                r.setTopLeft(r.topLeft() + delta)
            elif handle == 1:
                r.setTopRight(r.topRight() + delta)
            elif handle == 2:
                r.setBottomLeft(r.bottomLeft() + delta)
            elif handle == 3:
                r.setBottomRight(r.bottomRight() + delta)
        if r.width() > 20 and r.height() > 20:
            self._box = r.normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag_mode = None

    def _save(self):
        frac = {
            "top": max(0.0, self._box.top() / self._base_h),
            "left": max(0.0, self._box.left() / self._base_w),
            "width": self._box.width() / self._base_w,
            "height": self._box.height() / self._base_h,
        }
        if callable(self._on_save):
            self._on_save(frac)
        self.close()

    def _reset(self):
        if callable(self._on_save):
            self._on_save(None)
        self.close()

    def _cancel(self):
        if callable(self._on_cancel):
            self._on_cancel()
        self.close()

class JourneyStatusOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel("🟡 [DB 연결 중] 웹사이트 실시간 파싱 준비 중...", self)
        self.label.setCursor(Qt.PointingHandCursor)

        self._is_collapsed = False
        self._last_text = "🟡 [DB 연결 중] 웹사이트 실시간 파싱 준비 중..."
        self._last_border = "#ffcc00"
        self._last_text_color = "#ffcc00"
        self.on_settings_requested = None
        self.on_reset_requested = None
        self.setToolTip("좌클릭: 접기/펼치기 + 새로고침 · 드래그: 위치 이동(고정 해제 시) · 우클릭: 설정")

        _loaded_settings = load_settings()
        _saved = _loaded_settings.get("status_badge_offset")
        self._custom_offset = tuple(_saved) if (isinstance(_saved, (list, tuple)) and len(_saved) == 2) else None
        self._drag_start_global = None
        self._drag_start_widget_pos = None
        self._is_dragging = False
        self._DRAG_THRESHOLD_PX = 6
        self._position_locked = _loaded_settings.get("status_badge_locked", True)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self._render()
        self.reposition()
        self.show()
        exclude_window_from_capture(self)

    def reposition(self):
        if getattr(self, '_is_dragging', False):
            return
        game_rect = get_game_window_rect()
        if game_rect:
            base_left, base_top, base_w = game_rect["left"], game_rect["top"], game_rect["width"]
        else:
            base_left, base_top, base_w = 0, 0, QApplication.desktop().screenGeometry().width()

        if self._custom_offset is not None:
            target_x = int(base_left + self._custom_offset[0])
            target_y = int(base_top + self._custom_offset[1])
            screen = QApplication.desktop().screenGeometry()
            target_x = max(0, min(target_x, screen.width() - max(1, self.width())))
            target_y = max(0, min(target_y, screen.height() - max(1, self.height())))
            self.move(target_x, target_y)
        else:
            self.move(int(base_left + base_w / 2 - self.width() / 2), base_top + 4)

    def set_custom_status(self, text, border_color="#00e5ff", text_color="#00e5ff", protect_seconds=0):
        now = time.time()
        if now < getattr(self, '_status_protect_until', 0):
            return
        self._status_protect_until = (now + protect_seconds) if protect_seconds > 0 else 0
        self._last_text = text
        self._last_border = border_color
        self._last_text_color = text_color
        self._render()

    def set_custom_status_animated(self, text, border_color="#00e5ff", text_color="#00e5ff", protect_seconds=0, fade_ms=450):
        now = time.time()
        if now < getattr(self, '_status_protect_until', 0):
            return
        self._status_protect_until = (now + protect_seconds) if protect_seconds > 0 else 0

        def _apply_and_fade_in():
            self._last_text = text
            self._last_border = border_color
            self._last_text_color = text_color
            self._render()
            self.setWindowOpacity(0.0)
            anim_in = QPropertyAnimation(self, b"windowOpacity", self)
            anim_in.setDuration(fade_ms)
            anim_in.setStartValue(0.0)
            anim_in.setEndValue(1.0)
            anim_in.setEasingCurve(QEasingCurve.OutCubic)
            anim_in.start(QPropertyAnimation.DeleteWhenStopped)
            self._fade_anim = anim_in

        current_opacity = self.windowOpacity()
        anim_out = QPropertyAnimation(self, b"windowOpacity", self)
        anim_out.setDuration(max(150, fade_ms // 2))
        anim_out.setStartValue(current_opacity)
        anim_out.setEndValue(0.15)
        anim_out.setEasingCurve(QEasingCurve.InCubic)
        anim_out.finished.connect(_apply_and_fade_in)
        anim_out.start(QPropertyAnimation.DeleteWhenStopped)
        self._fade_anim = anim_out

    def _render(self):
        if self._is_collapsed:
            display_text = "🔽 (클릭하여 활성화)"
            border_color = "#666666"
            text_color = "#999999"
        else:
            display_text = self._last_text
            border_color = self._last_border
            text_color = self._last_text_color
        self.label.setText(display_text)
        self.label.setStyleSheet(f"""
            QLabel {{ background-color: rgba(10, 14, 24, 245); border: 2px solid {border_color}; border-radius: 15px; color: {text_color}; font-size: 13px; font-weight: bold; padding: 6px 18px; }}
        """)
        self.adjustSize()
        self.reposition()
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_global = event.globalPos()
            self._drag_start_widget_pos = self.pos()
            self._is_dragging = False
        elif event.button() == Qt.RightButton:
            if callable(self.on_settings_requested):
                self.on_settings_requested()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._position_locked:
            return super().mouseMoveEvent(event)
        if self._drag_start_global is not None and (event.buttons() & Qt.LeftButton):
            delta = event.globalPos() - self._drag_start_global
            if not self._is_dragging and delta.manhattanLength() >= self._DRAG_THRESHOLD_PX:
                self._is_dragging = True
            if self._is_dragging:
                self.move(self._drag_start_widget_pos + delta)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_start_global is not None:
            if self._is_dragging:
                game_rect = get_game_window_rect()
                if game_rect:
                    base_left, base_top = game_rect["left"], game_rect["top"]
                else:
                    base_left, base_top = 0, 0
                self._custom_offset = (self.x() - base_left, self.y() - base_top)
                _settings = load_settings()
                _settings["status_badge_offset"] = list(self._custom_offset)
                save_settings(_settings)
                print(f"✋ [상단바 위치 저장됨] 게임 창 기준 오프셋 {self._custom_offset}")
            else:
                self._is_collapsed = not self._is_collapsed
                self._render()
                if callable(self.on_reset_requested):
                    self.on_reset_requested()
            self._drag_start_global = None
            self._drag_start_widget_pos = None
            self._is_dragging = False
        super().mouseReleaseEvent(event)

    def reset_position(self):
        self._custom_offset = None
        _settings = load_settings()
        _settings["status_badge_offset"] = None
        save_settings(_settings)
        self.reposition()
        print("🔄 [상단바 위치 초기화됨] 기본 위치(화면 중앙 상단)로 복귀")

    def set_position_locked(self, locked):
        self._position_locked = locked

    def set_capture_visible_for_debug(self, visible_in_capture):
        set_window_capture_visible_for_debug(self, visible_in_capture)

    def is_collapsed(self):
        return self._is_collapsed

class SettingsDialog(QDialog):
    def __init__(self, on_quit=None,
                 on_toggle_card_preview=None, on_toggle_region_debug=None,
                 on_open_region_adjuster=None, on_font_scale_change=None,
                 on_hide_overlay=None, on_reset_position=None, on_toggle_position_lock=None,
                 on_toggle_capture_debug=None, on_toggle_low_spec=None, parent=None):
        super().__init__(parent)
        self._on_quit = on_quit
        self._on_toggle_card_preview = on_toggle_card_preview
        self._on_toggle_region_debug = on_toggle_region_debug
        self._on_open_region_adjuster = on_open_region_adjuster
        self._on_font_scale_change = on_font_scale_change
        self._on_hide_overlay = on_hide_overlay
        self._on_reset_position = on_reset_position
        self._on_toggle_position_lock = on_toggle_position_lock
        self._on_toggle_capture_debug = on_toggle_capture_debug
        self._on_toggle_low_spec = on_toggle_low_spec
        self.setWindowTitle("JourneyHelper 설정")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setFixedWidth(430)
        self.setStyleSheet("""
            QDialog { background-color: #14181f; }
            QLabel { color: #dddddd; font-size: 12px; }
            QCheckBox { color: #eeeeee; font-size: 13px; padding: 4px 0px; }
            QPushButton {
                background-color: #1f2733; color: #eeeeee; border: 1px solid #3a4453;
                border-radius: 6px; padding: 6px 12px; font-size: 12px;
            }
            QPushButton:hover { background-color: #2a3442; }
            QComboBox {
                background-color: #1f2733; color: #eeeeee; border: 1px solid #3a4453;
                border-radius: 6px; padding: 4px 10px; font-size: 12px;
            }
            QComboBox QAbstractItemView {
                background-color: #1f2733; color: #eeeeee; selection-background-color: #2a3442;
            }
            QLineEdit {
                background-color: #1f2733; color: #eeeeee; border: 1px solid #3a4453;
                border-radius: 6px; padding: 5px 10px; font-size: 12px;
            }
            QTabWidget::pane { border: 1px solid #2a3442; border-radius: 6px; top: -1px; }
            QTabBar::tab {
                background-color: #1a1f28; color: #aaaaaa; padding: 6px 12px;
                border: 1px solid #2a3442; border-bottom: none;
                border-top-left-radius: 6px; border-top-right-radius: 6px;
            }
            QTabBar::tab:selected { background-color: #1f2733; color: #00e5ff; }
            QTabBar::tab:hover { color: #eeeeee; }
        """)

        self._settings = load_settings()

        outer_layout = QVBoxLayout()
        outer_layout.setSpacing(10)

        title = QLabel("⚙️ 설정")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #00e5ff;")
        outer_layout.addWidget(title)

        tabs = QTabWidget()
        tab_general = QWidget()
        tab_greeting = QWidget()
        tab_display = QWidget()
        tab_advanced = QWidget()
        tabs.addTab(tab_general, "일반")
        tabs.addTab(tab_greeting, "인사말 & 생일")
        tabs.addTab(tab_display, "게임 표시")
        tabs.addTab(tab_advanced, "고급/디버그")

        # ===================== [탭 1] 일반 =====================
        layout = QVBoxLayout()
        layout.setSpacing(10)

        def _section_label(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#00e5ff; font-size:11px; font-weight:bold; margin-top:4px;")
            return lbl

        # --------- 성능 최적화 ---------
        layout.addWidget(_section_label("⚡ 성능 최적화"))
        self.chk_low_spec = QCheckBox("최적화 / CPU 절약 모드")
        self.chk_low_spec.setChecked(self._settings.get("low_spec_mode", False))
        self.chk_low_spec.stateChanged.connect(
            lambda state: self._on_toggle_low_spec(state != 0) if callable(self._on_toggle_low_spec) else None
        )
        layout.addWidget(self.chk_low_spec)
        low_spec_hint = QLabel("CPU 점유율이 높을 때 켜세요.\n화면 감시 주기를 단축하고 프레임 캐싱을 강화하여 CPU 부하를 줄여주지만, 반응 속도가 저하될 수 있습니다.")
        low_spec_hint.setStyleSheet("color: #b0b0b0; font-size: 11px;")
        low_spec_hint.setWordWrap(True)
        layout.addWidget(low_spec_hint)

        self.chk_gpu_ocr = QCheckBox("OCR 모델 로딩 시 GPU 가속 시도 (기본: 켜짐)")
        self.chk_gpu_ocr.setChecked(self._settings.get("try_gpu_ocr", True))
        layout.addWidget(self.chk_gpu_ocr)
        gpu_ocr_hint = QLabel("게임 감지 시 OCR 모델을 GPU로 먼저 로딩합니다. 대부분 환경에서 문제 없지만,\n드물게 일부 환경(그래픽 드라이버/VRAM 여유 부족 등)에서는 게임과 GPU를 동시에\n잡으려다 프로그램이 예고 없이 종료될 수 있습니다. 그런 증상이 있다면 꺼보세요.")
        gpu_ocr_hint.setStyleSheet("color: #b0b0b0; font-size: 11px;")
        gpu_ocr_hint.setWordWrap(True)
        layout.addWidget(gpu_ocr_hint)

        # --------- 시작 ---------
        layout.addWidget(_section_label("🚀 시작"))

        startup_row = QHBoxLayout()
        startup_label = QLabel("시작할 때 상단바 상태")
        startup_row.addWidget(startup_label)
        startup_row.addStretch()
        self.combo_startup_state = QComboBox()
        self._startup_state_options = [("펼친 상태", "expanded"), ("접은 상태", "collapsed"), ("숨긴 상태", "hidden")]
        for label, _ in self._startup_state_options:
            self.combo_startup_state.addItem(label)
        current_state = self._settings.get("startup_state", "expanded")
        state_idx = next((i for i, (_, v) in enumerate(self._startup_state_options) if v == current_state), 0)
        self.combo_startup_state.setCurrentIndex(state_idx)
        startup_row.addWidget(self.combo_startup_state)
        layout.addLayout(startup_row)

        # --------- 상단바 ---------
        layout.addWidget(_section_label("📌 상단바"))

        self.chk_position_locked = QCheckBox("상단바 위치 고정")
        self.chk_position_locked.setChecked(self._settings.get("status_badge_locked", True))
        self.chk_position_locked.stateChanged.connect(
            lambda state: self._on_toggle_position_lock(state != 0) if callable(self._on_toggle_position_lock) else None
        )
        layout.addWidget(self.chk_position_locked)

        pos_row = QHBoxLayout()
        self.btn_reset_position = QPushButton("↺ 상단바 위치 초기화")
        self.btn_reset_position.clicked.connect(self._on_reset_position_clicked)
        pos_row.addWidget(self.btn_reset_position)
        layout.addLayout(pos_row)

        hide_row = QHBoxLayout()
        self.btn_hide_overlay = QPushButton("🙈 상단바 숨기기")
        self.btn_hide_overlay.clicked.connect(self._on_hide_overlay_clicked)
        hide_row.addWidget(self.btn_hide_overlay)
        layout.addLayout(hide_row)

        self.chk_auto_expand = QCheckBox("게임 실행 시 자동으로 상단바 펼치기")
        self.chk_auto_expand.setChecked(self._settings.get("auto_expand_on_launch", False))
        self.chk_auto_expand.stateChanged.connect(self._on_auto_expand_toggled)
        layout.addWidget(self.chk_auto_expand)

        auto_expand_hidden_row = QHBoxLayout()
        auto_expand_hidden_row.addSpacing(20)
        self.chk_auto_expand_hidden = QCheckBox("숨겨진 상단바도 자동으로 펼치기")
        self.chk_auto_expand_hidden.setChecked(self._settings.get("auto_expand_include_hidden", False))
        self.chk_auto_expand_hidden.setEnabled(self.chk_auto_expand.isChecked())
        auto_expand_hidden_row.addWidget(self.chk_auto_expand_hidden)
        layout.addLayout(auto_expand_hidden_row)

        # --------- 툴팁 ---------
        layout.addWidget(_section_label("🔤 툴팁"))

        font_row = QHBoxLayout()
        font_label = QLabel("왼쪽 툴팁 글자 크기")
        font_row.addWidget(font_label)
        font_row.addStretch()
        self.combo_font_scale = QComboBox()
        self._font_scale_options = [("작게", 0.85), ("보통", 1.0), ("크게", 1.2), ("아주 크게", 1.4)]
        for label, _ in self._font_scale_options:
            self.combo_font_scale.addItem(label)
        current_scale = self._settings.get("tooltip_font_scale", 1.0)
        closest_idx = min(range(len(self._font_scale_options)),
                           key=lambda i: abs(self._font_scale_options[i][1] - current_scale))
        self.combo_font_scale.setCurrentIndex(closest_idx)
        self.combo_font_scale.currentIndexChanged.connect(self._on_font_scale_selected)
        font_row.addWidget(self.combo_font_scale)
        layout.addLayout(font_row)

        layout.addStretch()
        tab_general.setLayout(layout)

        # ===================== [탭 2] 인사말 & 생일 =====================
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.chk_greeting_enabled = QCheckBox("시작할 때 인사말 표시")
        self.chk_greeting_enabled.setChecked(self._settings.get("greeting_enabled", True))
        layout.addWidget(self.chk_greeting_enabled)

        greeting_label = QLabel("시작할 때 인사말 (| 로 구분, 최대 5개 · 비워두면 기본 문구)")
        greeting_label.setWordWrap(True)
        layout.addWidget(greeting_label)
        self.txt_custom_greeting = QLineEdit()
        self.txt_custom_greeting.setPlaceholderText("예: 오늘도 잘 부탁해! | 어서와~ | 반가워")
        self.txt_custom_greeting.setText(" | ".join(self._settings.get("custom_greetings", [])))
        self.txt_custom_greeting.setMaxLength(300)
        layout.addWidget(self.txt_custom_greeting)

        line_greet = QFrame()
        line_greet.setFrameShape(QFrame.HLine)
        line_greet.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line_greet)

        birthday_title = QLabel("🎉 단장님 생일 (등록하면 그날 특별 문구가 떠요)")
        birthday_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #ff8fc7;")
        layout.addWidget(birthday_title)

        my_birthday_row = QHBoxLayout()
        self.combo_my_birthday_month = QComboBox()
        self.combo_my_birthday_month.addItem("설정 안함")
        for m in range(1, 13):
            self.combo_my_birthday_month.addItem(f"{m}월")
        saved_month = self._settings.get("user_birthday_month", 0)
        self.combo_my_birthday_month.setCurrentIndex(saved_month if 0 <= saved_month <= 12 else 0)
        my_birthday_row.addWidget(self.combo_my_birthday_month)

        self.combo_my_birthday_day = QComboBox()
        for d in range(1, 32):
            self.combo_my_birthday_day.addItem(f"{d}일")
        saved_day = self._settings.get("user_birthday_day", 1)
        self.combo_my_birthday_day.setCurrentIndex((saved_day - 1) if 1 <= saved_day <= 31 else 0)
        my_birthday_row.addWidget(self.combo_my_birthday_day)
        layout.addLayout(my_birthday_row)

        birthday_msg_label = QLabel("단장님 생일 축하 문구 (| 로 구분, 최대 5개 · 비워두면 기본 문구)")
        birthday_msg_label.setWordWrap(True)
        layout.addWidget(birthday_msg_label)
        self.txt_custom_birthday_message = QLineEdit()
        self.txt_custom_birthday_message.setPlaceholderText("예: 생일 축하해! | 오늘은 내 날! (| 로 구분, 최대 5개)")
        self.txt_custom_birthday_message.setText(" | ".join(self._settings.get("custom_birthday_messages", [])))
        self.txt_custom_birthday_message.setMaxLength(300)
        layout.addWidget(self.txt_custom_birthday_message)

        line_greet2 = QFrame()
        line_greet2.setFrameShape(QFrame.HLine)
        line_greet2.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line_greet2)

        self.chk_birthday_notify = QCheckBox("오늘 생일인 구원자 알림")
        self.chk_birthday_notify.setChecked(self._settings.get("birthday_notify_enabled", True))
        layout.addWidget(self.chk_birthday_notify)

        self.chk_announce_no_birthday = QCheckBox("생일인 구원자가 없는 날에도 알려주기")
        self.chk_announce_no_birthday.setChecked(self._settings.get("announce_no_birthday", True))
        self.chk_announce_no_birthday.setStyleSheet("margin-left: 18px; font-size: 12px; color: #bbbbbb;")
        layout.addWidget(self.chk_announce_no_birthday)

        layout.addStretch()
        tab_greeting.setLayout(layout)

        # ===================== [탭 3] 게임 표시 =====================
        layout = QVBoxLayout()
        layout.setSpacing(10)

        diff_title = QLabel("🎚️ 툴팁에서 숨길 난이도")
        diff_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #00e5ff;")
        layout.addWidget(diff_title)

        self._settings_hidden_difficulties = set(self._settings.get("hidden_difficulties", []))
        self.chk_difficulties = {}
        diff_row = QHBoxLayout()
        for diff_name in AVAILABLE_DIFFICULTIES:
            chk = QCheckBox(diff_name)
            chk.setChecked(diff_name in self._settings_hidden_difficulties)
            chk.stateChanged.connect(lambda state, name=diff_name: self._on_difficulty_toggled(name, state != 0))
            self.chk_difficulties[diff_name] = chk
            diff_row.addWidget(chk)
        layout.addLayout(diff_row)

        diff_hint = QLabel("체크한 난이도는 툴팁에서 숨겨져요. 프로그램을 재시작해야 적용돼요.")
        diff_hint.setStyleSheet("color: #b0b0b0; font-size: 11px;")
        diff_hint.setWordWrap(True)
        layout.addWidget(diff_hint)

        layout.addStretch()
        tab_display.setLayout(layout)

        # ===================== [탭 4] 고급/디버그 =====================
        layout = QVBoxLayout()
        layout.setSpacing(10)

        self.chk_log = QCheckBox("문제 발생 시 진단용 로그를 파일로 저장")
        self.chk_log.setChecked(self._settings.get("file_logging", False))
        layout.addWidget(self.chk_log)

        log_hint = QLabel(f"저장 위치: {get_log_path()}")
        log_hint.setStyleSheet("color: #b0b0b0; font-size: 11px;")
        log_hint.setWordWrap(True)
        layout.addWidget(log_hint)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line)

        debug_title = QLabel("🔧 카드 인식 디버그 도구")
        debug_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #ff9800;")
        layout.addWidget(debug_title)

        self.chk_card_preview = QCheckBox("카드 인식 미리보기 표시 (원본 / 보정 후 비교)")
        self.chk_card_preview.setChecked(self._settings.get("show_card_preview", False))
        self.chk_card_preview.stateChanged.connect(
            lambda state: self._on_toggle_card_preview(state != 0) if callable(self._on_toggle_card_preview) else None
        )
        layout.addWidget(self.chk_card_preview)

        self.chk_region_debug = QCheckBox("캡처 영역 박스 표시 (카드 영역)")
        self.chk_region_debug.setChecked(self._settings.get("show_region_debug", False))
        self.chk_region_debug.stateChanged.connect(
            lambda state: self._on_toggle_region_debug(state != 0) if callable(self._on_toggle_region_debug) else None
        )
        layout.addWidget(self.chk_region_debug)

        btn_adjust_region = QPushButton("🎯 카드 인식 영역 직접 조정...")
        btn_adjust_region.clicked.connect(self._open_region_adjuster)
        layout.addWidget(btn_adjust_region)

        line3 = QFrame()
        line3.setFrameShape(QFrame.HLine)
        line3.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line3)

        self.chk_capture_debug = QCheckBox("🐛 상단바/툴팁을 스크린샷에 보이게 하기 (디버그용)")
        self.chk_capture_debug.setChecked(False)
        self.chk_capture_debug.stateChanged.connect(
            lambda state: self._on_toggle_capture_debug(state != 0) if callable(self._on_toggle_capture_debug) else None
        )
        layout.addWidget(self.chk_capture_debug)

        line4 = QFrame()
        line4.setFrameShape(QFrame.HLine)
        line4.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line4)

        danger_title = QLabel("⚠️ 위험 구역")
        danger_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #ff5252;")
        layout.addWidget(danger_title)

        btn_reset_all = QPushButton("🗑️ 모든 설정 초기화")
        btn_reset_all.setStyleSheet(
            "QPushButton { background-color: #4a1f24; border-color: #7a2f36; } "
            "QPushButton:hover { background-color: #6b2a30; }"
        )
        btn_reset_all.clicked.connect(self._on_reset_all_settings)
        layout.addWidget(btn_reset_all)

        layout.addStretch()
        tab_advanced.setLayout(layout)

        outer_layout.addWidget(tabs)

        version_label = QLabel(f"현재 버전: {APP_VERSION}")
        outer_layout.addWidget(version_label)

        button_row = QHBoxLayout()
        btn_quit = QPushButton("🛑 프로그램 종료")
        btn_quit.setStyleSheet("QPushButton { background-color: #4a1f24; border-color: #7a2f36; } QPushButton:hover { background-color: #6b2a30; }")
        btn_quit.clicked.connect(self._quit_program)
        button_row.addWidget(btn_quit)
        button_row.addStretch()
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self._save_and_close)
        button_row.addWidget(btn_close)
        outer_layout.addLayout(button_row)

        self.setLayout(outer_layout)

    def _quit_program(self):
        self._persist_settings()
        self.accept()
        if callable(self._on_quit):
            self._on_quit()

    def _open_region_adjuster(self):
        if callable(self._on_open_region_adjuster):
            self._on_open_region_adjuster()
        self.close()

    def _on_auto_expand_toggled(self, state):
        self.chk_auto_expand_hidden.setEnabled(state != 0)

    def _on_font_scale_selected(self, index):
        scale = self._font_scale_options[index][1]
        if callable(self._on_font_scale_change):
            self._on_font_scale_change(scale)

    def _on_hide_overlay_clicked(self):
        self._persist_settings()
        if callable(self._on_hide_overlay):
            self._on_hide_overlay()
        self.accept()

    def _on_reset_position_clicked(self):
        if callable(self._on_reset_position):
            self._on_reset_position()
        self._settings["status_badge_offset"] = None

    def _on_difficulty_toggled(self, diff_name, checked):
        if checked:
            self._settings_hidden_difficulties.add(diff_name)
        else:
            self._settings_hidden_difficulties.discard(diff_name)

    def _current_font_scale(self):
        return self._font_scale_options[self.combo_font_scale.currentIndex()][1]

    def _persist_settings(self):
        self._settings["low_spec_mode"] = self.chk_low_spec.isChecked()
        self._settings["try_gpu_ocr"] = self.chk_gpu_ocr.isChecked()
        self._settings["file_logging"] = self.chk_log.isChecked()
        self._settings["show_card_preview"] = self.chk_card_preview.isChecked()
        self._settings["show_region_debug"] = self.chk_region_debug.isChecked()
        self._settings["tooltip_font_scale"] = self._current_font_scale()
        self._settings["hidden_difficulties"] = sorted(self._settings_hidden_difficulties)
        self._settings["startup_state"] = self._startup_state_options[self.combo_startup_state.currentIndex()][1]
        self._settings["birthday_notify_enabled"] = self.chk_birthday_notify.isChecked()
        self._settings["announce_no_birthday"] = self.chk_announce_no_birthday.isChecked()
        self._settings["greeting_enabled"] = self.chk_greeting_enabled.isChecked()
        self._settings["status_badge_locked"] = self.chk_position_locked.isChecked()
        self._settings["auto_expand_on_launch"] = self.chk_auto_expand.isChecked()
        self._settings["auto_expand_include_hidden"] = self.chk_auto_expand_hidden.isChecked()
        self._settings["custom_greetings"] = self._parse_pipe_list(self.txt_custom_greeting.text())
        self._settings["user_birthday_month"] = self.combo_my_birthday_month.currentIndex()
        self._settings["user_birthday_day"] = self.combo_my_birthday_day.currentIndex() + 1
        self._settings["custom_birthday_messages"] = self._parse_pipe_list(self.txt_custom_birthday_message.text())
        save_settings(self._settings)


    @staticmethod
    def _parse_pipe_list(raw_text, max_items=5):
        items = [p.strip() for p in raw_text.split("|")]
        items = [p for p in items if p]
        return items[:max_items]

    def _save_and_close(self):
        self._persist_settings()
        self.accept()

    def _on_reset_all_settings(self):
        reply = QMessageBox.question(
            self, "모든 설정 초기화",
            "정말로 모든 설정을 초기값으로 되돌릴까요?\n\n"
            "인사말/생일 문구, 카드 인식 영역 조정값, 상단바 위치,\n"
            "자동 실행 등 지금까지 저장한 모든 설정이 사라지며\n"
            "되돌릴 수 없습니다.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        save_settings(dict(DEFAULT_SETTINGS))
        QMessageBox.information(
            self, "초기화 완료",
            "모든 설정이 초기화되었습니다.\n완전히 적용하려면 프로그램을 재시작해주세요."
        )
        self.reject()

class FixedLeftTooltip(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.label = QLabel("💡 선택지에 마우스를 올려보세요", self)
        self.label.setTextFormat(Qt.RichText)
        self._base_font_px = 13
        self._base_width = 400
        self._current_scale = 1.0
        self._is_wide = False
        self._current_min_width = None
        self._wide_multiplier = 1.9
        self.label.setStyleSheet(self._build_stylesheet(self._base_font_px))
        self.label.setWordWrap(True)
        self.setFixedWidth(self._base_width)
        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.reposition()
        exclude_window_from_capture(self)

    def set_capture_visible_for_debug(self, visible_in_capture):
        set_window_capture_visible_for_debug(self, visible_in_capture)

    def _build_stylesheet(self, font_px):
        return f"""
            QLabel {{ background-color: rgba(10, 12, 18, 250); border: 2px solid #00e5ff; border-radius: 12px; color: #ffffff; font-size: {font_px}px; font-weight: bold; padding: 14px 18px; line-height: 150%; }}
        """

    def _apply_width(self, wide, min_width=None):
        self._is_wide = wide
        if wide and min_width:
            new_width = max(round(self._base_width * self._current_scale), min_width)
        elif wide:
            new_width = round(self._base_width * self._current_scale * self._wide_multiplier)
        else:
            new_width = round(self._base_width * self._current_scale)
        if new_width != getattr(self, '_current_width', None):
            self._current_width = new_width
            self.setFixedWidth(new_width)

    def set_font_scale(self, scale):
        px = max(9, round(self._base_font_px * scale))
        self.label.setStyleSheet(self._build_stylesheet(px))
        self._current_scale = scale
        self._apply_width(self._is_wide, getattr(self, '_current_min_width', None))
        self.label.adjustSize()
        self.adjustSize()

    def reposition(self):
        game_rect = get_game_window_rect()
        if game_rect:
            self.fixed_x = game_rect["left"] + 25
            self.fixed_y = game_rect["top"] + int(game_rect["height"] * 0.30)
        else:
            screen_h = QApplication.desktop().screenGeometry().height()
            self.fixed_x = 25
            self.fixed_y = int(screen_h * 0.30)
        self.move(self.fixed_x, self.fixed_y)

    def show_info(self, text, wide=False, min_width=None):
        self.reposition()
        self._current_min_width = min_width
        self._apply_width(wide, min_width)
        self.label.setText(text)
        self.label.adjustSize()
        self.adjustSize()
        self.resize(self.sizeHint())
        self.move(self.fixed_x, self.fixed_y)
        self.show()

    def hide_and_clear(self):
        if self.isVisible():
            print(f"🫥 [툴팁 숨김] hide_and_clear() 호출됨 (직전 내용: '{self.label.text()[:30]}...')")
        self.label.setText("")
        self.move(-10000, -10000)
        self.hide()


# ----------------------------------------------------
# 4. OCR 스레드 및 사전 스캔 스레드
# ----------------------------------------------------
class EventTitleOCRThread(QThread):
    event_detected = pyqtSignal(str)
    journey_status_changed = pyqtSignal(bool)
    arcana_label_detected = pyqtSignal(bool) 

    model_loading_started = pyqtSignal()

    def __init__(self, screen_w, screen_h, pause_getter=None, low_spec_getter=None, gpu_ocr_getter=None):
        super().__init__()
        self.reader = None
        self.sct = mss.MSS()
        self.screen_w, self.screen_h = screen_w, screen_h
        self.last_event = ""
        self.empty_count = 0
        self.pending_event = ""
        self.pending_count = 0
        self.gpu_ocr_getter = gpu_ocr_getter or (lambda: True)
        self.pause_getter = pause_getter or (lambda: False)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self.is_arcana_label = True 
        self._last_label_log = 0
        self._last_label_raw_log = 0
        
        self._last_title_gray = None
        self._cached_combined = ""
        self._cached_raw_joined = ""
        self._last_label_gray = None
        self._cached_label_text = ""

        self.is_waiting_for_next_event = False
        self.completed_event_title = ""

    def _is_same_as_last(self, combined):
        if not self.last_event: return False
        ratio = difflib.SequenceMatcher(None, combined.replace(" ", ""), self.last_event.replace(" ", "")).ratio()
        return ratio >= 0.82

    def _is_same_as_completed(self, combined):
        if not self.completed_event_title: return False
        ratio = difflib.SequenceMatcher(None, combined.replace(" ", ""), self.completed_event_title.replace(" ", "")).ratio()
        return ratio >= 0.70

    def run(self):
        while True:
            if self.pause_getter():
                self.msleep(200)
                continue
            if not is_game_hovered_or_active():
                self.journey_status_changed.emit(False)
                self.pending_event = ""
                self.pending_count = 0
                self.msleep(1000)
                continue

            if self.reader is None:
                self.model_loading_started.emit()
                print("🪶 [지연 로딩] 게임 감지됨 → 제목 인식용 OCR 모델 로딩 시작...")
                # ⚠️ [GPU 크래시 이슈] 게임이 전체화면으로 GPU를 붙잡고 있는 바로 그 타이밍에
                # CUDA 컨텍스트를 새로 잡으려다 드라이버 레벨에서 죽는(=앱 전체가 조용히
                # 사라지는) 사례가 있어 기본은 CPU. 설정에서 GPU 가속을 켠 유저만 시도하고,
                # 실패하면 CPU로 폴백한다 (단, 드라이버 레벨 크래시는 이 try/except로도 못 막음).
                if self.gpu_ocr_getter():
                    try:
                        self.reader = easyocr.Reader(['ko'], gpu=True)
                    except Exception:
                        self.reader = easyocr.Reader(['ko'], gpu=False)
                else:
                    self.reader = easyocr.Reader(['ko'], gpu=False)
                print("🪶 [지연 로딩] 제목 인식용 OCR 모델 로딩 완료")

            global _auto_calibration_done, _auto_calibration_attempts, _last_calibrated_rect
            _game_rect = get_game_window_rect()
            if _game_rect:
                if not _auto_calibration_done and _auto_calibration_attempts < 25:
                    _auto_calibration_attempts += 1
                    try_auto_calibrate_regions(self.reader, _game_rect)
                elif _last_calibrated_rect and (
                    abs(_game_rect["left"] - _last_calibrated_rect["left"]) > 5 or
                    abs(_game_rect["top"] - _last_calibrated_rect["top"]) > 5 or
                    abs(_game_rect["width"] - _last_calibrated_rect["width"]) > 5 or
                    abs(_game_rect["height"] - _last_calibrated_rect["height"]) > 5
                ):
                    print("🪟 [게임 창 변경 감지] 위치/크기가 바뀜 → 캡처 영역 재보정")
                    global _calibration_samples
                    _calibration_samples = []
                    try_auto_calibrate_regions(self.reader, _game_rect)

            try:
                monitor = compute_region(REGION_FRACTIONS["event_title"], self.screen_w, self.screen_h)
                img = np.array(self.sct.grab(monitor))
                gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

                title_diff = 999.0
                if self._last_title_gray is not None and gray.shape == self._last_title_gray.shape:
                    title_diff = cv2.absdiff(gray, self._last_title_gray).mean()

                diff_limit = 3.0 if self.low_spec_getter() else 1.0  # ⚡ 최적화 모드 켜면 3.0 적용
                if title_diff < diff_limit:
                    combined = self._cached_combined
                    raw_joined = self._cached_raw_joined
                else:
                    self._last_title_gray = gray.copy()
                    
                    resized = cv2.resize(gray, None, fx=1.3, fy=1.3, interpolation=cv2.INTER_LINEAR)
                    results = self.reader.readtext(resized, detail=0, decoder='greedy')  
                    raw_joined = " ".join(results).strip()
                    valid_words = [w for text in results for w in re.findall(r'[가-힣]{2,}', text) if w not in NOISE_EVENT_WORDS]
                    combined = fix_ocr_typos(" ".join(valid_words).strip())
                    
                    self._cached_combined = combined
                    self._cached_raw_joined = raw_joined

                if len(combined.replace(" ", "")) >= 2:
                    if self.is_waiting_for_next_event:
                        if self._is_same_as_completed(combined):
                            self.msleep(150)
                            continue
                        else:
                            self.is_waiting_for_next_event = False
                            self.completed_event_title = ""

                    self.empty_count = 0
                    self.journey_status_changed.emit(True)

                    if combined == self.pending_event:
                        self.pending_count += 1
                    else:
                        self.pending_event = combined
                        self.pending_count = 1

                    if self._is_same_as_last(combined):
                        pass
                    elif self.pending_count >= 1 and combined != self.last_event:
                        print(f"🔍 [OCR RAW] '{raw_joined}' → [보정] '{combined}'")
                        self.last_event = combined
                        print(f"📌 [OCR 감지] {combined}")
                        self.event_detected.emit(combined)
                else:
                    self.pending_event = ""
                    self.pending_count = 0
                    if self.is_waiting_for_next_event:
                        self.empty_count = 0
                    else:
                        self.empty_count += 1
                        if self.empty_count >= 8:
                            self.journey_status_changed.emit(False)
                            self.last_event = ""

                try:
                    label_monitor = compute_region(REGION_FRACTIONS["arcana_label"], self.screen_w, self.screen_h)
                    label_img = np.array(self.sct.grab(label_monitor))
                    label_gray = cv2.cvtColor(label_img, cv2.COLOR_BGRA2GRAY)

                    label_diff = 999.0
                    if self._last_label_gray is not None and label_gray.shape == self._last_label_gray.shape:
                        label_diff = cv2.absdiff(label_gray, self._last_label_gray).mean()

                    label_diff_thresh = 2.0 if self.low_spec_getter() else 1.0
                    if label_diff < label_diff_thresh:
                        label_text = self._cached_label_text
                    else:
                        self._last_label_gray = label_gray.copy()
                        label_resized = cv2.resize(label_gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
                        label_results = self.reader.readtext(label_resized, detail=0, decoder='greedy')
                        label_text = " ".join(label_results).strip()
                        label_text = label_text.replace("제목", "").strip()
                        label_text = re.sub(r'(^|\s)제(\s|$)', r'\1\2', label_text).strip()
                        label_text = re.sub(r'\s+', ' ', label_text)
                        self._cached_label_text = label_text

                    now_l = time.time()
                    _ARCANA_VARIANTS = ("아르카나", "알카나", "알카", "아드가나", "아드카나", "아르가나")
                    is_arcana_hit = any(v in label_text for v in _ARCANA_VARIANTS)

                    if is_arcana_hit:
                        if not self.is_arcana_label and now_l - self._last_label_log > 2:
                            self._last_label_log = now_l
                            print(f"🏷️ [라벨] '아르카나 이벤트'로 전환 (인식문구:'{label_text}') → 카드 스캔 재개")
                        self.is_arcana_label = True
                    elif label_text:
                        if self.is_arcana_label and now_l - self._last_label_log > 2:
                            self._last_label_log = now_l
                            print(f"🏷️ [라벨] '{label_text}'(비-아르카나)로 전환 → 카드 스캔 일시중지")
                        self.is_arcana_label = False
                    self.arcana_label_detected.emit(self.is_arcana_label)
                except Exception:
                    pass

            except Exception as e:
                print(f"⚠️ [EventTitleOCR 예외] {e}")

            # ⚡ [최적화 모드 반영] 일반 200ms / 최적화 350ms
            sleep_ms = 350 if self.low_spec_getter() else 200
            self.msleep(sleep_ms)

class EventCardArtThread(QThread):
    card_matched = pyqtSignal(str, str)
    preview_signal = pyqtSignal(np.ndarray)
    raw_preview_signal = pyqtSignal(np.ndarray)

    def __init__(self, screen_w, screen_h, hash_cache_getter, pause_getter=None, lenient_getter=None, low_spec_getter=None):
        super().__init__()
        self.sct = mss.MSS()
        self.screen_w, self.screen_h = screen_w, screen_h
        self.get_hash_cache = hash_cache_getter
        self.pending_id = None
        self.pending_count = 0
        self.last_matched_id = ""
        self.pause_getter = pause_getter or (lambda: False)
        self.lenient_getter = lenient_getter or (lambda: False)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self._last_heartbeat = 0
        self._fail_streak_start = None
        self._recalib_triggered_this_streak = False
        self._last_card_gray = None  # ⚡ [최적화] 프레임 비교 캐싱용

    def run(self):
        print("🃏 [카드 인식 스레드 시작] 화면 감시 중...")
        while True:
            if self.pause_getter():
                self.msleep(200)
                continue
            if not is_game_hovered_or_active():
                self.msleep(1000)
                continue

            cache = self.get_hash_cache()
            if not cache:
                now = time.time()
                if now - self._last_heartbeat > 5:
                    self._last_heartbeat = now
                    print("🃏 [카드 인식 대기] 아직 카드 인덱싱이 끝나지 않아 비교할 데이터가 없음...")
                self.msleep(1000)
                continue

            try:
                monitor = compute_region(REGION_FRACTIONS["card_art"], self.screen_w, self.screen_h)
                raw_grab = np.array(self.sct.grab(monitor))
                cv_bgr = cv2.cvtColor(raw_grab, cv2.COLOR_BGRA2BGR)

                # ⚡ [최적화] 카드 영역 변화 없으면 무거운 원근 보정 / pHash 연산 스킵!
                gray_curr = cv2.cvtColor(cv_bgr, cv2.COLOR_BGR2GRAY)
                card_diff = 999.0
                if self._last_card_gray is not None and gray_curr.shape == self._last_card_gray.shape:
                    card_diff = float(cv2.absdiff(gray_curr, self._last_card_gray).mean())

                card_diff_thresh = 2.5 if self.low_spec_getter() else 1.0
                if card_diff < card_diff_thresh:
                    sleep_ms = 400 if self.low_spec_getter() else 200
                    self.msleep(sleep_ms)
                    continue

                self._last_card_gray = gray_curr.copy()
                
                self.raw_preview_signal.emit(cv_bgr.copy())
                cap_phash, cap_hist, flat_card = extract_card_features(cv_bgr)
                self.preview_signal.emit(flat_card)

                if any((cap_phash - h) <= NON_CARD_HASH_THRESHOLD for h in KNOWN_NON_CARD_HASHES):
                    self.pending_id = None
                    self.pending_count = 0
                    sleep_ms = 350 if self.low_spec_getter() else 200
                    self.msleep(sleep_ms)
                    continue

                all_scores = []
                best_id, best_name, best_score = None, None, 999.0
                best_color_corr = -1.0
                best_pdist = 99

                for card in cache:
                    p_dist = cap_phash - card['hash']
                    color_corr = cv2.compareHist(cap_hist, card['hist'], cv2.HISTCMP_CORREL)
                    score = float(p_dist) - (color_corr * 25.0)
                    all_scores.append((score, card['name'], p_dist))

                    if score < best_score:
                        best_score = score
                        best_id = card['id']
                        best_name = card['name']
                        best_color_corr = color_corr
                        best_pdist = p_dist

                if best_id is not None and (
                    (self.lenient_getter() and best_score < 19.0) or
                    (best_score < 7.1 and best_pdist <= 35)
                ):
                    self._fail_streak_start = None
                    self._recalib_triggered_this_streak = False

                    if best_id == self.pending_id:
                        self.pending_count += 1
                    else:
                        self.pending_id = best_id
                        self.pending_count = 1

                    if self.pending_count >= 1 and best_id != self.last_matched_id:
                        self.last_matched_id = best_id
                        if self.lenient_getter():
                            print(f"🃏 [이격 아르카나 매칭] 카드: '{best_name}' (점수: {best_score:.1f} /)")
                        else:
                            print(f"🃏 [카드 매칭 성공] 카드: '{best_name}' (점수: {best_score:.1f})")

                        self.card_matched.emit(best_id, best_name)
                else:
                    self.pending_id = None
                    self.pending_count = 0
                    now = time.time()
                    if now - self._last_heartbeat > 5:
                        self._last_heartbeat = now
                        top3 = sorted(all_scores, key=lambda x: x[0])[:3]
                        top3_str = ", ".join([f"'{n}'({s:.1f})" for s, n, p in top3])
                        print(f"🃏 [카드 판독 중] 상위 3개 후보: {top3_str} — 임계값 미달, 확정 안 함")

                    if self._fail_streak_start is None:
                        self._fail_streak_start = now
                    elif not self._recalib_triggered_this_streak and now - self._fail_streak_start > 10:
                        self._recalib_triggered_this_streak = True
                        self._trigger_recalibration()
            except Exception:
                pass

            # ⚡ [최적화 모드 반영] 일반 200ms / 최적화 400ms
            sleep_ms = 400 if self.low_spec_getter() else 200
            self.msleep(sleep_ms)

    def _trigger_recalibration(self):
        global _auto_calibration_done, _calibration_samples, _auto_calibration_attempts, _card_region_manual_override
        if _card_region_manual_override:
            print("🃏 [카드 인식 실패 지속] 사용자가 직접 고정한 영역이 있어 자동 재보정을 건너뜀")
            return
        print("🃏 [카드 인식 실패 지속(10초+)] 캡처 영역이 잘못됐을 가능성 → 자동 보정을 처음부터 다시 시도")
        _auto_calibration_done = False
        _calibration_samples = []
        _auto_calibration_attempts = 0

class ChoiceScannerThread(QThread):
    choices_scanned = pyqtSignal(list)

    def __init__(self, screen_w, pause_getter=None, options_getter=None, low_spec_getter=None, gpu_ocr_getter=None):
        super().__init__()
        self.reader = None
        self.sct = mss.MSS()
        self.screen_w = screen_w
        self.pause_getter = pause_getter or (lambda: False)
        self.options_getter = options_getter or (lambda: [])
        self.gpu_ocr_getter = gpu_ocr_getter or (lambda: True)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self.has_scanned_current_event = False
        self._last_proj_fail_log = 0

    def reset_cache(self):
        self.has_scanned_current_event = False

    def find_matching_option(self, ocr_text, current_options):
        if not current_options: return None
        clean_ocr = re.sub(r'^[\d]+[\.\s\)]', '', ocr_text).strip()
        clean_ocr = re.sub(r'[^가-힣0-9]', '', fix_ocr_typos(clean_ocr))
        if not clean_ocr or len(clean_ocr) < 2: return None

        best_match = None
        best_score = 0
        for opt in current_options:
            choice_name = opt.get('choice', '')
            clean_choice = re.sub(r'[^가-힣0-9]', '', choice_name)
            if not clean_choice: continue
            if clean_ocr in clean_choice or clean_choice in clean_ocr: return opt

            match_count = sum(1 for char in clean_ocr if char in clean_choice)
            score = match_count / max(len(clean_ocr), len(clean_choice))
            stem = clean_choice[:3] if len(clean_choice) >= 3 else clean_choice
            if stem in clean_ocr or clean_ocr[:3] in clean_choice: score += 0.45
            if score > best_score and score >= 0.45:
                best_score = score
                best_match = opt

        return best_match

    def detect_button_zones_projection(self, gray_img, monitor_top, options, base_top=None, base_h=None):
        try:
            h_img, w_img = gray_img.shape[:2]
            text_strip = gray_img[:, int(w_img * 0.10):int(w_img * 0.90)]

            _std = float(text_strip.std())
            if _std < 8:
                return None, f"영역 전체가 균일함(대비 낮음, std={_std:.1f}) → 선택지 UI가 아닌 것으로 추정"

            # ⚡ [최빈값 기반 검출] 고정 밝기(110)나 Otsu 2분할 대신, 크롭 영역에서 가장 많이
            # 나오는 톤(=반투명 선택지 박스의 배경색)을 기준으로 삼는다. 캡처 영역을 이미
            # 박스 위치에 맞춰 좁혀뒀기 때문에 박스 배경이 크롭의 대부분을 차지하므로,
            # 이 톤에서 크게 벗어나는 픽셀(=글자)이 일정 비율 섞인 행만 "텍스트 행"으로 판단.
            # 배경이 낮/밤 어느 쪽으로 바뀌어도(박스가 배경보다 밝든 어둡든) 동일하게 동작한다.
            _hist = np.bincount(text_strip.flatten(), minlength=256)
            box_fill = int(np.argmax(_hist))
            diff = np.abs(text_strip.astype(np.int16) - box_fill)
            row_box_ratio = (diff < 15).sum(axis=1) / float(text_strip.shape[1])
            row_text_ratio = (diff > 50).sum(axis=1) / float(text_strip.shape[1])
            active_rows = (row_box_ratio > 0.3) & (row_text_ratio > 0.02)

            if not active_rows.any():
                return None, f"박스 배경톤({box_fill}) 대비 텍스트로 볼 만한 행 없음 (최대 텍스트 비율={row_text_ratio.max()*100:.1f}%)"

            rects = []
            in_block = False
            start_y = 0

            for y, is_active in enumerate(active_rows):
                if is_active and not in_block:
                    in_block = True
                    start_y = y
                elif not is_active and in_block:
                    in_block = False
                    if (y - start_y) >= 10:
                        rects.append((start_y, y))
            if in_block and (h_img - start_y) >= 10:
                rects.append((start_y, h_img))

            merged = []
            for r in rects:
                if not merged:
                    merged.append(r)
                else:
                    prev_y1, prev_y2 = merged[-1]
                    if r[0] <= prev_y2 + 22:
                        merged[-1] = (prev_y1, max(prev_y2, r[1]))
                    else:
                        merged.append(r)

            merged = [r for r in merged if (r[1] - r[0]) >= 12]

            if len(merged) == len(options):
                if base_top is not None and base_h is not None:
                    bottom_anchor_y = base_top + int(base_h * 0.724)
                    pitch_px = int(base_h * 0.07037)
                    y_sanity_tolerance = max(12, int(base_h * 0.015))
                    count = len(options)
                    for i, (y1, y2) in enumerate(merged):
                        center_y_abs = monitor_top + (y1 + y2) // 2
                        expected_cy = bottom_anchor_y - (count - 1 - i) * pitch_px
                        if abs(center_y_abs - expected_cy) > y_sanity_tolerance:
                            return None, f"블록 {len(merged)}개는 찾았지만 예상 Y좌표와 어긋남(임계값 {y_sanity_tolerance}px 초과)"

                zones = []
                btn_half = 34
                for i, (y1, y2) in enumerate(merged):
                    center_y = (y1 + y2) // 2
                    zones.append({
                        'y_min': monitor_top + center_y - btn_half,
                        'y_max': monitor_top + center_y + btn_half,
                        'option': options[i],
                        'text': options[i].get('choice', '')
                    })
                return zones, "성공"
            return None, f"검출된 텍스트 블록 {len(merged)}개 ≠ 선택지 {len(options)}개"
        except Exception as e:
            return None, f"예외 발생: {e}"

    def run(self):
        while True:
            if self.pause_getter():
                self.msleep(150)
                continue

            if not is_game_hovered_or_active():
                self.choices_scanned.emit([])
                self.has_scanned_current_event = False
                self.msleep(500)
                continue

            if self.reader is None:
                print("🪶 [지연 로딩] 게임 감지됨 → 선택지 정밀 스캔용 OCR 모델 로딩 시작...")
                # ⚠️ [GPU 크래시 이슈] 위 제목 인식 스레드와 동일한 이유 — 설정에서 켠 유저만 시도.
                if self.gpu_ocr_getter():
                    try:
                        self.reader = easyocr.Reader(['ko'], gpu=True)
                    except Exception:
                        self.reader = easyocr.Reader(['ko'], gpu=False)
                else:
                    self.reader = easyocr.Reader(['ko'], gpu=False)
                print("🪶 [지연 로딩] 선택지 정밀 스캔용 OCR 모델 로딩 완료")

            options = self.options_getter() or []
            if not options or len(options) < 2:
                self.choices_scanned.emit([])
                self.has_scanned_current_event = False
                self.msleep(300)
                continue

            if self.has_scanned_current_event:
                sleep_ms = 500 if self.low_spec_getter() else 300
                self.msleep(sleep_ms)
                continue

            _scan_attempt_start = time.time()

            game_rect = get_game_window_rect()
            if game_rect:
                base_left, base_top, base_w, base_h = game_rect["left"], game_rect["top"], game_rect["width"], game_rect["height"]
            else:
                base_left, base_top, base_w, base_h = 0, 0, self.screen_w, 1080

            monitor = {
                "top": base_top + int(base_h * 0.35),
                "left": base_left + int(base_w * 0.655),
                "width": int(base_w * 0.345),
                "height": int(base_h * 0.47)
            }

            # ⚡ [2차 OCR 캡처 영역 축소] 이미 1차 비율 추정(anchor/pitch 공식)으로
            # 선택지들이 대략 어디 있을지 알고 있으므로, 전체 34%x47% 영역 대신
            # 예상 위치 주변만 캡처해서 무거운 readtext 연산량을 줄인다.
            # (선택지 텍스트가 2줄로 늘어나는 경우를 감안해 여유 margin을 넉넉히 둠)
            _count = len(options)
            _bottom_anchor_y = base_top + int(base_h * 0.724)
            _pitch_px = int(base_h * 0.07037)
            _top_expected_cy = _bottom_anchor_y - (_count - 1) * _pitch_px
            _margin = int(_pitch_px * 0.7)
            _tight_top = max(monitor["top"], _top_expected_cy - _margin)
            _tight_bottom = min(monitor["top"] + monitor["height"], _bottom_anchor_y + _margin)
            if _tight_bottom > _tight_top:
                monitor["top"] = _tight_top
                monitor["height"] = _tight_bottom - _tight_top

            try:
                img = np.array(self.sct.grab(monitor))
                gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

                proj_zones, proj_reason = self.detect_button_zones_projection(gray, monitor["top"], options, base_top, base_h)
                if proj_zones:
                    if options is not (self.options_getter() or []):
                        self.has_scanned_current_event = False
                        self.msleep(50)
                        continue
                    self.has_scanned_current_event = True
                    print(f"📊 [1차 프로젝션 검출 성공] 총 {len(proj_zones)}개 선택지 위치 확정")
                    self.choices_scanned.emit(proj_zones)
                    sleep_ms = 500 if self.low_spec_getter() else 300
                    self.msleep(sleep_ms)
                    continue

                _now_pf = time.time()
                if _now_pf - self._last_proj_fail_log > 5:
                    self._last_proj_fail_log = _now_pf
                    print(f"🟡 [1차 프로젝션 실패 → 2차 OCR 폴백] {proj_reason}")

                allow_chars = set()
                for opt in options:
                    allow_chars.update(opt.get('choice', ''))
                allowlist = ''.join(sorted(allow_chars)) if allow_chars else None

                _ocr_start = time.time()
                if allowlist:
                    detections = self.reader.readtext(gray, detail=1, decoder='greedy', allowlist=allowlist)
                else:
                    detections = self.reader.readtext(gray, detail=1, decoder='greedy')
                _ocr_ms = (time.time() - _ocr_start) * 1000
                if _ocr_ms > 500:
                    print(f"🐢 [선택지 2차 OCR 소요] {_ocr_ms:.0f}ms (torch 스레드={torch.get_num_threads()})")

                items = []
                for bbox, text, conf in detections:
                    ys = [p[1] for p in bbox]
                    xs = [p[0] for p in bbox]
                    items.append({
                        'y_center': sum(ys) / len(ys),
                        'x_center': sum(xs) / len(xs),
                        'min_y': min(ys),
                        'max_y': max(ys),
                        'text': text
                    })
                items.sort(key=lambda t: t['y_center'])

                line_gap = 25.0
                clusters = []
                for item in items:
                    if clusters and (item['y_center'] - (sum(p['y_center'] for p in clusters[-1]) / len(clusters[-1]))) <= line_gap:
                        clusters[-1].append(item)
                    else:
                        clusters.append([item])

                zones = []
                btn_half = 34
                used_option_ids = set()

                bottom_anchor_y = base_top + int(base_h * 0.724)
                est_pitch_px = int(base_h * 0.07037)
                count_opts = len(options)
                expected_cy_by_option_id = {}
                for idx, opt in enumerate(options):
                    expected_cy_by_option_id[id(opt)] = bottom_anchor_y - (count_opts - 1 - idx) * est_pitch_px
                y_sanity_tolerance = max(12, int(base_h * 0.015))

                for cluster in clusters:
                    cluster.sort(key=lambda p: p['x_center'])
                    combined_text = fix_ocr_typos(" ".join(p['text'] for p in cluster).strip())

                    cluster_y_center = (min(p['min_y'] for p in cluster) + max(p['max_y'] for p in cluster)) / 2.0

                    abs_y_min = monitor["top"] + cluster_y_center - btn_half
                    abs_y_max = monitor["top"] + cluster_y_center + btn_half
                    abs_y_center = monitor["top"] + cluster_y_center

                    available_options = [o for o in options if id(o) not in used_option_ids]
                    matched_opt = self.find_matching_option(combined_text, available_options)
                    if matched_opt:
                        expected_cy = expected_cy_by_option_id.get(id(matched_opt))
                        if expected_cy is not None and abs(abs_y_center - expected_cy) > y_sanity_tolerance:
                            matched_opt = None
                    if matched_opt:
                        used_option_ids.add(id(matched_opt))
                        zones.append({
                            'y_min': abs_y_min,
                            'y_max': abs_y_max,
                            'option': matched_opt,
                            'text': matched_opt.get('choice', combined_text)
                        })

                if len(zones) > 0 and len(zones) < len(options):
                    known_zone = zones[0]
                    known_idx = options.index(known_zone['option']) if known_zone['option'] in options else 0
                    known_center_y = (known_zone['y_min'] + known_zone['y_max']) / 2.0
                    
                    pitch_px = int(base_h * 0.07037)
                    restored_zones = []
                    
                    for idx, opt in enumerate(options):
                        offset = idx - known_idx
                        target_center_y = known_center_y + (offset * pitch_px)
                        restored_zones.append({
                            'y_min': target_center_y - btn_half,
                            'y_max': target_center_y + btn_half,
                            'option': opt,
                            'text': opt.get('choice', '')
                        })
                    zones = restored_zones

                if zones:
                    if options is not (self.options_getter() or []):
                        self.has_scanned_current_event = False
                        self.msleep(50)
                        continue
                    self.has_scanned_current_event = True
                    print(f"📝 [2차 OCR 텍스트 매칭 성공] 총 {len(zones)}개 선택지 위치 확정")
                    self.choices_scanned.emit(zones)
                    sleep_ms = 500 if self.low_spec_getter() else 300
                    self.msleep(sleep_ms)
                    continue

            except Exception:
                pass

            sleep_ms = 500 if self.low_spec_getter() else 300
            self.msleep(sleep_ms)


# ----------------------------------------------------
# 5. 아르카나 카드 65종 인덱서
# ----------------------------------------------------
def card_name_to_img_path(name):
    return f"/images/cards/{name.replace(' ', '')}.webp"

JSON_BASE_URL = "https://star-savior-arcana-db.pages.dev/data"
STAT_CODE_MAP = {
    "JST_POWER": "힘", "JST_FOCUS": "집중", "JST_ENDURANCE": "인내",
    "JST_HEALTH": "체력", "JST_PROTECT": "보호",
}

def _fmt_reward_value(entry):
    mn, mx = entry.get("min"), entry.get("max")
    if mn is None: return ""
    if mx is not None and mx != mn:
        return f"{mn}~{mx}"
    sign = "+" if mn >= 0 else ""
    return f"{sign}{mn}"

def translate_reward_entry(entry, journey_buffs_map, potentials_map, items_map=None):
    items_map = items_map or {}
    t = entry.get("type", "")
    if t == "RT_STAT":
        stat = STAT_CODE_MAP.get(entry.get("reward_stat"), entry.get("reward_stat", "?"))
        return f"{stat} {_fmt_reward_value(entry)}".strip()
    if t == "RT_STAMINA":
        return f"스태미나 {_fmt_reward_value(entry)}".strip()
    if t == "RT_CONDITION":
        return f"컨디션 {_fmt_reward_value(entry)}".strip()
    if t == "RT_POTEN_POINT" or t == "RR_PP_USE" or t == "RT_PP_USE":
        return f"잠재력 포인트 {_fmt_reward_value(entry)}".strip()
    if t == "RT_AP":
        return f"인연 포인트 {_fmt_reward_value(entry)}".strip()
    if t == "RT_COIN":
        return f"오래된 동전 {_fmt_reward_value(entry)}".strip()
    if t == "RT_JOURNEY_BUFF":
        buff = journey_buffs_map.get(entry.get("reward_id"))
        name = buff['name'].get('ko-KR', '?') if buff else '?'
        val = _fmt_reward_value(entry)
        return f"여정 버프 획득 {name}" + (f" {val}턴" if val else "")
    if t == "RT_JOURNEY_BUFF_REMOVE_POS":
        return "이로운 여정 버프 제거"
    if t == "RT_JOURNEY_ITEM":
        item = items_map.get(entry.get("reward_id"))
        name = item['name'].get('ko-KR', '?') if item else '?'
        val = _fmt_reward_value(entry)
        return f"{name}" + (f" {val}" if val else "")
    if t == "RT_SE_POTEN":
        poten = potentials_map.get(entry.get("reward_id"))
        name = poten['name'].get('ko-KR', '?') if poten else '?'
        mn, mx = entry.get("min"), entry.get("max")
        if mn is not None:
            if mn == mx or mx is None:
                return f"특수 잠재력: {name} {mn * 10}% 할인"
            return f"특수 잠재력: {name} {mn * 10}~{mx * 10}% 할인"
        return f"특수 잠재력: {name}"
    return f"{t} {_fmt_reward_value(entry)}".strip()

def translate_reward_groups(reward_groups, journey_buffs_map, potentials_map, items_map=None):
    lines = []
    for group in (reward_groups or []):
        group_lines = []
        for entry in group:
            try:
                group_lines.append(translate_reward_entry(entry, journey_buffs_map, potentials_map, items_map))
            except Exception:
                continue
        if not group_lines:
            continue
        if len(group_lines) > 1:
            lines.append(' or '.join(group_lines))
        else:
            lines.append(group_lines[0])
    return lines

CONDITION_CODE_MAP = {
    "RR_COIN_USE": lambda c: f"오래된 동전 사용 {c.get('value')}",
    "RR_STAMINA_USE": lambda c: f"스태미나 사용 {c.get('value')}",
    "RR_CONDITION_USE": lambda c: f"컨디션 사용 {c.get('value')}",
    "RR_PP_USE": lambda c: f"잠재력 포인트 사용 {c.get('value')}",
    "RR_STAT": lambda c: f"{STAT_CODE_MAP.get(c.get('target'), c.get('target', '?'))} {c.get('value')} 이상 필요",
}

def translate_condition_entry(condition, items_map=None):
    if not condition:
        return None
    items_map = items_map or {}
    t = condition.get("type", "")
    val = condition.get("value")

    if t == "RR_ITEM_USE":
        item_id = condition.get("target") or condition.get("item_id") or condition.get("reward_id") or condition.get("target_item_id") or condition.get("id")
        item = None
        if item_id is not None:
            item = items_map.get(item_id)
            if item is None:
                try:
                    item = items_map.get(int(item_id))
                except (TypeError, ValueError):
                    pass
        item_name = None
        if item:
            name_field = item.get('name')
            item_name = name_field.get('ko-KR', '?') if isinstance(name_field, dict) else str(name_field or '')
        item_name = item_name or "아이템"
        return f"{item_name} 사용 {val}".strip() if val is not None else f"{item_name} 사용"

    fmt = CONDITION_CODE_MAP.get(t)
    if fmt:
        try:
            return fmt(condition)
        except Exception:
            pass
    return f"{t} {val}".strip() if val is not None else (t or None)

def _build_choice_reward_lines(success_lines, fail_lines):
    if not success_lines and not fail_lines:
        success_lines = ['없음']
    return [f"[SUCCESS]{l}" for l in success_lines] + [f"[FAIL]{l}" for l in fail_lines]

def parse_single_event_json(ev, journey_buffs_map, potentials_map, is_arcana=False, items_map=None):
    name_field = ev.get('name') or {}
    raw_title = name_field.get('ko-KR', '') if isinstance(name_field, dict) else str(name_field or '')
    if not raw_title:
        return None

    clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', raw_title)
    options = []
    for choice in ev.get('choices', []):
        choice_name_field = choice.get('name', '')
        choice_text = choice_name_field.get('ko-KR', '') if isinstance(choice_name_field, dict) else str(choice_name_field or '')
        if not choice_text:
            choice_text = raw_title

        reward_lines = []
        cond_text = translate_condition_entry(choice.get('condition'), items_map)
        if cond_text:
            reward_lines.append(cond_text)

        success_lines = translate_reward_groups(choice.get('success_rewards'), journey_buffs_map, potentials_map, items_map)
        fail_lines = translate_reward_groups(choice.get('failure_rewards'), journey_buffs_map, potentials_map, items_map)
        reward_lines += _build_choice_reward_lines(success_lines, fail_lines)
        options.append({"choice": choice_text, "reward": "\n".join(reward_lines), "has_condition": bool(cond_text)})

    return {"rawTitle": raw_title, "cleanTitle": clean_title, "options": options, "isArcana": is_arcana}

def parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map=None, hidden_difficulties=None):
    hidden_difficulties = hidden_difficulties or set()
    if not variants:
        return None

    first = variants[0]
    name_field = first.get('name') or {}
    raw_title = name_field.get('ko-KR', '') if isinstance(name_field, dict) else str(name_field or '')
    if not raw_title:
        raw_title = str(event_name or '')
    if not raw_title:
        return None
    clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', raw_title)

    first_choices = first.get('choices', []) or []
    options = []
    for choice_idx in range(len(first_choices)):
        choice_name_field = (first_choices[choice_idx] or {}).get('name', '')
        choice_text = choice_name_field.get('ko-KR', '') if isinstance(choice_name_field, dict) else str(choice_name_field or '')
        if not choice_text:
            choice_text = raw_title

        diff_entries = []
        any_condition = False
        for variant in variants:
            variant_choices = variant.get('choices', []) or []
            if choice_idx >= len(variant_choices):
                continue
            choice = variant_choices[choice_idx] or {}

            diff_label = None
            diffs = variant.get('difficulties') or []
            if diffs:
                d0 = diffs[0]
                diff_label = d0.get('ko-KR', '') if isinstance(d0, dict) else str(d0)

            if diff_label and diff_label.strip() in hidden_difficulties:
                continue

            cond_text = translate_condition_entry(choice.get('condition'), items_map)
            if cond_text:
                any_condition = True

            try:
                success_lines = translate_reward_groups(choice.get('success_rewards'), journey_buffs_map, potentials_map, items_map)
                fail_lines = translate_reward_groups(choice.get('failure_rewards'), journey_buffs_map, potentials_map, items_map)
            except Exception:
                success_lines, fail_lines = [], []

            is_trivial = not cond_text and not success_lines and not fail_lines
            content_lines = ([cond_text] if cond_text else []) + _build_choice_reward_lines(success_lines, fail_lines)

            if diff_label or content_lines:
                diff_entries.append({'label': diff_label, 'content': content_lines, 'trivial': is_trivial})

        if diff_entries and all(e['trivial'] for e in diff_entries):
            reward_lines = diff_entries[0]['content']
        else:
            reward_lines = []
            for i, e in enumerate(diff_entries):
                if i > 0:
                    reward_lines.append('or')
                block = ([e['label']] if e['label'] else []) + e['content']
                reward_lines.extend(block)

        options.append({"choice": choice_text, "reward": "\n".join(reward_lines), "has_condition": any_condition})

    return {"rawTitle": raw_title, "cleanTitle": clean_title, "options": options, "isArcana": False}

def build_caches_from_all_json(arcanas_data, journeys_data, journey_buffs_map, potentials_map, items_map=None, hidden_difficulties=None):
    cards_for_indexing = []
    db_event_cache = []
    card_event_history = {}
    resolved_card_events = {}
    card_id_map = {}
    title_to_cards = {}
    title_to_events = {}

    for card in (arcanas_data or []):
        try:
            card_name = (card.get('name') or {}).get('ko-KR', '')
            if not card_name or card_name in EXCLUDED_CARD_NAMES:
                continue
            card_id_map[card_name] = str(card.get('id', ''))
            cards_for_indexing.append({"name": card_name, "img": card_name_to_img_path(card_name)})

            events = []
            for ev in card.get('events', []):
                parsed = parse_single_event_json(ev, journey_buffs_map, potentials_map, is_arcana=True, items_map=items_map)
                if parsed:
                    events.append(parsed)
                    db_event_cache.append(parsed)
                    if parsed.get('cleanTitle'):
                        title_to_cards.setdefault(parsed['cleanTitle'], set()).add(card_name)
                        title_to_events.setdefault(parsed['cleanTitle'], []).append(parsed)

            card_event_history[card_name] = events
            if events:
                resolved_card_events[card_name] = {"rawTitle": events[0]['rawTitle'], "options": events[0]['options']}
        except Exception:
            continue

    ambiguous_arcana_titles = {t for t, cards in title_to_cards.items() if len(cards) > 1}

    def _event_signature(ev):
        return tuple(sorted((o.get('choice', ''), o.get('reward', '')) for o in ev.get('options', [])))

    reward_diff_titles = set()
    for t in ambiguous_arcana_titles:
        evs = [e for e in title_to_events.get(t, []) if len(e.get('options', [])) >= 2]
        sigs = {_event_signature(ev) for ev in evs}
        if len(sigs) > 1:
            reward_diff_titles.add(t)

    if ambiguous_arcana_titles:
        print(f"🃏 [동명 이벤트 감지] {len(ambiguous_arcana_titles)}개 제목이 2개 이상의 카드에서 공유됨 → 약한 매칭 처리")

    journey_count = 0
    if isinstance(journeys_data, dict):
        for event_name, variants in journeys_data.items():
            try:
                if isinstance(variants, dict):
                    variants = [variants]
                if not isinstance(variants, list):
                    continue
                parsed = parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map, hidden_difficulties)
                if parsed:
                    db_event_cache.append(parsed)
                    journey_count += 1
            except Exception:
                continue
    elif isinstance(journeys_data, list):
        grouped = {}
        for ev in journeys_data:
            if not isinstance(ev, dict):
                continue
            name_field = ev.get('name') or {}
            title = name_field.get('ko-KR', '') if isinstance(name_field, dict) else str(name_field or '')
            grouped.setdefault(title, []).append(ev)
        for event_name, variants in grouped.items():
            try:
                parsed = parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map, hidden_difficulties)
                if parsed:
                    db_event_cache.append(parsed)
                    journey_count += 1
            except Exception:
                continue

    print(f"📦 [통합 DB 구성 완료] 아르카나 카드 {len(cards_for_indexing)}종, 일반 여정 이벤트 {journey_count}개 탑재 완료!")

    return {
        'cards': cards_for_indexing,
        'db_event_cache': db_event_cache,
        'card_event_history': card_event_history,
        'resolved_card_events': resolved_card_events,
        'card_id_map': card_id_map,
        'ambiguous_arcana_titles': ambiguous_arcana_titles,
        'title_to_cards': title_to_cards,
    }

class JsonDataLoadThread(QThread):
    finished_signal = pyqtSignal(dict)

    def run(self):
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36'}
        try:
            arcanas = requests.get(f"{JSON_BASE_URL}/arcanas.json", headers=headers, timeout=15).json()
            journey_buffs = requests.get(f"{JSON_BASE_URL}/journey_buffs.json", headers=headers, timeout=15).json()
            potentials = requests.get(f"{JSON_BASE_URL}/potentials.json", headers=headers, timeout=15).json()

            journeys = []
            for ep in ["journeys.json", "journey.json"]:
                try:
                    res = requests.get(f"{JSON_BASE_URL}/{ep}", headers=headers, timeout=8)
                    if res.status_code == 200:
                        journeys = res.json()
                        break
                except Exception:
                    pass

            items = requests.get(f"{JSON_BASE_URL}/journey_items.json", headers=headers, timeout=15).json()

        except Exception as e:
            print(f"⚠️ [JSON 데이터 로드 실패] {e}")
            self.finished_signal.emit({'error': str(e)})
            return

        saviors = []
        try:
            saviors = requests.get(f"{JSON_BASE_URL}/saviors.json", headers=headers, timeout=15).json()
        except Exception as e:
            print(f"⚠️ [구원자 목록 로드 실패] {e}")

        journey_buffs_map = {b['id']: b for b in (journey_buffs or [])}
        potentials_map = {p['id']: p for p in (potentials or [])}
        items_map = {i['id']: i for i in (items or []) if isinstance(i, dict) and 'id' in i}
        hidden_difficulties = set(load_settings().get("hidden_difficulties", []))
        result = build_caches_from_all_json(arcanas, journeys, journey_buffs_map, potentials_map, items_map, hidden_difficulties)
        result['saviors'] = saviors if isinstance(saviors, list) else []
        result['error'] = None
        self.finished_signal.emit(result)

class ArcanaCardIndexThread(QThread):
    finished_signal = pyqtSignal(list)

    def __init__(self, cards=None, card_names=None):
        super().__init__()
        self.cards = cards or []

    def run(self):
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36'}
        card_cache = []

        def fetch_one(card):
            name = card['name']
            try:
                img_path = card.get('img') or card_name_to_img_path(name)
                encoded_path = urllib.parse.quote(img_path, safe='/:')
                img_url = f"https://star-savior-arcana-db.pages.dev{encoded_path}"
                img_resp = requests.get(img_url, headers=headers, timeout=5)
                if img_resp.status_code != 200:
                    return None

                pil_raw = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                cv_bgr = cv2.cvtColor(np.array(pil_raw), cv2.COLOR_RGB2BGR)

                p_hash, hsv_hist, _ = extract_card_features(cv_bgr, apply_deskew=False)
                return {'hash': p_hash, 'hist': hsv_hist, 'id': name, 'name': name}
            except Exception as e:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            for result in executor.map(fetch_one, self.cards):
                if result: card_cache.append(result)

        print(f"🃏 [아르카나 정밀 인덱싱 완료] 총 {len(card_cache)}/{len(self.cards)}개 카드 특징 등록 완료!")
        self.finished_signal.emit(card_cache)


# ----------------------------------------------------
# 6. 메인 매니저
# ----------------------------------------------------
class MouseHoverManager(QWidget):
    def __init__(self):
        super().__init__()
        self._is_overlay_hidden = False
        desktop = QApplication.desktop()
        self.screen_w = desktop.screenGeometry(0).width()
        self.screen_h = desktop.screenGeometry(0).height()

        _saved_region_settings = load_settings()
        # ⚡ [최적화/CPU 절약 모드 플래그]
        self.is_low_spec_mode = _saved_region_settings.get("low_spec_mode", False)

        _gr = get_game_window_rect()
        if _gr:
            print(f"🪟 [게임 창 감지 성공] 위치=({_gr['left']},{_gr['top']}) 크기={_gr['width']}x{_gr['height']}")

        self.current_event_title = ""
        self.current_event_options = []
        self.matched_db_title = ""
        
        self.db_event_cache = []
        self.card_hash_cache = []
        self.known_card_names = set()
        self.card_id_map = {}
        self._pending_detail_fetch = None
        self._incremental_indexer = None
        
        self.is_in_journey = False
        self.page_loaded = False
        self.is_searching = False
        
        self.failed_searches = set()
        self.failed_card_searches = set()
        self.last_search_completed_for = ""
        self.current_matched_card_id = None

        self.event_confirmed = False
        self.event_confirmed_strong = False
        self.resolved_card_events = {}
        self.card_event_history = {}
        self.ambiguous_arcana_titles = set()
        self.title_to_cards = {}

        self.pending_card_search = None
        self._search_verify_retried = False
        self.pending_title_search = None
        self._dedupe_retry_done = False
        self._card_symbol_retry_done = False

        self.app_started_at = time.time()

        global _card_region_manual_override
        _region_override = _saved_region_settings.get("card_region_override")
        if _region_override:
            REGION_FRACTIONS["card_art"] = _region_override
            _card_region_manual_override = True

        self.status_overlay = JourneyStatusOverlay()
        self.status_overlay.on_settings_requested = self.open_settings_dialog
        self.status_overlay.on_reset_requested = self.reset_tracking_state

        self._startup_state = _saved_region_settings.get("startup_state", "expanded")
        self._pending_birthday_msg = None
        self._today_saviors_cache = None
        if self._startup_state == "collapsed":
            self.status_overlay._is_collapsed = True
            self.status_overlay._render()

        if not self._is_paused():
            self._show_startup_greeting(_saved_region_settings)

        self.tooltip = FixedLeftTooltip()

        self.tooltip_font_scale = _saved_region_settings.get("tooltip_font_scale", 1.0)
        self.tooltip.set_font_scale(self.tooltip_font_scale)

        self.status_overlay.repaint()

        self._region_debug_timer = QTimer()
        self._region_debug_timer.timeout.connect(self.status_overlay.reposition)
        self._region_debug_timer.timeout.connect(self.tooltip.reposition)
        self._region_debug_timer.timeout.connect(self._refresh_debug_overlays)
        self._region_debug_timer.start(500)

        self.active_choice_zones = []
        self.click_block_until = 0
        self._event_matched_timestamp = 0.0

        self.json_loader = JsonDataLoadThread()
        self.json_loader.finished_signal.connect(self.on_json_data_loaded)
        self.json_loader.start()

        self.status_overlay.set_custom_status("🟡 [DB 로딩 중] 카드/이벤트 데이터베이스를 받아오는 중...", "#ffcc00", "#ffcc00")
        self.status_overlay.repaint()

        self.event_ocr = EventTitleOCRThread(
            self.screen_w, self.screen_h,
            pause_getter=lambda: self.is_searching or getattr(self.choice_scanner, 'is_capturing', False) or self._is_paused(),
            low_spec_getter=lambda: self.is_low_spec_mode,
            gpu_ocr_getter=lambda: load_settings().get("try_gpu_ocr", True)
        )
        self.event_ocr.event_detected.connect(self.update_event_title)
        self.event_ocr.journey_status_changed.connect(self.update_journey_status)
        self.event_ocr.model_loading_started.connect(self._on_model_loading_started)

        self.is_arcana_event_screen = True
        self.event_ocr.arcana_label_detected.connect(self._on_arcana_label_changed)

        self.choice_scanner = ChoiceScannerThread(
            self.screen_w,
            pause_getter=lambda: self.is_searching or self._is_paused(),
            options_getter=lambda: self.current_event_options,
            low_spec_getter=lambda: self.is_low_spec_mode,
            gpu_ocr_getter=lambda: load_settings().get("try_gpu_ocr", True)
        )
        self.choice_scanner.choices_scanned.connect(self.on_choices_scanned)

        self.card_art_ocr = EventCardArtThread(
            self.screen_w, self.screen_h,
            lambda: self._card_search_pool(),
            pause_getter=lambda: self.is_searching or not self.is_in_journey or not self.is_arcana_event_screen or self._is_paused(),
            lenient_getter=lambda: self._is_force_confirm_active(),
            low_spec_getter=lambda: self.is_low_spec_mode
        )
        self.card_art_ocr.start()
        self.card_art_ocr.card_matched.connect(self.on_card_art_matched)

        self.card_preview_overlay = None
        self.region_debug_overlay = None
        self.card_art_ocr.raw_preview_signal.connect(self._on_card_raw_preview)
        self.card_art_ocr.preview_signal.connect(self._on_card_warped_preview)
        if _saved_region_settings.get("show_card_preview"):
            self.set_card_preview_enabled(True)
        if _saved_region_settings.get("show_region_debug"):
            self.set_region_debug_enabled(True)

        # ⚡ [최적화 모드 속도 반영] 일반 30ms / 최적화 80ms (아래 set_low_spec_mode 호출에서 최종 반영)
        self._POLL_FAST_HOVER_MS = 80 if self.is_low_spec_mode else 30
        self._POLL_SLOW_HOVER_MS = 1000
        self._POLL_FAST_LOADING_MS = 250 if self.is_low_spec_mode else 150
        self._POLL_SLOW_LOADING_MS = 1000
        self._poll_speed_is_fast = True
        self._game_was_active_prev = False

        self.instant_hover_timer = QTimer()
        self.instant_hover_timer.timeout.connect(self.check_instant_hover)
        self.instant_hover_timer.start(self._POLL_FAST_HOVER_MS)

        self._is_loading_global = False
        self._loading_pending_count = 0
        self._loading_end_pending_count = 0
        self._loading_sct = mss.mss()
        self._global_loading_timer = QTimer()
        self._global_loading_timer.timeout.connect(self._poll_global_loading_state)
        self._global_loading_timer.start(self._POLL_FAST_LOADING_MS)

        # ⚡ [최적화 모드] 저장된 설정값을 cv2/torch 스레드 수·스레드 우선순위에도 반영
        self.set_low_spec_mode(self.is_low_spec_mode)

        self._transition_sct = mss.mss()
        self._transition_baseline_gray = None
        self._transition_pending_confirm = 0
        self._transition_check_timer = QTimer()
        self._transition_check_timer.timeout.connect(self._poll_click_transition)

        self.tray_icon = QSystemTrayIcon(_create_tray_icon(), self)
        self.tray_icon.setToolTip("JourneyHelper 실행 중 (우클릭하여 종료)")
        tray_menu = QMenu()
        self.show_overlay_action = QAction("상단바 표시하기", tray_menu)
        self.show_overlay_action.triggered.connect(self.show_overlay)
        self.show_overlay_action.setEnabled(False)
        tray_menu.addAction(self.show_overlay_action)
        quit_action = QAction("종료", tray_menu)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        if self._startup_state == "hidden":
            self.hide_overlay()

    def set_low_spec_mode(self, enabled):
        self.is_low_spec_mode = enabled
        self._POLL_FAST_HOVER_MS = 80 if enabled else 30
        self._POLL_FAST_LOADING_MS = 250 if enabled else 150
        if self._poll_speed_is_fast:
            self.instant_hover_timer.setInterval(self._POLL_FAST_HOVER_MS)
            self._global_loading_timer.setInterval(self._POLL_FAST_LOADING_MS)

        # ⚡ [최적화 모드] cv2/torch 스레드 수를 함께 조절 (1코어 제한 대신 라이브러리 레벨로 제한)
        # torch는 EasyOCR 인식(CNN 추론)에도 그대로 쓰이므로 1스레드까지 낮추면
        # 선택지 텍스트 정밀 인식(readtext)이 크게 느려질 수 있어 최소 2스레드는 유지한다.
        _half_cores = max(1, (os.cpu_count() or 4) // 2)
        cv2.setNumThreads(1 if enabled else _half_cores)
        torch.set_num_threads(max(2, _half_cores // 2) if enabled else _half_cores)

        # ⚡ [최적화 모드] 백그라운드 OCR/카드 인식 스레드 우선순위를 낮춰
        # 게임(포그라운드 프로세스)에 CPU를 먼저 배분하도록 함
        _priority = QThread.LowPriority if enabled else QThread.InheritPriority
        for _t in (getattr(self, 'card_art_ocr', None), getattr(self, 'choice_scanner', None), getattr(self, 'event_ocr', None)):
            if _t is not None:
                try:
                    _t.setPriority(_priority)
                except Exception:
                    pass

        print(f"⚡ [성능 옵션 변경] 최적화/CPU 절약 모드 = {enabled} (cv2/torch 스레드={1 if enabled else _half_cores}, 스레드 우선순위={'낮음' if enabled else '기본'})")

    def hide_overlay(self):
        if self._is_overlay_hidden:
            return
        self._is_overlay_hidden = True
        print("🙈 [오버레이 숨김] 설정에서 요청됨")
        self.tooltip.hide_and_clear()
        self.status_overlay.hide()
        self.show_overlay_action.setEnabled(True)
        self.tray_icon.setToolTip("JourneyHelper (상단바 숨김 상태 - 우클릭하여 표시)")

    def show_overlay(self):
        if not self._is_overlay_hidden:
            return
        self._is_overlay_hidden = False
        print("👁️ [오버레이 표시] 트레이 메뉴에서 복귀 요청됨")
        self.status_overlay.show()
        self.status_overlay._render()
        self.show_overlay_action.setEnabled(False)
        self.tray_icon.setToolTip("JourneyHelper 실행 중 (우클릭하여 종료)")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.tray_icon.contextMenu().popup(QCursor.pos())

    def quit_app(self):
        print("🛑 트레이 메뉴에서 종료 요청 → 프로그램을 안전하게 종료합니다.")
        self.tray_icon.hide()
        QApplication.quit()

    def open_settings_dialog(self):
        dlg = SettingsDialog(
            on_quit=self.quit_app,
            on_toggle_card_preview=self.set_card_preview_enabled,
            on_toggle_region_debug=self.set_region_debug_enabled,
            on_open_region_adjuster=self.open_card_region_adjuster,
            on_font_scale_change=self.set_tooltip_font_scale,
            on_hide_overlay=self.hide_overlay,
            on_reset_position=self.status_overlay.reset_position,
            on_toggle_position_lock=self.status_overlay.set_position_locked,
            on_toggle_capture_debug=self.set_capture_debug_enabled,
            on_toggle_low_spec=self.set_low_spec_mode,
        )
        dlg.exec_()

    def set_capture_debug_enabled(self, visible_in_capture):
        self.status_overlay.set_capture_visible_for_debug(visible_in_capture)
        self.tooltip.set_capture_visible_for_debug(visible_in_capture)

    def set_tooltip_font_scale(self, scale):
        self.tooltip_font_scale = scale
        self.tooltip.set_font_scale(scale)

    def open_card_region_adjuster(self):
        self._region_adjust_overlay = CardRegionAdjustOverlay(
            on_save=self._on_card_region_adjusted,
            on_cancel=lambda: None,
        )

    def _on_card_region_adjusted(self, frac):
        global _card_region_manual_override
        settings = load_settings()
        if frac is None:
            settings["card_region_override"] = dict(_DEFAULT_CARD_ART_FRACTION)
            save_settings(settings)
            REGION_FRACTIONS["card_art"] = dict(_DEFAULT_CARD_ART_FRACTION)
            _card_region_manual_override = True
            print("↩️ [카드 영역] 기본값으로 되돌리고 고정")
        else:
            settings["card_region_override"] = frac
            save_settings(settings)
            REGION_FRACTIONS["card_art"] = frac
            _card_region_manual_override = True
            print(f"✅ [카드 영역] 사용자 지정 영역 저장됨: {frac}")

    def set_card_preview_enabled(self, enabled):
        if enabled:
            if self.card_preview_overlay is None:
                self.card_preview_overlay = CardPreviewOverlay()
            else:
                self.card_preview_overlay.show()
        elif self.card_preview_overlay is not None:
            self.card_preview_overlay.hide()

    def set_region_debug_enabled(self, enabled):
        if enabled:
            if self.region_debug_overlay is None:
                self.region_debug_overlay = RegionDebugOverlay(self.screen_w, self.screen_h)
            else:
                self.region_debug_overlay.show()
            self.region_debug_overlay.refresh()
        elif self.region_debug_overlay is not None:
            self.region_debug_overlay.hide()

    def _refresh_debug_overlays(self):
        if self.card_preview_overlay is not None and self.card_preview_overlay.isVisible():
            self.card_preview_overlay.reposition()
        if self.region_debug_overlay is not None and self.region_debug_overlay.isVisible():
            self.region_debug_overlay.refresh()

    def _on_card_raw_preview(self, cv_bgr_img):
        if self.card_preview_overlay is not None and self.card_preview_overlay.isVisible():
            self.card_preview_overlay.update_raw(cv_bgr_img)

    def _on_card_warped_preview(self, cv_bgr_img):
        if self.card_preview_overlay is not None and self.card_preview_overlay.isVisible():
            self.card_preview_overlay.update_image(cv_bgr_img)

    def reset_tracking_state(self, greeting_mode="none"):
        print("🔄 [수동 새로고침] 상단 배지 클릭 → 추적 중이던 이벤트/카드 정보 초기화")

        # 🎯 [영역 자동 보정 초기화] 접었다 펼 때(수동 클릭 토글, 게임 감지 시 자동 펼침 둘 다 여기로 옴)
        # 그동안 자동 보정으로 틀어져 있던 제목/라벨 캡처 영역을 기본값으로 되돌리고, 자동 보정을
        # 처음부터 다시 시도하게 한다. 자동 보정이 한 번 잘못된 위치를 학습해버리면 이후 계속 그
        # 틀린 위치를 기준으로 삼아서 스스로는 못 벗어나는 경우가 있는데, 유저가 수동으로 리셋할
        # 수 있는 탈출구를 만들어주는 목적.
        global _auto_calibration_done, _auto_calibration_attempts, _last_calibrated_rect, _calibration_samples
        if _auto_calibration_done or _calibration_samples:
            print("🎯 [영역 보정 초기화] 제목/라벨 캡처 영역을 기본값으로 되돌리고 자동 재보정을 다시 시작합니다.")
        _auto_calibration_done = False
        _auto_calibration_attempts = 0
        _last_calibrated_rect = None
        _calibration_samples = []
        REGION_FRACTIONS["event_title"] = dict(_DEFAULT_EVENT_TITLE_FRACTION)
        REGION_FRACTIONS["arcana_label"] = dict(_DEFAULT_ARCANA_LABEL_FRACTION)
        # card_art는 별도의 "카드 인식 영역 직접 조정" 수동 설정(_card_region_manual_override)이
        # 있는 영역이라 이 초기화 대상에서 제외한다 — 유저가 직접 맞춰둔 값을 건드리지 않기 위함.

        self.active_choice_zones = []
        self.current_event_options = []
        self.current_event_title = ""
        self.matched_db_title = ""
        self.event_confirmed = False
        self.event_confirmed_strong = False
        self.current_matched_card_id = None
        self._card_symbol_retry_done = False
        if hasattr(self, 'card_art_ocr'):
            self.card_art_ocr.last_matched_id = ""
            self.card_art_ocr.pending_id = None
            self.card_art_ocr.pending_count = 0
            self.card_art_ocr._last_card_gray = None
        self.tooltip.hide_and_clear()
        self.click_block_until = 0
        if hasattr(self, 'choice_scanner'):
            self.choice_scanner.reset_cache()
        if hasattr(self, 'event_ocr'):
            self.event_ocr.is_waiting_for_next_event = False
            self.event_ocr.completed_event_title = ""
            self.event_ocr.pending_event = ""
            self.event_ocr.last_event = ""
            self.event_ocr._last_title_gray = None
        if hasattr(self, '_lock_verify_timer'):
            self._lock_verify_timer.stop()
        self.status_overlay.set_custom_status("🔄 [새로고침됨] 여정 이벤트 화면을 다시 찾고 있습니다...", "#888888", "#aaaaaa")
        if greeting_mode == "full" and not self._is_paused():
            self._show_startup_greeting()
            self._show_birthday_on_auto_expand()

    def _poll_global_loading_state(self):
        if self._is_paused():
            return
        if not is_game_hovered_or_active():
            return
        game_rect = get_game_window_rect()
        loading_now = is_loading_screen_visible(game_rect, self.screen_w, self.screen_h, sct=self._loading_sct)

        if not loading_now:
            self._loading_pending_count = 0
        elif not self._is_loading_global:
            self._loading_pending_count += 1
            if self._loading_pending_count < 2:
                return

        if loading_now:
            self._loading_end_pending_count = 0
        elif self._is_loading_global:
            self._loading_end_pending_count += 1
            if self._loading_end_pending_count < 2:
                return

        if loading_now and not self._is_loading_global:
            self._is_loading_global = True
            print("🌕 [전역 로딩 화면 감지] 이전 이벤트 상태 전체 초기화")
            self.active_choice_zones = []
            self.current_event_options = []
            self.current_event_title = ""
            self.matched_db_title = ""
            self.event_confirmed = False
            self.event_confirmed_strong = False
            self.current_matched_card_id = None
            self._card_symbol_retry_done = False
            if hasattr(self, 'card_art_ocr'):
                self.card_art_ocr.last_matched_id = ""
                self.card_art_ocr.pending_id = None
                self.card_art_ocr.pending_count = 0
                self.card_art_ocr._last_card_gray = None
            self.tooltip.hide_and_clear()
            self.click_block_until = 0
            if hasattr(self, 'choice_scanner'):
                self.choice_scanner.reset_cache()
            if hasattr(self, '_lock_verify_timer'):
                self._lock_verify_timer.stop()
            self.status_overlay.set_custom_status("🌕 [로딩 중] 다음 화면으로 이동하는 중...", "#ffcc00", "#ffcc00")

        elif not loading_now and self._is_loading_global:
            self._is_loading_global = False
            print("✅ [전역 로딩 화면 종료 감지] 다음 이벤트 탐색 대기 상태로 전환")
            if hasattr(self, 'event_ocr'):
                self.event_ocr.is_waiting_for_next_event = False
                self.event_ocr.completed_event_title = ""
                self.event_ocr.pending_event = ""
                self.event_ocr.last_event = ""
                self.event_ocr._last_title_gray = None
            self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")

    def register_instant_estimated_zones(self):
        if not self.current_event_options or len(self.current_event_options) < 2:
            self.active_choice_zones = []
            return

        game_rect = get_game_window_rect()
        if game_rect:
            base_top, base_h = game_rect["top"], game_rect["height"]
        else:
            base_top, base_h = 0, 1080

        count = len(self.current_event_options)
        bottom_anchor_y = base_top + int(base_h * 0.724)
        pitch_px = int(base_h * 0.07037)
        btn_height_px = int(base_h * 0.063)

        estimated_zones = []
        for i in range(count):
            cy = bottom_anchor_y - (count - 1 - i) * pitch_px
            opt = self.current_event_options[i]
            estimated_zones.append({
                'y_min': cy - (btn_height_px // 2),
                'y_max': cy + (btn_height_px // 2),
                'option': opt,
                'text': opt.get('choice', ''),
            })

        self.active_choice_zones = estimated_zones
        if self._event_matched_timestamp > 0:
            elapsed_ms = (time.time() - self._event_matched_timestamp) * 1000
            print(f"📐 [1차 비율 추정 Y좌표 등록] 총 {len(estimated_zones)}개 선택지 (소요시간: {elapsed_ms:.1f}ms)")

    def on_choices_scanned(self, zones):
        return  # 👈 2차 정밀 보정 완전 무시 (1차 정적 좌표만 유지)

    def on_choice_selected_action(self, option=None):
        self._transition_baseline_gray = self._capture_choice_panel_gray(get_game_window_rect())
        self._transition_pending_confirm = 0
        self._transition_check_timer.start(120)

        has_condition = bool(option.get('has_condition', True)) if option else True
        self._last_choice_has_condition = has_condition

        if not has_condition:
            print("🟢 [조건 없는 선택지 클릭] 항상 성공하는 선택지로 판단 → 툴팁 즉시 숨김")
            self.tooltip.hide_and_clear()
            self.active_choice_zones = []
            self.current_event_options = []
            self._transition_check_timer.stop()

        if hasattr(self, 'event_ocr'):
            self.event_ocr.completed_event_title = self.current_event_title or getattr(self.event_ocr, 'last_event', '')
            self.event_ocr.is_waiting_for_next_event = True
            self.event_ocr.last_event = ""
            self.event_ocr.pending_event = ""
            self.event_ocr._last_title_gray = None

        self.click_block_until = time.time() + 2.5  
        self.current_event_title = ""
        self.matched_db_title = ""
        self.event_confirmed = False
        self.event_confirmed_strong = False
        self.current_matched_card_id = None
        self._card_symbol_retry_done = False
        if hasattr(self, 'card_art_ocr'):
            self.card_art_ocr.last_matched_id = ""
            self.card_art_ocr.pending_id = None
            self.card_art_ocr.pending_count = 0
            self.card_art_ocr._last_card_gray = None

        self.status_overlay.set_custom_status("🔵 [선택 반응 확인 중] 화면 전환 여부 확인 중...", "#00aaff", "#00aaff")

        if hasattr(self, 'choice_scanner'):
            self.choice_scanner.reset_cache()

        print(f"🖱️ [선택지 선택 감지] 완료 이벤트 '{getattr(self.event_ocr, 'completed_event_title', '')}' 락 등록")

        self._lock_check_started_at = time.time()
        if not hasattr(self, '_lock_verify_timer'):
            self._lock_verify_timer = QTimer()
            self._lock_verify_timer.timeout.connect(self._verify_choice_actually_advanced)
        self._lock_verify_timer.start(1000)

    LOCK_RECOVERY_MAX_WAIT_SEC = 8.0

    def _verify_choice_actually_advanced(self):
        if not hasattr(self, 'event_ocr') or not self.event_ocr.is_waiting_for_next_event:
            if hasattr(self, '_lock_verify_timer'):
                self._lock_verify_timer.stop()
            return

        elapsed = time.time() - getattr(self, '_lock_check_started_at', time.time())
        if elapsed >= self.LOCK_RECOVERY_MAX_WAIT_SEC:
            print(f"🔓 [잠긴 선택지 자동 복구] {elapsed:.1f}초간 로딩 화면이 감지되지 않음 → 선택 무효로 판단, 락 해제")
            self.event_ocr.is_waiting_for_next_event = False
            self.event_ocr.completed_event_title = ""
            self.event_ocr.pending_event = ""
            self.event_ocr.last_event = ""
            self.event_ocr._last_title_gray = None
            self.click_block_until = 0
            self._lock_verify_timer.stop()
            self.active_choice_zones = []
            self.current_event_options = []
            self.current_event_title = ""
            self.matched_db_title = ""
            self.event_confirmed = False
            self.event_confirmed_strong = False
            self.current_matched_card_id = None
            self._card_symbol_retry_done = False
            if hasattr(self, 'card_art_ocr'):
                self.card_art_ocr.last_matched_id = ""
                self.card_art_ocr.pending_id = None
                self.card_art_ocr.pending_count = 0
                self.card_art_ocr._last_card_gray = None
            self.tooltip.hide_and_clear()
            if hasattr(self, 'choice_scanner'):
                self.choice_scanner.reset_cache()
            self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")

    def _capture_choice_panel_gray(self, game_rect):
        try:
            if not self.active_choice_zones:
                return None
            y_min = min(z['y_min'] for z in self.active_choice_zones)
            y_max = max(z['y_max'] for z in self.active_choice_zones)
            base_left, base_w = (game_rect["left"], game_rect["width"]) if game_rect else (0, self.screen_w)
            x_min = base_left + int(base_w * 0.64)
            x_max = base_left + int(base_w * 0.95)
            monitor = {
                "top": int(y_min), "left": int(x_min),
                "width": max(1, int(x_max - x_min)), "height": max(1, int(y_max - y_min)),
            }
            img = np.array(self._transition_sct.grab(monitor))
            gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
            return cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        except Exception:
            return None

    def _poll_click_transition(self):
        if not hasattr(self, 'event_ocr') or not self.event_ocr.is_waiting_for_next_event:
            self._transition_check_timer.stop()
            return

        if self._transition_baseline_gray is None:
            self._transition_check_timer.stop()
            return

        current_gray = self._capture_choice_panel_gray(get_game_window_rect())
        if current_gray is None or current_gray.shape != self._transition_baseline_gray.shape:
            self._transition_pending_confirm = 0
            return

        diff = float(np.mean(np.abs(current_gray.astype(np.int16) - self._transition_baseline_gray.astype(np.int16))))

        if diff <= 18:
            self._transition_pending_confirm = 0
            return

        self._transition_pending_confirm += 1
        if self._transition_pending_confirm < 2:
            return

        print(f"🖼️ [화면 전환 시각 감지] 선택지 영역 변화량 {diff:.1f} → 선택 확정으로 판단, 즉시 정리")
        self._transition_check_timer.stop()
        self.click_block_until = 0

        self.active_choice_zones = []
        self.current_event_options = []
        self.current_event_title = ""
        self.matched_db_title = ""
        self.event_confirmed = False
        self.event_confirmed_strong = False
        self.current_matched_card_id = None
        self._card_symbol_retry_done = False
        if hasattr(self, 'card_art_ocr'):
            self.card_art_ocr.last_matched_id = ""
            self.card_art_ocr.pending_id = None
            self.card_art_ocr.pending_count = 0
            self.card_art_ocr._last_card_gray = None
        self.tooltip.hide_and_clear()
        if hasattr(self, 'choice_scanner'):
            self.choice_scanner.reset_cache()
        self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")

    def get_choice_zone_at(self, pos, game_rect):
        if not self.active_choice_zones:
            return None

        base_left, base_w = (game_rect["left"], game_rect["width"]) if game_rect else (0, self.screen_w)
        x_min = base_left + int(base_w * 0.64)
        x_max = base_left + int(base_w * 0.95)

        if not (x_min <= pos.x() <= x_max):
            return None

        for zone in self.active_choice_zones:
            if zone['y_min'] <= pos.y() <= zone['y_max']:
                return zone
        return None

    def is_cursor_inside_choice_zone(self, pos, game_rect):
        return self.get_choice_zone_at(pos, game_rect) is not None

    def _set_poll_speed(self, fast):
        self._poll_speed_is_fast = fast
        if fast:
            self.instant_hover_timer.setInterval(self._POLL_FAST_HOVER_MS)
            self._global_loading_timer.setInterval(self._POLL_FAST_LOADING_MS)
        else:
            self.instant_hover_timer.setInterval(self._POLL_SLOW_HOVER_MS)
            self._global_loading_timer.setInterval(self._POLL_SLOW_LOADING_MS)

    def _is_paused(self):
        return self.status_overlay.is_collapsed() or self._is_overlay_hidden

    def _show_priority_status(self, show_func):
        remaining = getattr(self.status_overlay, '_status_protect_until', 0) - time.time()
        if remaining > 0:
            QTimer.singleShot(int(remaining * 1000) + 100, lambda: self._show_priority_status(show_func))
        else:
            show_func()

    def _show_startup_greeting(self, settings=None):
        if settings is None:
            settings = load_settings()
        today = datetime.date.today()
        my_birthday_month = settings.get("user_birthday_month", 0)
        my_birthday_day = settings.get("user_birthday_day", 1)

        if my_birthday_month == today.month and my_birthday_day == today.day:
            custom_bday_msgs = settings.get("custom_birthday_messages", [])
            if custom_bday_msgs:
                greeting_text = f"🎉 {random.choice(custom_bday_msgs)}"
            else:
                greeting_text = random.choice(MY_BIRTHDAY_TEMPLATES)
            self._show_priority_status(
                lambda t=greeting_text: self.status_overlay.set_custom_status_animated(t, "#ff8fc7", "#ff8fc7", protect_seconds=8)
            )
        elif settings.get("greeting_enabled", True):
            custom_greetings = settings.get("custom_greetings", [])
            if custom_greetings:
                greeting_text = f"👋 {random.choice(custom_greetings)}"
            else:
                greeting_text = random.choice(GREETING_TEMPLATES)
            self._show_priority_status(
                lambda t=greeting_text: self.status_overlay.set_custom_status_animated(t, "#00e5ff", "#00e5ff", protect_seconds=7)
            )

    def _check_auto_expand_on_game_launch(self, game_just_launched):
        if not game_just_launched:
            return
        settings = load_settings()
        if not settings.get("auto_expand_on_launch", False):
            return

        if self._is_overlay_hidden:
            if not settings.get("auto_expand_include_hidden", False):
                return
            print("🚀 [자동 펼침] 게임 감지됨 → 숨김 상태 자동 해제")
            self.show_overlay()
            self._show_startup_greeting(settings)
            self._show_birthday_on_auto_expand()

        if self.status_overlay.is_collapsed():
            print("🚀 [자동 펼침] 게임 감지됨 → 접힘 상태 자동 해제")
            self.status_overlay._is_collapsed = False
            self.status_overlay._render()
            self.reset_tracking_state(greeting_mode="full")

    def check_instant_hover(self):
        game_running_now = get_game_window_rect() is not None
        game_just_launched = game_running_now and not self._game_was_active_prev
        self._game_was_active_prev = game_running_now

        if self._is_paused():
            self.tooltip.hide_and_clear()
            if self._poll_speed_is_fast:
                self._set_poll_speed(fast=False)
            self._check_auto_expand_on_game_launch(game_just_launched)
            return

        game_now = is_game_hovered_or_active()
        if game_now != self._poll_speed_is_fast:
            self._set_poll_speed(fast=game_now)

        user32 = ctypes.windll.user32

        if game_now and time.time() >= self.click_block_until:
            game_rect = get_game_window_rect()
            pos = QCursor.pos()

            if (user32.GetAsyncKeyState(0x01) & 0x8000) != 0:
                clicked_zone = self.get_choice_zone_at(pos, game_rect)
                if clicked_zone:
                    self.on_choice_selected_action(clicked_zone.get('option'))
                    return

            num_keys = [(0x31, 1), (0x32, 2), (0x33, 3), (0x34, 4),
                        (0x61, 1), (0x62, 2), (0x63, 3), (0x64, 4)]
            
            for vk, choice_num in num_keys:
                if (user32.GetAsyncKeyState(vk) & 0x8000) != 0:
                    if len(self.active_choice_zones) >= choice_num:
                        chosen_option = self.active_choice_zones[choice_num - 1].get('option')
                        self.on_choice_selected_action(chosen_option)
                        return

        if not self.is_in_journey or not game_now:
            self.tooltip.hide_and_clear()
            return

        if getattr(self, '_is_loading_global', False):
            self.tooltip.hide_and_clear()
            return

        if not self.active_choice_zones:
            self.tooltip.hide_and_clear()
            return

        pos = QCursor.pos()
        game_rect = get_game_window_rect()
        if game_rect:
            base_left, base_w = game_rect["left"], game_rect["width"]
        else:
            base_left, base_w = 0, self.screen_w

        x_min = base_left + int(base_w * 0.64)
        x_max = base_left + int(base_w * 0.95)

        if not (x_min <= pos.x() <= x_max):
            self.tooltip.hide_and_clear()
            return

        hovered_zone = None
        for zone in self.active_choice_zones:
            if zone['y_min'] <= pos.y() <= zone['y_max']:
                hovered_zone = zone
                break

        if hovered_zone:
            if getattr(self, '_last_logged_hover_text', '') != hovered_zone['text']:
                self._last_logged_hover_text = hovered_zone['text']
                print(f"🎯 [선택지 상자 호버 감지] 마우스 Y: {pos.y()}px ➔ [{hovered_zone['text']}]")

            opt = hovered_zone['option']
            choice_name = re.sub(r'[\.\s]+$', '', opt.get('choice', ''))
            raw_reward = opt.get('reward', '')
            formatted_text, is_wide, min_width = self.smart_format_reward(raw_reward, choice_name)
            self.tooltip.show_info(formatted_text, wide=is_wide, min_width=min_width)
        else:
            self._last_logged_hover_text = ""
            self.tooltip.hide_and_clear()

    def _on_arcana_label_changed(self, is_arcana):
        self.is_arcana_event_screen = is_arcana

    def on_json_data_loaded(self, result):
        if result.get('error'):
            print(f"⚠️ [JSON 로드 오류] {result.get('error')}")
            return

        if not hasattr(self, 'choice_scanner') or not hasattr(self, 'event_ocr'):
            QTimer.singleShot(50, lambda: self.on_json_data_loaded(result))
            return

        self.db_event_cache = result['db_event_cache']
        self.card_event_history = result['card_event_history']
        self.resolved_card_events = result['resolved_card_events']
        self.card_id_map = result['card_id_map']
        self.ambiguous_arcana_titles = result.get('ambiguous_arcana_titles', set())
        self.title_to_cards = result.get('title_to_cards', {})
        for c in result['cards']:
            self.known_card_names.add(c['name'])

        self.page_loaded = True
        self.is_in_journey = False

        self.arcana_indexer = ArcanaCardIndexThread(cards=result['cards'])
        self.arcana_indexer.finished_signal.connect(self.on_arcana_indexed)
        self.arcana_indexer.start()

        if not self.event_ocr.isRunning(): self.event_ocr.start()
        # if not self.choice_scanner.isRunning(): self.choice_scanner.start() 쓰지 않으니 주석처리함

        done_msg = f"🟢 [통합 DB 완료] 총 {len(self.db_event_cache)}개 이벤트 로딩 완료"
        self.status_overlay.set_custom_status(done_msg, "#00e5ff", "#00e5ff")

        self._today_saviors_cache = result.get('saviors', [])
        self._show_today_birthdays(self._today_saviors_cache)

    def _show_today_birthdays(self, saviors, revert_to_msg=None):
        if not load_settings().get("birthday_notify_enabled", True):
            return
        try:
            today = datetime.date.today()
            names = []
            for s in (saviors or []):
                if not isinstance(s, dict):
                    continue
                birth = ((s.get('inst') or {}).get('birth')) or {}
                if birth.get('month') == today.month and birth.get('day') == today.day:
                    name = ((s.get('name') or {}).get('ko-KR') or '').strip()
                    if name:
                        names.append(name)

            if names:
                names = sorted(set(names))
                joined = ", ".join(names)
                msg = f"🎂 오늘 {today.month}월 {today.day}일은 {joined}의 생일입니다!"
                color = "#ff8fc7"
                print(f"🎂 [오늘의 생일] {joined}")
            else:
                if not load_settings().get("announce_no_birthday", True):
                    return
                no_birthday_templates = [
                    "🎂 오늘({m}월 {d}일)은 생일인 구원자가 없네요~ 내일은 있을까요?",
                    "🎂 오늘({m}월 {d}일)은 딱히 생일인 구원자가 없어요. 조용한 하루네요!",
                    "🎂 오늘 생일인 구원자는 없지만, 다들 무탈하게 잘 지내고 있어요~",
                    "🎂 아쉽게도 오늘({m}월 {d}일)은 생일 파티가 없는 날이네요.",
                    "🎂 오늘({m}월 {d}일) 생일인 구원자는 없어요. 대신 내일을 기대해봐요!",
                ]
                msg = random.choice(no_birthday_templates).format(m=today.month, d=today.day)
                color = "#888888"

            if self._is_paused():
                self._pending_birthday_msg = msg
                self._pending_birthday_color = color
                print("🎂 [생일 알림 보류] 상단바가 접힘/숨김 상태라 펼쳐질 때 표시 예정")
                return

            self._show_priority_status(
                lambda: self.status_overlay.set_custom_status_animated(msg, color, color, protect_seconds=7)
            )
        except Exception as e:
            print(f"⚠️ [생일 확인 실패] {e}")

    def _maybe_show_pending_birthday(self):
        msg = getattr(self, '_pending_birthday_msg', None)
        if not msg or self._is_paused():
            return
        color = getattr(self, '_pending_birthday_color', '#ff8fc7')
        self._pending_birthday_msg = None
        self._show_priority_status(
            lambda: self.status_overlay.set_custom_status_animated(msg, color, color, protect_seconds=7)
        )

    def _show_birthday_on_auto_expand(self):
        if getattr(self, '_today_saviors_cache', None) is not None:
            self._pending_birthday_msg = None
            self._show_today_birthdays(self._today_saviors_cache)
        else:
            self._maybe_show_pending_birthday()

    def on_arcana_indexed(self, hash_cache):
        self.card_hash_cache = [c for c in hash_cache if c.get('name') not in EXCLUDED_CARD_NAMES]

    def _discover_new_arcana_cards(self, event_list):
        if not event_list: return
        new_names = []
        for ev in event_list:
            if ev.get('isArcana') and ev.get('rawTitle'):
                name = ev['rawTitle'].strip()
                if name and name not in self.known_card_names:
                    new_names.append(name)
                    self.known_card_names.add(name)

        if not new_names: return
        print(f"🆕 [신규 카드 후보 발견] {new_names} → 자동 인덱싱 시도")
        if self._incremental_indexer is not None and self._incremental_indexer.isRunning():
            for n in new_names: self.known_card_names.discard(n)
            return
        self._incremental_indexer = ArcanaCardIndexThread(card_names=new_names)
        self._incremental_indexer.finished_signal.connect(self.on_incremental_indexed)
        self._incremental_indexer.start()

    def on_incremental_indexed(self, hash_cache):
        if hash_cache:
            hash_cache = [c for c in hash_cache if c.get('name') not in EXCLUDED_CARD_NAMES]
            self.card_hash_cache = self.card_hash_cache + hash_cache
            print(f"🆕 [신규 카드 자동 등록] {len(hash_cache)}개 카드가 인식 대상에 추가됨")
        self._incremental_indexer = None

    def _is_force_confirm_active(self):
        if not self.matched_db_title:
            return False
        clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
        return clean_title in self.ambiguous_arcana_titles

    def _card_search_pool(self):
        if self._is_force_confirm_active():
            clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
            allowed_names = self.title_to_cards.get(clean_title)
            if allowed_names:
                pool = [c for c in self.card_hash_cache if c.get('name') in allowed_names]
                if pool:
                    return pool
        return self.card_hash_cache

    def _debug_reward_snippet(self):
        try:
            if not self.current_event_options:
                return "(선택지 없음)"
            first = self.current_event_options[0]
            reward = (first.get('reward', '') or '').replace('\n', ' | ')
            return f"[{first.get('choice','')}] {reward[:60]}"
        except Exception:
            return "(요약 실패)"

    def on_card_art_matched(self, card_id, card_name):
        if hasattr(self, 'event_ocr') and self.event_ocr.is_waiting_for_next_event:
            return

        if self.event_confirmed_strong:
            return

        if card_name == self.current_matched_card_id:
            return
        self.current_matched_card_id = card_name

        best_guess = None
        if self.current_event_title:
            clean_ocr_ev = re.sub(r'[^가-힣0-9]', '', self.current_event_title)
            clean_ocr_ev = re.sub(r'여정|이벤트|아르카나|알카|노아|NOA', '', clean_ocr_ev)
            hist_best, hist_score = self._pick_closest_card_history(card_name, clean_ocr_ev)
            if hist_best and hist_score >= 0.35:
                best_guess = hist_best
            if best_guess is None:
                cand = self.resolved_card_events.get(card_name)
                if cand and self._score_event_match(clean_ocr_ev, cand.get('cleanTitle', '')) >= 0.35:
                    best_guess = cand
        else:
            known_events = self.card_event_history.get(card_name, [])
            if len(known_events) <= 1:
                best_guess = self.resolved_card_events.get(card_name)
            else:
                self.status_overlay.set_custom_status(f"🟣 [카드 인식] {card_name} (제목 확인 대기 — 여러 이벤트 보유 카드)", "#c084fc", "#c084fc")

        if best_guess:
            self.matched_db_title = best_guess.get("rawTitle", card_name)
            self.current_event_options = best_guess.get("options", [])
            self.status_overlay.set_custom_status(f"🟣 [카드 인식/추정] {self.matched_db_title} (문구 확인 대기)", "#c084fc", "#c084fc")
            print(f"⚡ [캐시 즉시 매칭-추정] 카드 '{card_name}' ➔ DB: '{self.matched_db_title}'")
            if self.current_event_title: self.match_current_event_from_cache()
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()

    def update_event_title(self, title):
        title = fix_ocr_typos(title)
        clean_new = re.sub(r'[^가-힣0-9a-zA-Z]', '', title)
        clean_curr = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.current_event_title)

        if clean_new and clean_new != clean_curr:
            if self.event_confirmed and self.matched_db_title and self.db_event_cache:
                quick_best, quick_score = self._find_best_event(clean_new, self.db_event_cache)
                clean_matched = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
                if quick_best and quick_score >= 0.5 and quick_best.get('cleanTitle', '') == clean_matched:
                    self.current_event_title = title
                    return

            had_previous_title = bool(clean_curr)

            self.current_event_title = title
            self.matched_db_title = title
            self.current_event_options = []
            self.event_confirmed = False
            self.event_confirmed_strong = False
            self._dedupe_retry_done = False
            if had_previous_title:
                self.current_matched_card_id = None
                self._card_symbol_retry_done = False
                if hasattr(self, 'card_art_ocr'):
                    self.card_art_ocr.last_matched_id = ""
                    self.card_art_ocr.pending_id = None
                    self.card_art_ocr.pending_count = 0
                    self.card_art_ocr._last_card_gray = None
            self.active_choice_zones = []
            self.tooltip.hide_and_clear()
            self.choice_scanner.reset_cache()

            self.status_overlay.set_custom_status(f"🔵 [이벤트 탐색 중] {title}...", "#00aaff", "#00aaff")
            self.match_current_event_from_cache()

    def _score_event_match(self, clean_query, clean_candidate):
        if not clean_candidate or not clean_query: return 0
        if clean_candidate in clean_query or clean_query in clean_candidate:
            return (min(len(clean_query), len(clean_candidate)) / max(len(clean_query), len(clean_candidate))) + 0.35
        mc = sum(1 for ch in clean_query if ch in clean_candidate)
        if (mc / len(clean_query)) < 0.5 or (mc / len(clean_candidate)) < 0.3: return 0
        return ((mc / len(clean_query)) + (mc / len(clean_candidate))) / 2.0

    def _find_best_event(self, clean_query, events):
        best, best_score = None, 0
        for ev in events:
            cand = ev.get('cleanTitle', '')
            score = self._score_event_match(clean_query, cand)
            if score > best_score and score >= 0.35:
                best_score = score
                best = ev
        return best, best_score

    def _record_card_history(self, card_name, event_dict):
        clean_title = event_dict.get('cleanTitle', '')
        if not clean_title: return
        history = self.card_event_history.setdefault(card_name, [])
        if any(h.get('cleanTitle') == clean_title for h in history): return
        history.append({
            'rawTitle': event_dict.get('rawTitle', ''),
            'cleanTitle': clean_title,
            'options': event_dict.get('options', [])
        })

    def _pick_closest_card_history(self, card_name, clean_query):
        candidates = self.card_event_history.get(card_name)
        if not candidates: return None, 0
        best, best_score = None, -999
        for ev in candidates:
            score = self._score_event_match(clean_query, ev.get('cleanTitle', ''))
            if score > best_score:
                best_score = score
                best = ev
        return best, best_score

    def _fallback_ambiguous_event(self, snapshot_title, fallback_event, score):
        if self.current_event_title != snapshot_title or self.event_confirmed_strong:
            return

        self.current_event_options = fallback_event.get('options', [])
        self.status_overlay.set_custom_status(
            f"⚠️ [카드 확인 실패 - 추정치] {self.matched_db_title} (동명 이벤트, 다른 카드일 수 있음)",
            "#ff9800", "#ff9800"
        )
        print(f"⚠️ [동명 이벤트 - 추정치 대체 표시] '{snapshot_title}' ➔ DB: '{self.matched_db_title}'")
        self.register_instant_estimated_zones()
        self.choice_scanner.reset_cache()

    def match_current_event_from_cache(self):
        if not self.current_event_title: return
        clean_ocr_ev = re.sub(r'[^가-힣0-9]', '', self.current_event_title)
        clean_ocr_ev = re.sub(r'여정|이벤트|아르카나|알카|노아|NOA', '', clean_ocr_ev)
        if not clean_ocr_ev or len(clean_ocr_ev) < 2: return

        self._event_matched_timestamp = time.time()

        best_event, best_score = self._find_best_event(clean_ocr_ev, self.db_event_cache) if self.db_event_cache else (None, 0)

        if self.current_matched_card_id and self.current_matched_card_id not in self.failed_card_searches:
            card_hist_best, card_hist_score = self._pick_closest_card_history(self.current_matched_card_id, clean_ocr_ev)
            if card_hist_best and card_hist_score >= 0.5:
                self.matched_db_title = card_hist_best.get('rawTitle', '')
                self.current_event_options = card_hist_best.get('options', [])
                self.event_confirmed = True
                self.event_confirmed_strong = True
                self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
                print(f"🎯 [카드별 역사 매칭 성공] 카드 '{self.current_matched_card_id}' ➔ '{self.matched_db_title}'")
                self.register_instant_estimated_zones()
                self.choice_scanner.reset_cache()
                return

            card_cached = self.resolved_card_events.get(self.current_matched_card_id)
            if card_cached and self._score_event_match(clean_ocr_ev, card_cached.get('cleanTitle', '')) >= 0.35:
                self.matched_db_title = card_cached.get('rawTitle', '')
                self.current_event_options = card_cached.get('options', [])
                self.event_confirmed = True
                self.event_confirmed_strong = True
                self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
                print(f"🎯 [카드 보완 매칭] 카드 '{self.current_matched_card_id}' 전용 데이터 사용 ➔ '{self.matched_db_title}'")
                self.register_instant_estimated_zones()
                self.choice_scanner.reset_cache()
                return

        STRONG_TITLE_MATCH_THRESHOLD = 0.5
        if best_event and best_score >= STRONG_TITLE_MATCH_THRESHOLD:
            is_ambiguous_title = best_event.get('cleanTitle', '') in self.ambiguous_arcana_titles
            self.matched_db_title = best_event.get('rawTitle', '')
            self.event_confirmed = True
            self.event_confirmed_strong = not is_ambiguous_title

            if is_ambiguous_title:
                self.current_event_options = []
                self.active_choice_zones = []
                self.status_overlay.set_custom_status(f"🟣 [카드 확인 중] {self.matched_db_title} (동명 이벤트, 카드 인식 대기)", "#c084fc", "#c084fc")
                print(f"🃏 [동명 이벤트 - 카드 확인 대기] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}'")
                self.choice_scanner.reset_cache()
                snapshot_title = self.current_event_title
                QTimer.singleShot(1200, lambda: self._fallback_ambiguous_event(snapshot_title, best_event, best_score))
                return

            self.current_event_options = best_event.get('options', [])
            self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
            print(f"🎯 [제목 기반 매칭 성공] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}'")
            if self.current_matched_card_id:
                self._record_card_history(self.current_matched_card_id, best_event)
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()
            return

        if best_event:
            is_ambiguous_title = best_event.get('cleanTitle', '') in self.ambiguous_arcana_titles
            self.matched_db_title = best_event.get('rawTitle', '')
            self.event_confirmed = True

            if is_ambiguous_title:
                self.current_event_options = []
                self.active_choice_zones = []
                self.status_overlay.set_custom_status(f"🟣 [카드 확인 중] {self.matched_db_title} (동명 이벤트, 카드 인식 대기)", "#c084fc", "#c084fc")
                print(f"🃏 [동명 이벤트(약한 매칭) - 카드 확인 대기] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}'")
                self.choice_scanner.reset_cache()
                snapshot_title = self.current_event_title
                QTimer.singleShot(1200, lambda: self._fallback_ambiguous_event(snapshot_title, best_event, best_score))
                return

            self.current_event_options = best_event.get('options', [])
            self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
            print(f"🎯 [제목 기반 매칭 성공(약함)] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}'")
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()

    def _on_model_loading_started(self):
        print("🪶 [모델 로딩] 게임 최초 감지 - OCR 모델 로딩 중")
        self.status_overlay.set_custom_status("🟡 [OCR 모델 로딩 중] 처음 한 번만 몇 초 걸려요...", "#ffcc00", "#ffcc00")

    def update_journey_status(self, is_in_journey):
        self.is_in_journey = is_in_journey
        if is_in_journey:
            if self.event_confirmed and self.matched_db_title:
                self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
            else:
                title_display = self.current_event_title or "탐색 중"
                self.status_overlay.set_custom_status(f"🔵 [이벤트 확인 중] {title_display}...", "#00aaff", "#00aaff")
        else:
            self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")
            self.tooltip.hide_and_clear()
            self.active_choice_zones = []
            self.current_event_options = []
            self.current_event_title = ""
            self.matched_db_title = ""
            self.event_confirmed = False
            self.event_confirmed_strong = False
            if hasattr(self, 'event_ocr'):
                self.event_ocr.last_event = ""
                self.event_ocr.pending_event = ""
                self.event_ocr.pending_count = 0
                self.event_ocr.completed_event_title = ""
                self.event_ocr.is_waiting_for_next_event = False
                self.event_ocr._last_title_gray = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape or event.key() == Qt.Key_F12:
            self.quit_app()

    DIFF_BADGE_COLORS = {"이지": "#3ddc84", "노말": "#ffcc4d", "하드": "#ff6b6b"}

    OUTCOME_BLOCK_STYLE = {
        "SUCCESS": ("rgba(61, 220, 132, 0.16)", "#3ddc84"),
        "FAIL": ("rgba(255, 92, 92, 0.16)", "#ff5c5c"),
    }

    def smart_format_reward(self, raw_reward, choice_name):
        esc = html_escape.escape
        if not raw_reward: return f"💡 <b>{esc(choice_name)}</b>", False, None

        lines = [l.strip() for l in raw_reward.strip().split('\n') if l.strip()]
        has_failure = any('[FAIL]' in l for l in lines)

        outcome_blocks = []
        current_block = []
        current_type = None

        for line in lines:
            clean_line = line
            line_type = 'NORMAL'

            if '[SUCCESS]' in line:
                line_type = 'SUCCESS'
                clean_line = line.replace('[SUCCESS]', '').strip()
            elif '[FAIL]' in line:
                line_type = 'FAIL'
                clean_line = line.replace('[FAIL]', '').strip()

            is_or_separator = clean_line.strip().lower() == 'or'

            if is_or_separator or (current_type and line_type != 'NORMAL' and line_type != current_type):
                if current_block:
                    outcome_blocks.append({'type': current_type or 'NORMAL', 'lines': current_block})
                    current_block = []

            if not is_or_separator:
                current_block.append(clean_line)
                if line_type != 'NORMAL':
                    current_type = line_type

        if current_block:
            outcome_blocks.append({'type': current_type or 'NORMAL', 'lines': current_block})

        formatted_outcomes = []
        column_content_widths = []
        pure_stat_words = {"잠재력 포인트", "컨디션", "스태미나", "오래된 동전", "오래된 주화", "힘", "체력", "인내", "집중", "보호", "선택"}

        for block in outcome_blocks:
            b_type = block['type']
            b_lines = [re.sub(r'[\.\s]+$', '', l) for l in block['lines'] if l != choice_name]

            merged_lines = []
            i = 0
            while i < len(b_lines):
                curr = b_lines[i]
                curr = re.sub(r'^(.+?)(?:\s+\1)+$', r'\1', curr).strip()

                if i + 1 < len(b_lines):
                    nxt = b_lines[i + 1].strip()
                    nxt = re.sub(r'^(.+?)(?:\s+\1)+$', r'\1', nxt).strip()
                    
                    if re.match(r'^[\+\-\d\%\(]', nxt) or nxt.startswith("할인"):
                        merged_lines.append(f"{curr} {nxt}")
                        i += 2
                        continue

                if curr in pure_stat_words: pass 
                else: merged_lines.append(curr)
                i += 1

            local_diff_marker = None
            local_conds = []
            final_bullets = []

            for item in merged_lines:
                if not item or item == choice_name: continue
                if item in ["이지", "노말", "하드"]:
                    local_diff_marker = item
                elif ("사용" in item or "소모" in item or "필요" in item) and "+" not in item:
                    local_conds.append(item)
                else:
                    final_bullets.append(item)

            def cond_icon(text):
                if "사용" in text or "소모" in text:
                    return "💰"
                return "📊"

            def render_conds(conds):
                return " &nbsp; ".join(f"{cond_icon(c)} {esc(c)}" for c in conds)

            html_parts = []
            _scale = getattr(self, 'tooltip_font_scale', 1.0)
            _badge_px = max(9, round(12 * _scale))
            _cond_px = max(9, round(12 * _scale))
            _bullet_px = max(10, round(14 * _scale))

            _measure_font = QFont()
            _measure_font.setBold(True)
            _measure_font.setPixelSize(max(_badge_px, _cond_px, _bullet_px))
            _metrics = QFontMetrics(_measure_font)

            def _text_px(t):
                try:
                    return _metrics.horizontalAdvance(t)
                except AttributeError:
                    return _metrics.width(t)

            _candidate_lines = []
            if local_diff_marker:
                _line = local_diff_marker
                if local_conds:
                    _line += "   " + "    ".join(f"{cond_icon(c)} {c}" for c in local_conds)
                _candidate_lines.append(_line)
            elif local_conds:
                _candidate_lines.append("    ".join(f"{cond_icon(c)} {c}" for c in local_conds))
            for _item in final_bullets:
                _candidate_lines.append("• " + _item)
            column_content_widths.append(max((_text_px(t) for t in _candidate_lines), default=0))

            if local_diff_marker:
                color = self.DIFF_BADGE_COLORS.get(local_diff_marker, "#00e5ff")
                header = (f'<span style="background-color:{color}; color:#111111; '
                          f'padding:2px 9px; border-radius:6px; font-weight:bold; font-size:{_badge_px}px;">'
                          f'{esc(local_diff_marker)}</span>')
                if local_conds:
                    header += (f'&nbsp;&nbsp;<span style="color:#ffb454; font-size:{_cond_px}px;">'
                               f'{render_conds(local_conds)}</span>')
                html_parts.append(header)
            elif local_conds:
                html_parts.append(f'<span style="color:#ffb454; font-size:{_cond_px}px;">{render_conds(local_conds)}</span>')

            for item in final_bullets:
                html_parts.append(f'<span style="color:#ffffff; font-size:{_bullet_px}px;">• {esc(item)}</span>')

            inner_html = "<br>".join(html_parts)

            if has_failure and b_type in self.OUTCOME_BLOCK_STYLE:
                bg, border = self.OUTCOME_BLOCK_STYLE[b_type]
                text = (f'<div style="background-color:{bg}; border-left:3px solid {border}; '
                        f'padding:4px 8px; border-radius:6px;">{inner_html}</div>')
            else:
                text = inner_html

            formatted_outcomes.append(text)

        if formatted_outcomes:
            _scale = getattr(self, 'tooltip_font_scale', 1.0)
            use_columns = _scale > 1.0 and len(formatted_outcomes) > 1
            min_width = None
            if use_columns:
                cell_parts = []
                CELL_SIDE_PADDING = 16
                FAIL_BLOCK_PADDING = 16 if has_failure else 0
                DIVIDER_WIDTH = 1 + CELL_SIDE_PADDING * 2
                LABEL_OUTER = 18 * 2 + 2 * 2
                SAFETY_MARGIN = 20

                total = LABEL_OUTER + SAFETY_MARGIN
                for i, outcome in enumerate(formatted_outcomes):
                    left_pad = 0 if i == 0 else CELL_SIDE_PADDING
                    right_pad = CELL_SIDE_PADDING
                    total += column_content_widths[i] + left_pad + right_pad + FAIL_BLOCK_PADDING
                    cell_parts.append(
                        f'<td style="vertical-align:top; white-space:nowrap; padding:2px {right_pad}px 2px {left_pad}px;">{outcome}</td>'
                    )
                    if i < len(formatted_outcomes) - 1:
                        cell_parts.append('<td width="1" style="background-color:#3a3a3a;">&nbsp;</td>')
                        total += DIVIDER_WIDTH
                min_width = round(total)
                body = (
                    '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;">'
                    f'<tr>{"".join(cell_parts)}</tr></table>'
                )
            else:
                block_divider = '<hr style="border:none;border-top:1px solid #333333;margin:6px 0;">'
                body = block_divider.join(f'<div style="margin-bottom:2px;">{o}</div>' for o in formatted_outcomes)
            html = f'💡 <b>{esc(choice_name)}</b><hr style="border:none;border-top:1px solid #444444;margin:4px 0;">{body}'
            return html, use_columns, min_width
        else:
            return f'💡 <b>{esc(choice_name)}</b>', False, None


if __name__ == '__main__':
    # 🖥️ [DPI 스케일링 대응] Windows 디스플레이 배율이 100%가 아닌 경우(125%, 150% 등 —
    # 특히 고해상도 노트북/모니터 유저에게 흔함), DPI-aware 선언이 없으면 Windows가 이
    # 프로세스를 "구식 프로그램"으로 보고 좌표를 가짜로 축소해서 넘겨준다. 그러면 mss가
    # 캡처하는 실제 물리 픽셀 좌표와 Qt가 인식하는 화면 크기(screen_w/screen_h)가 서로
    # 어긋나서, 그 비율로 계산되는 카드/제목/선택지 캡처 영역이 통째로 틀어질 수 있다.
    # QApplication을 만들기 전에 프로세스를 Per-Monitor DPI Aware로 선언해두면 Windows가
    # 더 이상 좌표를 축소해서 주지 않으므로, mss와 Qt가 항상 같은 좌표계(실제 물리 픽셀)를
    # 보게 되어 이 어긋남 자체가 원천적으로 발생하지 않는다.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE (Windows 8.1+)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Windows 7 폴백 (System DPI Aware)
        except Exception:
            pass

    # 🔒 [중복 실행 방지] 이미 켜져 있으면 새 인스턴스는 알림만 띄우고 바로 종료
    if not ensure_single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(0, "JourneyHelper가 이미 실행 중입니다.\n작업표시줄 트레이 아이콘을 확인해주세요.", "이미 실행 중", 0x40)
        except Exception:
            pass
        sys.exit(0)

    # 🩹 [크래시 로깅] 다른 초기화보다 먼저 걸어야 이후 어떤 단계에서 죽어도 잡힌다.
    _crash_log_path = install_crash_logger()
    print(f"🩹 [크래시 로깅 활성화] {_crash_log_path}")

    # ⚙️ [설정 반영] 로그 파일 저장 여부는 설정창에서 바꾼 값을 따른다.
    _user_settings = load_settings()
    if _user_settings.get("file_logging"):
        _log_path = enable_file_logging()
        if _log_path:
            print(f"📝 [파일 로그 활성화] {_log_path}")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 트레이만 남는 상태에서 실수로 마지막 창이 닫혀도 안 꺼지게
    manager = MouseHoverManager()
    sys.exit(app.exec_())

