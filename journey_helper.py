import sys
import os

# ⚠️ [PyTorch + OpenCV OpenMP 충돌 방지] torch와 opencv가 각자 다른 OpenMP 런타임 DLL을
# 들고 있어서, exe로 패키징하면 둘 다 같은 폴더에 들어가며 충돌해 "DLL 초기화 루틴을
# 실행할 수 없습니다" 에러가 나는 경우가 많다. torch/cv2를 import하기 전에 이 환경변수를
# 설정해두면 중복 로드를 허용해서 이 문제를 막을 수 있다. (아래 import보다 반드시 먼저 와야 함)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import time
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

# 불필요한 시스템 경고 콘솔 난립 억제 필터
warnings.filterwarnings('ignore')

torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))

from PyQt5.QtCore import QUrl, Qt, QThread, pyqtSignal, QTimer, QRect, QPoint
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QSystemTrayIcon,
                              QMenu, QAction, QDialog, QCheckBox, QPushButton, QFrame, QComboBox)
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

# 🙈 [자기 자신을 화면 캡처에서 제외] is_loading_screen_visible()이 game_rect 전체를
# mss로 실제 화면 캡처해서 흰색 비율/평균 밝기를 판정하는데, 우리 앱의 툴팁/상태창처럼
# 항상 위에 떠 있는(WindowStaysOnTopHint) 오버레이 창이 그 캡처 범위 안에 걸쳐 있으면
# 그 창이 덮은 만큼 어두운 픽셀로 잡혀서, 진짜 흰 로딩 화면인데도 밝기 임계값을 못 넘겨
# "로딩 감지 실패 → 툴팁 영원히 안 지워짐" 자기 자신을 방해하는 악순환이 생길 수 있다.
# Windows 10 2004+ 의 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)를 걸어두면
# 이 창들은 실제 화면 캡처(BitBlt/DXGI 기반, mss 포함) 결과에서 아예 빠지므로 이 문제를
# 원천 차단한다(사용자 눈에는 평소처럼 계속 보임 — 오직 "캡처 결과"에서만 빠짐).
WDA_EXCLUDEFROMCAPTURE = 0x00000011

def exclude_window_from_capture(widget):
    try:
        hwnd = int(widget.winId())
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        if not ok:
            # 구버전 Windows(2004 이전)라 지원 안 하는 경우 등. 조용히 무시(치명적이지 않음).
            pass
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
    # 🖼️ [트레이 아이콘] 외부 이미지 파일 없이 코드로 간단히 아이콘을 그려서 사용한다.
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

# 🔒 [중복 실행 방지] 이미 실행 중이면 새 인스턴스는 알림만 띄우고 바로 종료한다.
# (실수로 exe를 두 번 클릭해도 툴팁/상태배지가 겹쳐서 뜨는 걸 방지)
_SINGLE_INSTANCE_MUTEX_NAME = "Global\\JourneyHelper_SingleInstance_Mutex"
_single_instance_mutex_handle = None  # GC로 핸들이 회수되지 않도록 전역에 계속 들고 있는다

def ensure_single_instance():
    global _single_instance_mutex_handle
    try:
        _single_instance_mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        ERROR_ALREADY_EXISTS = 183
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        return True  # 확인 자체가 실패하면 그냥 실행되게 둔다 (과도한 차단 방지)

# ----------------------------------------------------
# ⚙️ 설정 저장/불러오기 (상단 배지 우클릭 → 설정창)
# ----------------------------------------------------
APP_VERSION = "1.0.0"

DEFAULT_SETTINGS = {
    "file_logging": False,
    "show_card_preview": False,
    "show_region_debug": False,
    "card_region_override": None,
    "tooltip_font_scale": 1.0,
}

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
    # 콘솔이 없는 --windowed 빌드에서도 print 로그를 파일로 계속 남기기 위한 스트림.
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

# 🌕 [로딩 화면 감지] N.O.A 로고 + 거의 흰색에 가까운 배경 그라데이션이 뜨는 여정 로딩 화면 감지.
# 일반 게임플레이 화면과 달리 전체 화면 대부분이 매우 밝은 흰색/연회색이라는 점을 이용한다.
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
        bright_ratio = float(np.mean(small > 200))   # 밝은(흰색 계열) 픽셀 비율
        mean_brightness = float(np.mean(small))
        return bright_ratio > 0.50 and mean_brightness > 180
    except Exception:
        return False

REGION_FRACTIONS = {
    "card_art":    {"top": 0.120, "left": 0.050, "width": 0.052, "height": 0.163},
    "event_title": {"top": 0.205, "left": 0.110, "width": 0.240, "height": 0.056},
    "arcana_label":{"top": 0.170, "left": 0.110, "width": 0.200, "height": 0.032},
}
_DEFAULT_CARD_ART_FRACTION = dict(REGION_FRACTIONS["card_art"])  # 사용자가 "기본값으로" 되돌릴 때 쓰는 원본 스냅샷
_card_region_manual_override = False  # True면 자동 보정이 card_art 영역을 건드리지 않는다
_auto_calibration_done = False
_auto_calibration_attempts = 0
_last_calibrated_rect = None  
_calibration_samples = []  
_CALIBRATION_SAMPLES_NEEDED = 1

def try_auto_calibrate_regions(reader, game_rect):
    global _auto_calibration_done, REGION_FRACTIONS, _last_calibrated_rect, _calibration_samples
    try:
        sct = mss.MSS()
        scan_region = {
            "top": game_rect["top"],
            "left": game_rect["left"],
            "width": int(game_rect["width"] * 0.45),
            "height": int(game_rect["height"] * 0.42),
        }
        img = np.array(sct.grab(scan_region))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        results = reader.readtext(gray, detail=1)

        label_box = None
        for bbox, text, conf in results:
            if "아르카나" in text or "여정" in text:
                bx1, by1 = bbox[0]
                if by1 < scan_region["height"] * 0.06:
                    print(f"🪟 [자동 보정] '{text}' 후보는 화면 맨 위쪽이라 저희 앱 자신의 UI로 의심 → 건너뜀")
                    continue
                label_box = bbox
                break
        if not label_box:
            print("🪟 [자동 보정] 화면에서 '아르카나 이벤트'/'여정 이벤트' 글자를 못 찾음 → 기본값 사용")
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
            if by1 > ly1 and by1 < ly2 + label_h * 5 and bh > label_h * 1.15:
                if title_box is None or by1 < title_box[0][1]:
                    title_box = bbox
        if not title_box:
            print("🪟 [자동 보정] 라벨은 찾았지만 이벤트 제목으로 보이는 큰 글씨를 못 찾음 → 기본값 사용")
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

        card_left_px = lx1_px - gw * 0.067
        card_top_px = ly1_px - gh * 0.0556
        card_region_frac = px_to_frac(card_left_px, card_top_px, gw * 0.051, gh * 0.160)

        REGION_FRACTIONS["arcana_label"] = label_region_frac
        REGION_FRACTIONS["event_title"] = title_region_frac
        if not _card_region_manual_override:
            REGION_FRACTIONS["card_art"] = card_region_frac
        else:
            print("🃏 [카드 영역] 사용자가 직접 지정한 영역이 있어 자동 보정을 건너뜀")
        _auto_calibration_done = True
        _last_calibrated_rect = dict(game_rect)
        _calibration_samples = []
        print(f"🪟 [자동 보정 성공, {_CALIBRATION_SAMPLES_NEEDED}회 측정 중간값 사용] 라벨/제목/카드 캡처 영역 재계산 완료 (라벨:{label_region_frac}, 제목:{title_region_frac}, 카드:{card_region_frac})")
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
            # 🩹 [OCR 오염 방지] 세 박스(제목/라벨/카드)가 세로로 촘촘히 붙어있어서, 라벨 글자를
            # 박스 위나 아래에 그리면 바로 인접한 다른 캡처 영역 안으로 들어가 그 영역의 OCR이
            # 우리 오버레이 글자 자체를 읽어버리는 문제가 있었다(제목 박스의 "제목" 글자가
            # 라벨 박스 안으로 들어간 사례). 대신 박스 오른쪽 옆에 그려서 세로로 쌓인 다른
            # 캡처 영역과 겹치지 않게 한다.
            painter.drawText(rect["left"] + rect["width"] + 6, rect["top"] + 14, label)

class CardRegionAdjustOverlay(QWidget):
    # 🎯 [카드 인식 영역 직접 조정] 반투명 박스를 드래그로 옮기고 모서리를 끌어 크기를 조절.
    # 저장하면 콜백으로 fraction(게임 창 기준 비율)을 넘겨준다. None을 넘기면 "기본값으로".
    HANDLE_SIZE = 10

    def __init__(self, on_save=None, on_cancel=None):
        super().__init__()
        self._on_save = on_save
        self._on_cancel = on_cancel
        # ⚠️ 이 오버레이는 드래그 조작을 받아야 하므로 다른 오버레이들과 달리
        # WindowTransparentForInput을 일부러 안 쓴다 (= 조정 중엔 게임 클릭이 막힘, 의도된 동작).
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
        # ⚠️ 클릭으로 접었다 켤 수 있어야 하므로 이 배지만 WindowTransparentForInput을 빼서
        # 마우스 클릭을 직접 받도록 한다. (다른 오버레이들은 그대로 클릭 통과 유지)
        # Qt.WindowDoesNotAcceptFocus로 클릭해도 이 창이 포커스를 가져가지 않게 해서
        # 게임 창의 포커스를 뺏지 않는다.
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.label = QLabel("🟡 [DB 연결 중] 웹사이트 실시간 파싱 준비 중...", self)
        self.label.setCursor(Qt.PointingHandCursor)

        self._is_collapsed = False
        self._last_text = "🟡 [DB 연결 중] 웹사이트 실시간 파싱 준비 중..."
        self._last_border = "#ffcc00"
        self._last_text_color = "#ffcc00"
        self.on_settings_requested = None  # 우클릭 시 호출될 콜백 (MouseHoverManager가 나중에 연결)
        self.on_reset_requested = None  # 좌클릭(접기/펼치기) 시 호출될 콜백 - 추적 중인 데이터 초기화용
        self.setToolTip("좌클릭: 접기/펼치기 + 새로고침 · 우클릭: 설정")

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self._render()
        self.reposition()
        self.show()
        exclude_window_from_capture(self)  # 🙈 로딩 화면 감지용 스크린샷에 이 상태창 자신이 안 잡히게 (화면 정중앙 상단이라 특히 중요)

    def reposition(self):
        game_rect = get_game_window_rect()
        if game_rect:
            base_left, base_top, base_w = game_rect["left"], game_rect["top"], game_rect["width"]
        else:
            base_left, base_top, base_w = 0, 0, QApplication.desktop().screenGeometry().width()
        self.move(int(base_left + base_w / 2 - self.width() / 2), base_top + 4)

    def set_custom_status(self, text, border_color="#00e5ff", text_color="#00e5ff"):
        # 접혀 있어도 최신 상태 텍스트는 계속 기억해둔다 → 활성화하는 순간 바로 최신 내용이 보임
        self._last_text = text
        self._last_border = border_color
        self._last_text_color = text_color
        self._render()

    def _render(self):
        if self._is_collapsed:
            display_text = "🔽 (클릭하여 활성화)"
        else:
            display_text = self._last_text
        self.label.setText(display_text)
        self.label.setStyleSheet(f"""
            QLabel {{ background-color: rgba(10, 14, 24, 245); border: 2px solid {self._last_border}; border-radius: 15px; color: {self._last_text_color}; font-size: 13px; font-weight: bold; padding: 6px 18px; }}
        """)
        self.adjustSize()
        self.reposition()
        self.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_collapsed = not self._is_collapsed
            self._render()
            if callable(self.on_reset_requested):
                self.on_reset_requested()
        elif event.button() == Qt.RightButton:
            if callable(self.on_settings_requested):
                self.on_settings_requested()
        super().mousePressEvent(event)

class SettingsDialog(QDialog):
    def __init__(self, on_quit=None,
                 on_toggle_card_preview=None, on_toggle_region_debug=None,
                 on_open_region_adjuster=None, on_font_scale_change=None, parent=None):
        super().__init__(parent)
        self._on_quit = on_quit
        self._on_toggle_card_preview = on_toggle_card_preview
        self._on_toggle_region_debug = on_toggle_region_debug
        self._on_open_region_adjuster = on_open_region_adjuster
        self._on_font_scale_change = on_font_scale_change
        self.setWindowTitle("JourneyHelper 설정")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setFixedWidth(360)
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
        """)

        self._settings = load_settings()

        layout = QVBoxLayout()
        layout.setSpacing(10)

        title = QLabel("⚙️ 설정")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #00e5ff;")
        layout.addWidget(title)

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

        self.chk_log = QCheckBox("문제 발생 시 진단용 로그를 파일로 저장")
        self.chk_log.setChecked(self._settings.get("file_logging", False))
        layout.addWidget(self.chk_log)

        log_hint = QLabel(f"저장 위치: {get_log_path()}")
        log_hint.setStyleSheet("color: #888888; font-size: 10px;")
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

        debug_hint = QLabel("카드 인식이 잘 안 되거나 캡처 위치가 어긋나는 것 같을 때, 실제로 어느 영역을\n찍고 있는지 눈으로 바로 확인할 수 있습니다. 체크 즉시 화면에 반영됩니다.")
        debug_hint.setStyleSheet("color: #888888; font-size: 10px;")
        debug_hint.setWordWrap(True)
        layout.addWidget(debug_hint)

        btn_adjust_region = QPushButton("🎯 카드 인식 영역 직접 조정...")
        btn_adjust_region.clicked.connect(self._open_region_adjuster)
        layout.addWidget(btn_adjust_region)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("background-color: #2a3442;")
        layout.addWidget(line2)

        version_label = QLabel(f"현재 버전: {APP_VERSION}")
        layout.addWidget(version_label)

        button_row = QHBoxLayout()
        btn_quit = QPushButton("🛑 프로그램 종료")
        btn_quit.setStyleSheet("QPushButton { background-color: #4a1f24; border-color: #7a2f36; } QPushButton:hover { background-color: #6b2a30; }")
        btn_quit.clicked.connect(self._quit_program)
        button_row.addWidget(btn_quit)
        button_row.addStretch()
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self._save_and_close)
        button_row.addWidget(btn_close)
        layout.addLayout(button_row)

        self.setLayout(layout)

    def _quit_program(self):
        # 종료 전에 지금까지 바꾼 설정도 같이 저장해준다
        self._settings["file_logging"] = self.chk_log.isChecked()
        self._settings["show_card_preview"] = self.chk_card_preview.isChecked()
        self._settings["show_region_debug"] = self.chk_region_debug.isChecked()
        self._settings["tooltip_font_scale"] = self._current_font_scale()
        save_settings(self._settings)
        self.accept()
        if callable(self._on_quit):
            self._on_quit()

    def _open_region_adjuster(self):
        if callable(self._on_open_region_adjuster):
            self._on_open_region_adjuster()
        self.close()  # 조정 오버레이가 게임 화면을 덮으니, 설정창은 닫아서 안 겹치게 함

    def _on_font_scale_selected(self, index):
        scale = self._font_scale_options[index][1]
        if callable(self._on_font_scale_change):
            self._on_font_scale_change(scale)

    def _current_font_scale(self):
        return self._font_scale_options[self.combo_font_scale.currentIndex()][1]

    def _save_and_close(self):
        self._settings["file_logging"] = self.chk_log.isChecked()
        self._settings["show_card_preview"] = self.chk_card_preview.isChecked()
        self._settings["show_region_debug"] = self.chk_region_debug.isChecked()
        self._settings["tooltip_font_scale"] = self._current_font_scale()
        save_settings(self._settings)
        self.accept()

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
        # 🖼️ [가로 2단 배치 폭 배율] 표(<table>)로 옆에 한 컬럼이 더 붙으므로, 기본 폭의
        # 약 1.9배로 넓혀야 두 번째 컬럼 내용과 오른쪽 테두리가 잘리지 않는다.
        self._wide_multiplier = 1.9
        self.label.setStyleSheet(self._build_stylesheet(self._base_font_px))
        self.label.setWordWrap(True)
        self.setFixedWidth(self._base_width)
        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.reposition()
        exclude_window_from_capture(self)  # 🙈 로딩 화면 감지용 스크린샷에 이 툴팁 자신이 안 잡히게

    def _build_stylesheet(self, font_px):
        return f"""
            QLabel {{ background-color: rgba(10, 12, 18, 250); border: 2px solid #00e5ff; border-radius: 12px; color: #ffffff; font-size: {font_px}px; font-weight: bold; padding: 14px 18px; line-height: 150%; }}
        """

    def _apply_width(self, wide, min_width=None):
        # ⚙️ [폭 재계산] 가로모드일 땐 smart_format_reward가 실측해서 넘겨준 min_width를
        # 그대로 우선 사용한다(대충 정한 배율보다 정확함). min_width가 없을 때만
        # 배율 기반 폴백을 쓴다. 일반(세로) 모드는 기존처럼 배율만 적용.
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
        # ⚙️ [설정에서 글자 크기 조정] 기본(13px)에 배율을 곱해서 반영. 실시간 반영을 위해
        # 즉시 스타일시트를 다시 적용한다. 글자가 커지는데 박스 너비는 그대로면 텍스트가
        # 테두리를 침범하므로, 너비도 같은 배율로 같이 늘려준다.
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
        # 🛡️ [컴포지팅 잔상 방어] 이 위젯은 FramelessWindowHint + WA_TranslucentBackground +
        # WindowStaysOnTopHint 조합이라, 일부 Windows 환경에서 hide()를 호출해도 DWM
        # 컴포지팅이 해당 영역을 즉시 다시 안 그려서 이전에 표시하던 내용이 화면에 잔상처럼
        # 남는 경우가 실제로 확인됐다(로그상 정리 로직은 정상 실행됐는데도 화면엔 옛날
        # 툴팁 내용이 계속 보임). hide() 하나만 믿지 않고, 내용을 비우고 화면 밖으로
        # 치운 뒤 숨겨서, 설령 잔상이 남더라도 최소한 "틀린 옛날 정보"가 보이는 일은 없게 한다.
        #
        # 🔍 [진단용 로그] 다음에 또 재현되면, 그 시점 로그에 이 줄이 찍혀 있는지로
        # "코드는 지우라고 명령했는데 화면에서 실제로 안 지워진 것"인지, 아니면
        # "애초에 hide 명령 자체가 이 순간엔 안 내려간 것"(다른 코드 경로 문제)인지
        # 구분할 수 있다.
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

    def __init__(self, screen_w, screen_h, pause_getter=None):
        super().__init__()
        try: self.reader = easyocr.Reader(['ko'], gpu=True)
        except Exception: self.reader = easyocr.Reader(['ko'], gpu=False)
        self.sct = mss.MSS()
        self.screen_w, self.screen_h = screen_w, screen_h
        self.last_event = ""
        self.empty_count = 0
        self.pending_event = ""
        self.pending_count = 0
        self.pause_getter = pause_getter or (lambda: False)
        self.is_arcana_label = True 
        self._last_label_log = 0
        self._last_label_raw_log = 0
        
        self._last_title_gray = None
        self._cached_combined = ""
        self._cached_raw_joined = ""
        self._last_label_gray = None
        self._cached_label_text = ""

        # 🔒 [선택 완료 락 변수] 선택 후 대화 연출 동안 동일 제목 재인식 방지
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

                if title_diff < 1.0:
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
                    # 🔒 완료된 이전 이벤트 제목이 연출 동안 화면 상단에 떠있으면 무시!
                    if self.is_waiting_for_next_event:
                        if self._is_same_as_completed(combined):
                            self.msleep(150)
                            continue
                        else:
                            # 전혀 다른 신규 이벤트 제목 등장 시 락 자동 해제
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
                        print(f"🔁 [OCR 중복 무시] '{combined}' ≈ 기존 '{self.last_event}' (재검색 생략)")
                    elif self.pending_count >= 1 and combined != self.last_event:
                        print(f"🔍 [OCR RAW] '{raw_joined}' → [보정] '{combined}'")
                        self.last_event = combined
                        print(f"📌 [OCR 감지] {combined}")
                        self.event_detected.emit(combined)
                else:
                    self.pending_event = ""
                    self.pending_count = 0
                    if self.is_waiting_for_next_event:
                        # 🔒 [선택 후 대화 연출 보호] 선택 직후엔 대화창이 뜨면서 제목 영역이
                        # 일부러 비어 보이는 게 정상이다. 메인 스레드(on_choice_selected_action,
                        # 2617~2619줄)는 이 기간 동안 이전 선택지 툴팁/좌표를 일부러 유지해두는데,
                        # 이 스레드가 "제목이 안 보이니 여정 이탈"이라고 오판해서
                        # journey_status_changed(False)를 쏘면 update_journey_status가 그 유지된
                        # 상태를 전부 지워버려, 대화 연출 중에도 상단엔 "헬퍼 대기 중"이 뜨고
                        # 좌측 툴팁만 지워지지 않고 남는 등 상태가 서로 어긋나는 원인이 됐다.
                        # 잠금 중엔 "이탈 아님"으로 보고 empty_count를 쌓지 않는다.
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

                    # ⚡ [프레임 캐싱] 제목 영역과 동일하게, 화면이 안 바뀌었으면 OCR을 다시 돌리지 않는다.
                    # (루프 주기를 800ms→150ms로 줄인 만큼, 캐싱 없이는 라벨 OCR 호출이 5배로 늘어난다)
                    label_diff = 999.0
                    if self._last_label_gray is not None and label_gray.shape == self._last_label_gray.shape:
                        label_diff = cv2.absdiff(label_gray, self._last_label_gray).mean()

                    if label_diff < 1.0:
                        label_text = self._cached_label_text
                    else:
                        self._last_label_gray = label_gray.copy()
                        label_resized = cv2.resize(label_gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
                        label_results = self.reader.readtext(label_resized, detail=0, decoder='greedy')
                        label_text = " ".join(label_results).strip()
                        # 🩹 [상자 위치는 그대로, 텍스트만 필터링] arcana_label과 event_title
                        # 캡처 영역이 화면상 서로 인접해 있어서(사람이 눈으로 정밀 보정해둔
                        # 좌표라 건드리지 않음) 라벨 쪽 OCR이 "제목"이라는 title 영역의 UI
                        # 글자를 같이 집어오는 경우가 있다. 좌표를 옮기는 대신, 여기서
                        # "제목"이라는 문구와 그로 인해 떨어져 나온 한 글자짜리 잔상("제" 등)을
                        # 텍스트 단계에서 지워버려서 판정/로그에 섞여 들어가지 않게 한다.
                        label_text = label_text.replace("제목", "").strip()
                        label_text = re.sub(r'(^|\s)제(\s|$)', r'\1\2', label_text).strip()
                        label_text = re.sub(r'\s+', ' ', label_text)
                        self._cached_label_text = label_text

                    now_l = time.time()
                    if now_l - self._last_label_raw_log > 5:
                        self._last_label_raw_log = now_l
                        print(f"🏷️ [라벨 영역 원본 OCR] '{label_text}' (캘리브레이션 확인용, 캡처영역 top={label_monitor['top']} left={label_monitor['left']} w={label_monitor['width']} h={label_monitor['height']})")

                    # 🩹 [OCR 오탈자 대응] "아르카나"를 완전 일치 부분문자열로만 찾으면
                    # "똑르카나"처럼 OCR이 흔들려서 오인식한 경우 계속 놓친다. 이미 카드
                    # 매칭용으로 등록돼 있던 NOISE_EVENT_WORDS의 아르카나 오탈자 변형들을
                    # 재사용해서 라벨 판정도 그 변형들 중 하나라도 포함되면 아르카나로 인정한다.
                    _ARCANA_VARIANTS = ("아르카나", "알카나", "알카", "아드가나", "아드카나", "아르가나")
                    is_arcana_hit = any(v in label_text for v in _ARCANA_VARIANTS)

                    if is_arcana_hit:
                        if not self.is_arcana_label and now_l - self._last_label_log > 2:
                            self._last_label_log = now_l
                            print(f"🏷️ [라벨] '아르카나 이벤트'로 전환 (인식문구:'{label_text}') → 카드 스캔 재개")
                        self.is_arcana_label = True
                    elif label_text:
                        # 🩹 [상태 유지 버그 수정] 예전에는 "여정"이 아니면(예: "훈련" 이벤트,
                        # 혹은 위 변형에도 안 걸리는 다른 오인식) 아무 것도 안 하고 넘어가서
                        # is_arcana_label이 이전 값 그대로 남아있었다. 아르카나가 아닌 라벨이
                        # 무엇이든(여정/훈련/기타) 확실히 False로 내려서 카드 스캔을 멈춘다.
                        if self.is_arcana_label and now_l - self._last_label_log > 2:
                            self._last_label_log = now_l
                            print(f"🏷️ [라벨] '{label_text}'(비-아르카나)로 전환 → 카드 스캔 일시중지")
                        self.is_arcana_label = False
                    self.arcana_label_detected.emit(self.is_arcana_label)
                except Exception:
                    pass

            except Exception as e:
                print(f"⚠️ [EventTitleOCR 예외] {e}")
            self.msleep(150)  # ⚡ 제목 변화 감지 지연을 줄이기 위해 800ms → 150ms (프레임 캐싱 덕에 저렴함)

class EventCardArtThread(QThread):
    card_matched = pyqtSignal(str, str)
    preview_signal = pyqtSignal(np.ndarray)
    raw_preview_signal = pyqtSignal(np.ndarray)

    def __init__(self, screen_w, screen_h, hash_cache_getter, pause_getter=None, lenient_getter=None):
        super().__init__()
        self.sct = mss.MSS()
        self.screen_w, self.screen_h = screen_w, screen_h
        self.get_hash_cache = hash_cache_getter
        self.pending_id = None
        self.pending_count = 0
        self.last_matched_id = ""
        self.pause_getter = pause_getter or (lambda: False)
        # 🃏 [널널 모드] "이격 아르카나"처럼 후보가 이미 소수의 카드로 좁혀진 상황에서는
        # pdist 문턱값은 따질 필요 없이 그중 가장 점수 좋은 후보를 채택하되, 점수 자체가
        # 19점을 넘는(너무 안 닮은) 후보는 여전히 거른다 (완전히 엉뚱한 카드 오매칭 방지).
        self.lenient_getter = lenient_getter or (lambda: False)
        self._last_heartbeat = 0
        self._fail_streak_start = None  # 🩹 [자동 재보정] 연속 실패가 시작된 시각
        self._recalib_triggered_this_streak = False

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
                
                self.raw_preview_signal.emit(cv_bgr.copy())
                cap_phash, cap_hist, flat_card = extract_card_features(cv_bgr)
                self.preview_signal.emit(flat_card)

                if any((cap_phash - h) <= NON_CARD_HASH_THRESHOLD for h in KNOWN_NON_CARD_HASHES):
                    self.pending_id = None
                    self.pending_count = 0
                    self.msleep(150)  # ⚡ 카드 인식 확정 속도 개선 (300ms→150ms)
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
                    # 🩹 [자동 재보정] 그럴듯한 후보가 하나라도 잡혔다는 건 영역 자체는 대체로
                    # 맞다는 신호이므로, 연속 실패 스트릭을 리셋한다.
                    self._fail_streak_start = None
                    self._recalib_triggered_this_streak = False

                    if best_id == self.pending_id:
                        self.pending_count += 1
                    else:
                        self.pending_id = best_id
                        self.pending_count = 1

                    if self.pending_count >= 1 and best_id != self.last_matched_id:
                        self.last_matched_id = best_id
                         # 📌 이격 아르카나 매칭 적용 여부에 따라 깔끔하게 1줄만 출력
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

                    # 🩹 [자동 재보정] "아르카나 이벤트" 화면인데도(=이 스레드가 돌고 있다는 것
                    # 자체가 그 뜻) 카드를 계속 못 찾으면, 캡처 영역 자체가 잘못됐을 가능성을
                    # 의심해서 자동 보정을 처음부터 다시 시도한다. 사람이 직접 고정해둔 영역이
                    # 있으면 존중해서 건드리지 않는다. 짧은 화면 전환 등과 혼동하지 않도록
                    # 충분히 오래(10초) 연속으로 실패했을 때만 한 번 트리거한다.
                    if self._fail_streak_start is None:
                        self._fail_streak_start = now
                    elif not self._recalib_triggered_this_streak and now - self._fail_streak_start > 10:
                        self._recalib_triggered_this_streak = True
                        self._trigger_recalibration()
            except Exception:
                pass
            self.msleep(150)  # ⚡ 카드 인식 확정 속도 개선 (300ms→150ms)

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
    # ⚡ [실제 화면 글자 Y좌표 정밀 스캔] 이벤트 진입 시 선택지 글자의 Bounding Box(실제 Y 픽셀)를 단 1번 스캔하여 100% 정확하게 등록
    choices_scanned = pyqtSignal(list)

    def __init__(self, screen_w, pause_getter=None, options_getter=None):
        super().__init__()
        try: self.reader = easyocr.Reader(['ko'], gpu=True)
        except Exception: self.reader = easyocr.Reader(['ko'], gpu=False)
        self.sct = mss.MSS()
        self.screen_w = screen_w
        self.pause_getter = pause_getter or (lambda: False)
        self.options_getter = options_getter or (lambda: [])
        self.has_scanned_current_event = False

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

    # ⚡ 선택지 영역 수평 픽셀 밀도 프로젝션을 이용한 100% 정밀 Y좌표 검출 (실측 버튼두께 68px + 유격 8px 반영)
    def detect_button_zones_projection(self, gray_img, monitor_top, options, base_top=None, base_h=None):
        try:
            h_img, w_img = gray_img.shape[:2]
            
            # 선택지 문구/아이콘이 존재하는 중앙 가로 스트립 구역 추려내기
            text_strip = gray_img[:, int(w_img * 0.10):int(w_img * 0.90)]
            
            # 각 행(Row)별 최대 픽셀 밝기값 계산
            row_max = np.sum(text_strip, axis=1) / float(text_strip.shape[1])
            
            # 밝은 글자/아이콘 픽셀 존재 행 판별 (밝기 110 이상)
            active_rows = row_max > 110
            
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

            # 같은 선택지 상자에 속한 근접 행들 묶기 (간격 22px 이내)
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
                # 🛡️ [위치 기반 검증] 이 방식은 텍스트 내용을 전혀 보지 않고 밝은 블록 개수만으로
                # 선택지를 위치 순서대로 기계적으로 배정하기 때문에, 캐릭터 초상화/HP바 등
                # 선택지가 아닌 밝은 UI 요소가 우연히 개수를 맞추면 완전히 엉뚱한 위치를 그대로
                # 선택지로 확정해버릴 위험이 있다. 각 블록이 "있어야 할" 예상 위치(피치 간격 기반)에서
                # 너무 벗어나면 이 결과 전체를 신뢰하지 않고 버려서, 더 안전한 2차 OCR 텍스트 매칭으로 넘긴다.
                if base_top is not None and base_h is not None:
                    bottom_anchor_y = base_top + int(base_h * 0.724)
                    pitch_px = int(base_h * 0.07037)
                    y_sanity_tolerance = max(12, int(base_h * 0.015))
                    count = len(options)
                    for i, (y1, y2) in enumerate(merged):
                        center_y_abs = monitor_top + (y1 + y2) // 2
                        expected_cy = bottom_anchor_y - (count - 1 - i) * pitch_px
                        if abs(center_y_abs - expected_cy) > y_sanity_tolerance:
                            print(f"🟡 [1차 프로젝션 오인식 의심] 블록 {i+1} 위치 {center_y_abs}px가 예상 위치 {expected_cy}px에서 {abs(center_y_abs - expected_cy)}px 벗어남 → 2차 OCR 매칭으로 넘김")
                            return None

                zones = []
                btn_half = 34  # 실측 버튼 두께 68px의 절반 = 34px
                for i, (y1, y2) in enumerate(merged):
                    center_y = (y1 + y2) // 2
                    zones.append({
                        'y_min': monitor_top + center_y - btn_half,
                        'y_max': monitor_top + center_y + btn_half,
                        'option': options[i],
                        'text': options[i].get('choice', '')
                    })
                return zones
        except Exception:
            pass
        return None


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

            options = self.options_getter() or []
            if not options or len(options) < 2:
                # 🛡️ 실제 분기 선택지가 없는(0~1개) 이벤트는 화면에 호버 가능한
                # 선택 버튼이 없으므로 스캔을 건너뛰고 zone을 비운다.
                self.choices_scanned.emit([])
                self.has_scanned_current_event = False
                self.msleep(300)
                continue

            # 이미 현재 이벤트의 1회 정밀 스캔이 완료되었으면 추가 연산 없이 대기
            if self.has_scanned_current_event:
                self.msleep(300)
                continue

            game_rect = get_game_window_rect()
            if game_rect:
                base_left, base_top, base_w, base_h = game_rect["left"], game_rect["top"], game_rect["width"], game_rect["height"]
            else:
                base_left, base_top, base_w, base_h = 0, 0, self.screen_w, 1080

            # 선택지가 위치한 우측 구역만 타이트하게 캡처 (X: 62% ~ 96%, Y: 35% ~ 82%)
            monitor = {
                "top": base_top + int(base_h * 0.35),
                "left": base_left + int(base_w * 0.62),
                "width": int(base_w * 0.34),
                "height": int(base_h * 0.47)
            }

            try:
                img = np.array(self.sct.grab(monitor))
                gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

                # ⚡ 1차: 수평 밀도 프로젝션 검출 시도
                proj_zones = self.detect_button_zones_projection(gray, monitor["top"], options, base_top, base_h)
                if proj_zones:
                    # 🛡️ [스캔 중 이벤트 전환 감지] 이 스캔을 시작한 이후 current_event_options가
                    # 바뀌었다면(제목 재매칭 등으로), 지금 손에 든 결과는 이미 낡은 이벤트 기준이라
                    # 버린다 — 그대로 emit하면 방금 정상적으로 고쳐진 좌표를 도로 덮어써버린다.
                    if options is not (self.options_getter() or []):
                        print("🗑️ [스캔 결과 폐기] 스캔 도중 이벤트가 바뀌어 낡은 결과를 버림 (1차 프로젝션)")
                        self.has_scanned_current_event = False
                        self.msleep(50)
                        continue
                    self.has_scanned_current_event = True
                    print(f"📊 [1차 프로젝션 검출 성공] 총 {len(proj_zones)}개 선택지 위치 확정")
                    self.choices_scanned.emit(proj_zones)
                    self.msleep(300)
                    continue

                # ⚡ 2차: EasyOCR Bounding Box 1회 정밀 스캔 & 복원
                allow_chars = set()
                for opt in options:
                    allow_chars.update(opt.get('choice', ''))
                allowlist = ''.join(sorted(allow_chars)) if allow_chars else None

                if allowlist:
                    detections = self.reader.readtext(gray, detail=1, decoder='greedy', allowlist=allowlist)
                else:
                    detections = self.reader.readtext(gray, detail=1, decoder='greedy')

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
                btn_half = 34  # 실측 버튼 두께 68px의 절반 = 34px
                used_option_ids = set()  # 🛡️ 이미 다른 줄에 배정된 옵션은 후보에서 제외 (중복 배정 방지)

                # 🛡️ [위치 기반 오인식 검증용] 1차 비율 추정과 동일한 공식으로 각 선택지의 "있어야 할" 대략적 Y좌표 계산
                bottom_anchor_y = base_top + int(base_h * 0.724)
                est_pitch_px = int(base_h * 0.07037)
                count_opts = len(options)
                expected_cy_by_option_id = {}
                for idx, opt in enumerate(options):
                    expected_cy_by_option_id[id(opt)] = bottom_anchor_y - (count_opts - 1 - idx) * est_pitch_px
                y_sanity_tolerance = max(12, int(base_h * 0.015))  # 선택지 개수와 무관하게 실측 편차는 항상 7~8px 수준이므로, 그보다 살짝만 여유를 둔 값. 이 이상 벌어지면 오인식으로 간주하고 버림

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
                            # 텍스트는 비슷하게 매칭됐지만 실제 선택지 버튼이 있을 만한 위치에서
                            # 너무 벗어나 있음 → 다른 UI 텍스트를 잘못 읽은 오인식으로 판단하고 버린다.
                            matched_opt = None
                    if matched_opt:
                        used_option_ids.add(id(matched_opt))
                        zones.append({
                            'y_min': abs_y_min,
                            'y_max': abs_y_max,
                            'option': matched_opt,
                            'text': matched_opt.get('choice', combined_text)
                        })

                # 🛠️ [선택지 일부 누락 자동 복원] OCR이 일부 선택지를 빼먹어도, 인식된 선택지의 Y좌표와 76px 피치 간격으로 100% 완전 복원!
                if len(zones) > 0 and len(zones) < len(options):
                    known_zone = zones[0]
                    known_idx = options.index(known_zone['option']) if known_zone['option'] in options else 0
                    known_center_y = (known_zone['y_min'] + known_zone['y_max']) / 2.0
                    
                    pitch_px = int(base_h * 0.07037)  # 인게임 버튼 피치 간격 (76px)
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
                    # 🛡️ [스캔 중 이벤트 전환 감지] 위와 동일한 이유로, 2차(EasyOCR) 결과도
                    # 스캔 시작 이후 이벤트가 바뀌었으면 낡은 결과이니 버린다.
                    if options is not (self.options_getter() or []):
                        print("🗑️ [스캔 결과 폐기] 스캔 도중 이벤트가 바뀌어 낡은 결과를 버림 (2차 OCR)")
                        self.has_scanned_current_event = False
                        self.msleep(50)
                        continue
                    self.has_scanned_current_event = True
                    print(f"📝 [2차 OCR 텍스트 매칭 성공] 총 {len(zones)}개 선택지 위치 확정")
                    self.choices_scanned.emit(zones)
                    self.msleep(300)
                    continue

            except Exception:
                pass

            self.msleep(300)


# ----------------------------------------------------
# 5. 아르카나 카드 65종 인덱서
# ----------------------------------------------------
def card_name_to_img_path(name):
    return f"/images/cards/{name.replace(' ', '')}.webp"

# ----------------------------------------------------
# 5-1. JSON 데이터 기반 카드/이벤트 통합 DB
# ----------------------------------------------------
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
    if t == "RT_JOURNEY_ITEM":
        item = items_map.get(entry.get("reward_id"))
        name = item['name'].get('ko-KR', '?') if item else '?'
        val = _fmt_reward_value(entry)
        return f"{name}" + (f" {val}" if val else "")
    if t == "RT_SE_POTEN":
        poten = potentials_map.get(entry.get("reward_id"))
        name = poten['name'].get('ko-KR', '?') if poten else '?'
        mn, mx = entry.get("min"), entry.get("max")
        # 🎫 실제 확인 결과, RT_SE_POTEN의 min 값은 "잠재력 습득 비용 할인율"의 배수였다.
        # min:1 → 10% 할인 (실측 확인됨). min:2 이상은 min×10%로 추정 (미확인, 추후 검증 필요).
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
        # 🔀 같은 그룹 안의 여러 항목은 서로 "랜덤 택1" 대안 관계 → 한 줄에 or로 이어붙여
        # 하나의 불릿으로 표시한다 (별도 줄로 쪼개면 smart_format_reward가 완전히 다른
        # 블록으로 갈라버려서, 뒤따르는 확정 보상이 어느 쪽에 속하는지 애매해진다).
        # 서로 다른 그룹은 전부 같이 주어지는 확정 보상이므로 각자 별도 줄로 이어붙인다.
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
        # 🎒 아이템 소모 조건: target/reward_id/item_id 등으로 어떤 아이템인지 참조하는 경우가
        # 있어 폭넓게 탐색. target은 문자열("105036")로 오는 경우가 있어 int 변환도 같이 시도.
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
    # 🈳 성공/실패 보상이 둘 다 없으면 원래 게임처럼 "없음"을 명시적으로 표시한다.
    # (그냥 빈 문자열로 두면 smart_format_reward가 아무 텍스트도 안 만들어서 화면에 통째로 안 뜬다)
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
        # 🔓 cond_text는 화면 표시용 reward 문자열에 이미 녹아 들어갔지만, 클릭 시점에
        # "이 선택지가 조건부(잠길 수 있는) 선택지인지"를 코드에서 판단하려면 별도의
        # bool 값이 필요하다. reward 문자열을 다시 파싱하지 않도록 여기서 같이 저장한다.
        options.append({"choice": choice_text, "reward": "\n".join(reward_lines), "has_condition": bool(cond_text)})

    return {"rawTitle": raw_title, "cleanTitle": clean_title, "options": options, "isArcana": is_arcana}

def parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map=None):
    # 🎚️ [난이도별 변형 통합] journeys.json은 같은 이벤트가 이지/노말/하드마다
    # 완전히 별개인 객체(다른 id, 다른 condition 값)로 나뉘어 있다. 이를 하나로 합쳐
    # 선택지별로 "난이도 배지 + 조건 + 보상"을 or로 구분된 블록들로 묶는다.
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

            cond_text = translate_condition_entry(choice.get('condition'), items_map)
            if cond_text:
                any_condition = True

            try:
                success_lines = translate_reward_groups(choice.get('success_rewards'), journey_buffs_map, potentials_map, items_map)
                fail_lines = translate_reward_groups(choice.get('failure_rewards'), journey_buffs_map, potentials_map, items_map)
            except Exception:
                success_lines, fail_lines = [], []

            is_trivial = not cond_text and not success_lines and not fail_lines  # 조건도 없고 보상도 없는 완전히 빈 결과
            content_lines = ([cond_text] if cond_text else []) + _build_choice_reward_lines(success_lines, fail_lines)

            if diff_label or content_lines:
                diff_entries.append({'label': diff_label, 'content': content_lines, 'trivial': is_trivial})

        # 🧹 [완전히 빈 결과 중복 제거] 모든 난이도에서 조건도 보상도 없이 "없음"뿐이면
        # 난이도 배지/or로 3번 반복할 필요 없이 깔끔하게 한 번만 보여준다.
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

def build_caches_from_all_json(arcanas_data, journeys_data, journey_buffs_map, potentials_map, items_map=None):
    cards_for_indexing = []
    db_event_cache = []
    card_event_history = {}
    resolved_card_events = {}
    card_id_map = {}
    title_to_cards = {}  # 🃏 cleanTitle -> 그 제목을 가진 카드 이름들의 집합 (동명 이벤트 충돌 감지용)
    title_to_events = {}  # 🔍 [진단용] cleanTitle -> 그 제목의 파싱된 이벤트(옵션/보상 포함) 목록

    # 1. 아르카나 카드 전용 이벤트 파싱
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

    # 🃏 [동명 이벤트 카드 목록] 같은 제목을 여러 카드가 공유하는 경우 → 제목만으로는 어느 카드인지
    # 구분이 불가능하므로, 이런 제목은 카드 그림 인식이 확인해주기 전까지 "강한 확정"을 걸지 않는다.
    ambiguous_arcana_titles = {t for t, cards in title_to_cards.items() if len(cards) > 1}

    # 🔍 [진단용] "동명 이벤트"라고 해서 다 카드마다 보상이 다른 건 아니다(제목만 우연히 같고
    # 선택지/보상까지 완전히 같을 수도 있음 — 이 경우는 카드가 뭐든 상관없어서 사진 강제확인이
    # 사실상 무의미하다). 카드별 실제 보상 내용(choice+reward 조합)이 서로 다른 제목만
    # "진짜 이격"으로 따로 집계해서 알려준다.
    # 📌 선택지가 1개뿐인 이벤트(그냥 지나가는 안내성 이벤트)는 애초에 "고를 게" 없어서
    # 카드 구분 자체가 의미 없으므로 이 비교에서 제외하고, 선택지가 2개 이상인 이벤트만 본다.
    def _event_signature(ev):
        return tuple(sorted((o.get('choice', ''), o.get('reward', '')) for o in ev.get('options', [])))

    reward_diff_titles = set()
    for t in ambiguous_arcana_titles:
        evs = [e for e in title_to_events.get(t, []) if len(e.get('options', [])) >= 2]
        sigs = {_event_signature(ev) for ev in evs}
        if len(sigs) > 1:
            reward_diff_titles.add(t)

    if ambiguous_arcana_titles:
        print(f"🃏 [동명 이벤트 감지] {len(ambiguous_arcana_titles)}개 제목이 2개 이상의 카드에서 공유됨 → 카드 인식 확인 전까지 약한 매칭으로만 처리")
        same_reward_count = len(ambiguous_arcana_titles) - len(reward_diff_titles)
        print(f"   └ 🔀 [진짜 이격] {len(reward_diff_titles)}개는 카드마다 보상 내용이 실제로 다름 (사진 확인 꼭 필요)")
        print(f"   └ 🟰 [보상 동일] {same_reward_count}개는 제목만 겹치고 보상까지 동일함 (카드 구분 실익 적음)")
        if reward_diff_titles:
            sample_names = []
            for t in list(reward_diff_titles)[:8]:
                evs = title_to_events.get(t, [])
                if evs:
                    sample_names.append(evs[0].get('rawTitle', t))
            print(f"   └ 🔀 [진짜 이격 예시] {sample_names}")



    # 2. ⚡ [핵심 통합] 일반 여정 이벤트(journeys.json - 이벤트이름:난이도별변형리스트 딕셔너리) 완벽 파싱!
    journey_count = 0
    if isinstance(journeys_data, dict):
        for event_name, variants in journeys_data.items():
            try:
                if isinstance(variants, dict):
                    variants = [variants]
                if not isinstance(variants, list):
                    continue
                parsed = parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map)
                if parsed:
                    db_event_cache.append(parsed)
                    journey_count += 1
            except Exception:
                continue
    elif isinstance(journeys_data, list):
        # 폴백: 혹시 리스트 구조로 오는 경우, 같은 이름끼리 다시 묶어서 처리
        grouped = {}
        for ev in journeys_data:
            if not isinstance(ev, dict):
                continue
            name_field = ev.get('name') or {}
            title = name_field.get('ko-KR', '') if isinstance(name_field, dict) else str(name_field or '')
            grouped.setdefault(title, []).append(ev)
        for event_name, variants in grouped.items():
            try:
                parsed = parse_event_variants_json(event_name, variants, journey_buffs_map, potentials_map, items_map)
                if parsed:
                    db_event_cache.append(parsed)
                    journey_count += 1
            except Exception:
                continue

    print(f"📦 [통합 DB 구성 완료] 아르카나 카드 {len(cards_for_indexing)}종, 일반 여정 이벤트 {journey_count}개 100% 완전 탑재!")

    return {
        'cards': cards_for_indexing,
        'db_event_cache': db_event_cache,
        'card_event_history': card_event_history,
        'resolved_card_events': resolved_card_events,
        'card_id_map': card_id_map,
        'ambiguous_arcana_titles': ambiguous_arcana_titles,
        # 🃏 [카드 검색 범위 축소용] cleanTitle -> 그 제목을 가진 카드 이름 집합. "이격 아르카나"처럼
        # 강제 사진 확인 대상인 이벤트는, 이 매핑으로 후보 카드를 좁혀서 인식 정확도를 높인다.
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

            # ⚡ 일반 여정 이벤트 전체 JSON 불러오기
            journeys = []
            for ep in ["journeys.json", "journey.json"]:
                try:
                    res = requests.get(f"{JSON_BASE_URL}/{ep}", headers=headers, timeout=8)
                    if res.status_code == 200:
                        journeys = res.json()
                        print(f"📦 [여정 DB 로드] '{ep}' 백그라운드 다운로드 완료!")
                        break
                except Exception:
                    pass

            # 🎒 [RT_JOURNEY_ITEM 이름 매칭용] 여정 아이템 JSON 불러오기
            items = requests.get(f"{JSON_BASE_URL}/journey_items.json", headers=headers, timeout=15).json()

        except Exception as e:
            print(f"⚠️ [JSON 데이터 로드 실패] {e}")
            self.finished_signal.emit({'error': str(e)})
            return

        journey_buffs_map = {b['id']: b for b in (journey_buffs or [])}
        potentials_map = {p['id']: p for p in (potentials or [])}
        items_map = {i['id']: i for i in (items or []) if isinstance(i, dict) and 'id' in i}
        result = build_caches_from_all_json(arcanas, journeys, journey_buffs_map, potentials_map, items_map)
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
                    print(f"[아르카나 특징 경고] '{name}' 이미지 다운로드 실패 (HTTP {img_resp.status_code}, url={img_url})")
                    return None

                pil_raw = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                cv_bgr = cv2.cvtColor(np.array(pil_raw), cv2.COLOR_RGB2BGR)

                p_hash, hsv_hist, _ = extract_card_features(cv_bgr, apply_deskew=False)
                return {'hash': p_hash, 'hist': hsv_hist, 'id': name, 'name': name}
            except Exception as e:
                print(f"[아르카나 특징 경고] '{name}' 생성 실패: {e}")
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
        desktop = QApplication.desktop()
        self.screen_w = desktop.screenGeometry(0).width()
        self.screen_h = desktop.screenGeometry(0).height()

        _gr = get_game_window_rect()
        if _gr:
            print(f"🪟 [게임 창 감지 성공] 위치=({_gr['left']},{_gr['top']}) 크기={_gr['width']}x{_gr['height']} → 이 창 기준으로 캡처 영역 계산")
        else:
            print(f"🪟 [게임 창 감지 실패] 전체 화면({self.screen_w}x{self.screen_h}) 기준으로 폴백 (게임이 아직 안 켜졌으면 나중에 자동으로 잡힘)")

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
        # 🃏 [카드 검색 범위 축소] cleanTitle -> 그 제목을 가진 카드 이름 집합. JSON 로드가 끝나면
        # on_json_data_loaded에서 실제 값으로 채워진다.
        self.title_to_cards = {}

        self.pending_card_search = None
        self._search_verify_retried = False
        self.pending_title_search = None
        self._dedupe_retry_done = False
        self._card_symbol_retry_done = False

        self.app_started_at = time.time()
        self.STARTUP_GRACE_SECONDS = 12

        # 🎯 [카드 영역 사용자 지정 값 적용] 저장된 오버라이드가 있으면 캡처 스레드들이
        # 시작되기 전에 미리 반영해서, 첫 캡처부터 바로 사용자가 지정한 영역을 쓰게 한다.
        global _card_region_manual_override
        _saved_region_settings = load_settings()
        _region_override = _saved_region_settings.get("card_region_override")
        if _region_override:
            REGION_FRACTIONS["card_art"] = _region_override
            _card_region_manual_override = True
            print(f"🎯 [카드 영역] 저장된 사용자 지정 영역 적용: {_region_override}")

        self.status_overlay = JourneyStatusOverlay()
        self.status_overlay.on_settings_requested = self.open_settings_dialog
        self.status_overlay.on_reset_requested = self.reset_tracking_state
        self.tooltip = FixedLeftTooltip()

        # 🔤 [왼쪽 툴팁 글자 크기] 저장된 배율을 바로 적용
        self.tooltip_font_scale = _saved_region_settings.get("tooltip_font_scale", 1.0)
        self.tooltip.set_font_scale(self.tooltip_font_scale)

        # 🖌️ [즉시 렌더링 강제] EasyOCR 모델 로딩처럼 무거운 초기화가 곧바로 이어지면
        # Qt 이벤트 루프가 아직 안 돌고 있어서 방금 만든 배지가 화면에 실제로 그려질 틈이
        # 없다. QApplication.processEvents()는 백그라운드 스레드의 신호(json_loader 완료 등)
        # 까지 앞당겨 처리해버려서 아직 안 만들어진 속성을 참조하다 에러가 날 수 있으므로,
        # 대신 이 위젯 하나만 강제로 즉시 다시 그리는 repaint()를 쓴다.
        self.status_overlay.repaint()

        self._region_debug_timer = QTimer()
        self._region_debug_timer.timeout.connect(self.status_overlay.reposition)
        self._region_debug_timer.timeout.connect(self.tooltip.reposition)
        self._region_debug_timer.timeout.connect(self._refresh_debug_overlays)
        self._region_debug_timer.start(500)

        # ⚡ Y좌표 저장소, 소요시간 측정을 위한 타임스탬프 & 클릭 차단 타이머
        self.active_choice_zones = []
        self.click_block_until = 0
        self._event_matched_timestamp = 0.0

        self.json_loader = JsonDataLoadThread()
        self.json_loader.finished_signal.connect(self.on_json_data_loaded)
        self.json_loader.start()

        self.status_overlay.set_custom_status("🟡 [OCR 엔진 준비 중] 처음 실행 시 다소 시간이 걸릴 수 있어요...", "#ffcc00", "#ffcc00")
        self.status_overlay.repaint()  # EasyOCR 모델 로딩(다음 줄) 전에 이 문구가 먼저 보이도록

        self.event_ocr = EventTitleOCRThread(self.screen_w, self.screen_h, pause_getter=lambda: self.is_searching or getattr(self.choice_scanner, 'is_capturing', False))
        self.event_ocr.event_detected.connect(self.update_event_title)
        self.event_ocr.journey_status_changed.connect(self.update_journey_status)

        self.is_arcana_event_screen = True
        self.event_ocr.arcana_label_detected.connect(self._on_arcana_label_changed)

        # 선택지 사전 스캔 스레드 (여기도 자체 EasyOCR 모델을 로딩하니 그 전에 한 번 더 그려준다)
        self.status_overlay.repaint()
        self.choice_scanner = ChoiceScannerThread(self.screen_w, pause_getter=lambda: self.is_searching, options_getter=lambda: self.current_event_options)
        self.choice_scanner.choices_scanned.connect(self.on_choices_scanned)

        self.card_art_ocr = EventCardArtThread(self.screen_w, self.screen_h, lambda: self._card_search_pool(),
                                                pause_getter=lambda: self.is_searching or not self.is_arcana_event_screen,
                                                lenient_getter=lambda: self._is_force_confirm_active())
        self.card_art_ocr.start()
        self.card_art_ocr.card_matched.connect(self.on_card_art_matched)

        # 🔧 [디버그 오버레이] 카드 인식 미리보기 / 캡처 영역 박스. 기본은 꺼져있고,
        # 설정창에서 켜면 그때 만들어서 보여준다 (처음부터 만들면 괜히 리소스만 씀).
        self.card_preview_overlay = None
        self.region_debug_overlay = None
        self.card_art_ocr.raw_preview_signal.connect(self._on_card_raw_preview)
        self.card_art_ocr.preview_signal.connect(self._on_card_warped_preview)
        _saved_settings = load_settings()
        if _saved_settings.get("show_card_preview"):
            self.set_card_preview_enabled(True)
        if _saved_settings.get("show_region_debug"):
            self.set_region_debug_enabled(True)

        # ⚡ [즉시 반응 툴팁 타이머] 30ms 마다 마우스 위치 & 선택지 고름(클릭/숫자키) 감지
        self.instant_hover_timer = QTimer()
        self.instant_hover_timer.timeout.connect(self.check_instant_hover)
        self.instant_hover_timer.start(30)

        # 🌕 [전역 로딩 화면 감시] 클릭 여부와 무관하게, 어떤 경로로든 로딩 화면(N.O.A 로고)이
        # 뜨면 그 즉시 이전 이벤트의 모든 상태(선택지 zone, 좌측 툴팁, 매칭 정보 등)를 초기화한다.
        # 로비 복귀, 자동 진행 등 클릭 감지로 못 잡는 화면 전환도 전부 여기서 처리된다.
        self._is_loading_global = False
        self._loading_pending_count = 0  # 🛡️ 찰나의 밝은 플래시 오탐 방지용 연속 확인 카운터
        self._loading_end_pending_count = 0  # 🛡️ 2단계 로딩 중간의 짧은 틈 오탐 방지용 연속 확인 카운터
        self._loading_sct = mss.mss()  # 매번 새로 만들지 않고 재사용 (초당 2회 폴링이라 재사용이 더 가볍다)
        self._global_loading_timer = QTimer()
        self._global_loading_timer.timeout.connect(self._poll_global_loading_state)
        self._global_loading_timer.start(150)  # ⚡ 짧게 스쳐가는 로딩 화면도 놓치지 않도록 500ms → 150ms

        # 🖼️ [선택지 클릭 후 화면 전환 시각 감지] "새 이벤트 제목이 떴는지" / "로딩 화면이 떴는지"에만
        # 의존하면, 이벤트가 아닌 서브 화면(명상실 등)으로 넘어가는 더 흔한 경우를 못 잡아서
        # 툴팁이 15초 타임아웃까지 남아있게 된다. 대신 클릭한 선택지 버튼 영역 자체를 스크린샷으로
        # 찍어두고, 그 자리가 시각적으로 달라졌는지를 직접 비교해서 이벤트/비이벤트 화면 구분 없이
        # "선택이 실제로 반영됐는지"를 빠르게(약 0.2~0.3초) 판단한다.
        self._transition_sct = mss.mss()
        self._transition_baseline_gray = None
        self._transition_pending_confirm = 0
        self._transition_check_timer = QTimer()
        self._transition_check_timer.timeout.connect(self._poll_click_transition)

        # 🖥️ [시스템 트레이 아이콘] 창이 따로 없는 오버레이 프로그램이라 종료할 방법이 마땅치
        # 않았는데, 트레이 아이콘 우클릭 메뉴로 깔끔하게 종료할 수 있게 한다.
        self.tray_icon = QSystemTrayIcon(_create_tray_icon(), self)
        self.tray_icon.setToolTip("JourneyHelper 실행 중 (우클릭하여 종료)")
        tray_menu = QMenu()
        quit_action = QAction("종료", tray_menu)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        # 왼쪽 더블클릭으로도 종료 메뉴가 바로 뜨게 (오른쪽 클릭 메뉴를 못 찾는 사용자 배려)
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
        )
        dlg.exec_()

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
            # "기본값으로" → 내장된 정적 기본값에 고정한다. 자동 보정을 다시 켜면 몇 초 뒤
            # 자동 계산이 다시 돌면서 카드를 벗어난 위치로 덮어써버릴 수 있으므로, 여기서도
            # "사용자 지정 값"과 동일하게 취급해서 자동 보정이 더는 못 건드리게 고정한다.
            settings["card_region_override"] = dict(_DEFAULT_CARD_ART_FRACTION)
            save_settings(settings)
            REGION_FRACTIONS["card_art"] = dict(_DEFAULT_CARD_ART_FRACTION)
            _card_region_manual_override = True
            print("↩️ [카드 영역] 기본값으로 되돌리고 고정 (자동 보정이 더 이상 건드리지 않음)")
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

    def reset_tracking_state(self):
        # 🔄 [수동 새로고침] 상단 배지를 클릭(접기/펼치기)할 때마다, 인식이 꼬였거나 막힌 것
        # 같을 때 사용자가 직접 초기화할 수 있도록 지금까지 추적 중이던 이벤트/카드 정보를
        # 전부 지우고 다시 찾기 시작한다. 로딩 화면 감지 시 정리하는 로직과 동일하게 처리.
        print("🔄 [수동 새로고침] 상단 배지 클릭 → 추적 중이던 이벤트/카드 정보 초기화")
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

    def _poll_global_loading_state(self):
        if not is_game_hovered_or_active():
            return
        game_rect = get_game_window_rect()
        loading_now = is_loading_screen_visible(game_rect, self.screen_w, self.screen_h, sct=self._loading_sct)

        if not loading_now:
            self._loading_pending_count = 0
        elif not self._is_loading_global:
            # 🛡️ [플래시 오탐 방지] 잠긴 선택지 클릭 시의 짧은 흰색 플래시 등, 찰나의 밝은 화면과
            # 실제 로딩 화면(최소 1초 이상 지속)을 구분하기 위해 연속 2번(0.5초 간격) 확인되어야
            # "진짜 로딩"으로 인정한다.
            self._loading_pending_count += 1
            if self._loading_pending_count < 2:
                return

        if loading_now:
            self._loading_end_pending_count = 0
        elif self._is_loading_global:
            # 🛡️ [2단계 로딩 대응] 로딩이 두 단계로 나뉘어 있어서 중간에 아주 짧게 밝기가
            # 떨어지는 게임도 있다. 이 한 프레임만 보고 "로딩 종료"로 확정해버리면, 아직
            # 안정되지 않은 중간 전환 프레임(글자가 깨져 보이는 등)을 그대로 읽어서 엉뚱하게
            # 이전 이벤트로 재매칭하는 문제가 있었다. 시작 판정과 마찬가지로 연속 2번
            # (0.3초 안팎) 확인되어야 "진짜 종료"로 인정한다.
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
            self.current_matched_card_id = None  # 🃏 로딩 화면 = 확실히 새 화면으로 전환됨 → 이전 카드 정보는 버림
            self._card_symbol_retry_done = False
            if hasattr(self, 'card_art_ocr'):
                self.card_art_ocr.last_matched_id = ""
                self.card_art_ocr.pending_id = None
                self.card_art_ocr.pending_count = 0
            self.tooltip.hide_and_clear()
            self.click_block_until = 0
            if hasattr(self, 'choice_scanner'):
                self.choice_scanner.reset_cache()
            # ⚠️ [근본 원인 수정] event_ocr의 잠금 해제(is_waiting_for_next_event 등)는 여기서
            # 하지 않는다. 로딩이 "시작"된 시점일 뿐 아직 진행 중이라, 전환 애니메이션의 잔상
            # 프레임이 화면에 남아있을 수 있다. 여기서 잠금을 풀면 그 잔상(예: 방금 완료한 이전
            # 이벤트 제목)을 OCR이 "새로 감지된 이벤트"로 착각해서 재확정해버리는 문제가 있었다
            # (예: '여가 시간' 선택 후 실제로는 '훈련 의뢰' 화면인데 '여가 시간'이 다시 뜸).
            # 잠금 해제는 로딩이 완전히 "끝난" 시점(아래 elif 분기)으로 옮긴다.
            if hasattr(self, '_lock_verify_timer'):
                self._lock_verify_timer.stop()
            self.status_overlay.set_custom_status("🌕 [로딩 중] 다음 화면으로 이동하는 중...", "#ffcc00", "#ffcc00")

        elif not loading_now and self._is_loading_global:
            self._is_loading_global = False
            print("✅ [전역 로딩 화면 종료 감지] 다음 이벤트 탐색 대기 상태로 전환")
            # ⚠️ [근본 원인 수정] OCR 잠금 해제를 로딩이 진짜로 끝난 이 시점으로 옮겼다.
            # 로딩 화면이 완전히 사라져 다음 화면이 안정된 뒤에야 "이전 제목 잔상 무시"
            # 보호를 풀어서, 전환 도중의 잔상 프레임이 새 이벤트로 오인되는 것을 막는다.
            if hasattr(self, 'event_ocr'):
                self.event_ocr.is_waiting_for_next_event = False
                self.event_ocr.completed_event_title = ""
                self.event_ocr.pending_event = ""
                self.event_ocr.last_event = ""
                self.event_ocr._last_title_gray = None
            self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")

    # ⚡ [0.00초 즉시 등록] 실측 게임 화면 기반 1~5개 선택지 Y좌표 즉시 비율 추정 배치 (버튼두께 68px + 유격 8px 반영)
    def register_instant_estimated_zones(self):
        # 🛡️ [단순 보상형 이벤트 필터] 실제 분기 선택지가 없는(옵션 1개 이하) 이벤트는
        # 화면에 호버 가능한 선택 버튼이 존재하지 않으므로 zone을 등록하지 않는다.
        # (JSON 매칭 이후, 선택지 없이 보상만 자동 지급되는 이벤트까지 선택지로 오인되는 것을 방지)
        if not self.current_event_options or len(self.current_event_options) < 2:
            self.active_choice_zones = []
            return

        game_rect = get_game_window_rect()
        if game_rect:
            base_top, base_h = game_rect["top"], game_rect["height"]
        else:
            base_top, base_h = 0, 1080

        count = len(self.current_event_options)
        
        # 🎯 인게임 버튼 피치(76px) 및 유격 8px 적용 정밀 공식
        bottom_anchor_y = base_top + int(base_h * 0.724)
        pitch_px = int(base_h * 0.07037)     # 버튼 피치 간격 약 76px
        btn_height_px = int(base_h * 0.063)   # 버튼 두께 약 68px (유격 8px 보존)

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
            for i, z in enumerate(estimated_zones):
                print(f"   └ [선택지 {i+1}] \"{z['text']}\" ➔ Y범위: {z['y_min']}px ~ {z['y_max']}px (두께:{z['y_max']-z['y_min']}px)")

    def on_choices_scanned(self, zones):
        if zones:
            self.active_choice_zones = zones
            if self._event_matched_timestamp > 0:
                elapsed_ms = (time.time() - self._event_matched_timestamp) * 1000
                print(f"🎯 [실제 화면 글자 Y좌표 정밀 보정 완료] 총 {len(zones)}개 선택지 (소요시간: {elapsed_ms:.1f}ms)")
                for i, z in enumerate(zones):
                    print(f"   └ [선택지 {i+1}] \"{z['text']}\" ➔ 보정 Y범위: {z['y_min']}px ~ {z['y_max']}px (두께:{z['y_max']-z['y_min']}px)")

    # ⚡ [선택지 선택 액션] 클릭 또는 숫자키(1~4)로 선택지를 골랐을 때 호출
    # option: 실제로 클릭/선택된 선택지 데이터(dict). 넘겨주면 'condition' 필드 유무로
    # "이 선택지가 애초에 잠길 수 있는 조건부 선택지인지"를 판단해 툴팁 처리 방식을 나눈다.
    #   - condition이 없는 선택지 = 항상 성공하는 선택지 → 클릭 즉시 툴팁을 지워도 안전
    #   - condition이 있는 선택지 = 조건 충족 여부를 스크립트가 알 수 없음(잠겨 있을 수도 있음)
    #     → 기존처럼 로딩 화면 확인/타임아웃 전까지 툴팁을 유지
    def on_choice_selected_action(self, option=None):
        # 🖼️ [화면 전환 감지용 baseline 캡처] active_choice_zones가 아래에서 지워지기 전에,
        # 지금 화면(클릭 직전 상태)을 먼저 찍어둔다. 이후 _poll_click_transition이 주기적으로
        # 같은 영역을 다시 찍어서 비교한다.
        self._transition_baseline_gray = self._capture_choice_panel_gray(get_game_window_rect())
        self._transition_pending_confirm = 0
        self._transition_check_timer.start(120)

        # 필드가 아예 없는 경우(예상 못한 데이터 구조)엔 안전하게 "조건이 있을 수도 있다"고
        # 보수적으로 판단해서 기존처럼 로딩 화면 확인까지 기다린다.
        has_condition = bool(option.get('has_condition', True)) if option else True
        self._last_choice_has_condition = has_condition

        # 🟢 조건 없는(항상 성공하는) 선택지는 화면 전환을 기다릴 필요 없이 즉시 정리한다.
        if not has_condition:
            print("🟢 [조건 없는 선택지 클릭] 항상 성공하는 선택지로 판단 → 툴팁 즉시 숨김")
            self.tooltip.hide_and_clear()
            self.active_choice_zones = []
            # 🛡️ [스캔 스태일 감지용] ChoiceScannerThread는 "options is not self.options_getter()"로
            # 스캔 도중 이벤트가 바뀌었는지 식별한다(리스트 객체의 identity 비교). 여기서
            # current_event_options를 새 리스트(빈 리스트)로 바꿔줘야, 클릭 직전에 이미 시작돼
            # 있던 낡은 스캔이 뒤늦게 결과를 던졌을 때 스태일로 인식되어 버려진다. 이걸 안 해주면
            # 방금 지운 active_choice_zones가 몇 초 뒤 낡은 스캔 결과로 도로 채워질 수 있다.
            self.current_event_options = []
            self._transition_check_timer.stop()  # 이미 정리했으니 화면 전환 감지는 더 필요 없음

        # 🔒 방금 완료한 이벤트 제목 저장 및 락 설정 (후속 대화문 진행 동안 이전 이벤트 재로딩 완전 방지!)
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
        self.current_matched_card_id = None  # 🃏 클릭 직후엔 실제로 화면이 넘어갈지 아직 모르므로 카드 정보도 리셋 (다음 화면에서 새로 인식)
        self._card_symbol_retry_done = False
        if hasattr(self, 'card_art_ocr'):
            self.card_art_ocr.last_matched_id = ""
            self.card_art_ocr.pending_id = None
            self.card_art_ocr.pending_count = 0
        # 📌 좌측 툴팁/선택지 zone은 여기서 지우지 않는다. 실제로 화면이 넘어갔는지(로딩 화면) 아직
        # 확인 전이므로, 잠긴 선택지였을 경우를 대비해 방금 보던 내용을 그대로 유지한다.
        # 실제 로딩이 확인되는 순간(_poll_global_loading_state)에만 지운다.

        # 🗣️ 클릭했다고 해서 화면이 실제로 넘어갔는지는 아직 모르는 상태(잠긴 선택지일 수도 있음)이므로,
        # "전환 중"이라고 단정 짓지 않고 확인 중이라는 중립적인 문구로 표시한다.
        self.status_overlay.set_custom_status("🔵 [선택 반응 확인 중] 화면 전환 여부 확인 중...", "#00aaff", "#00aaff")

        if hasattr(self, 'choice_scanner'):
            self.choice_scanner.reset_cache()

        print(f"🖱️ [선택지 선택 감지] 완료 이벤트 '{getattr(self.event_ocr, 'completed_event_title', '')}' 락 등록 (툴팁은 로딩 확인 전까지 유지)")

        # 🔓 [잠긴 선택지 자동 복구 예약] 전역 로딩 감시기가 로딩 화면을 감지하면 상태 정리는
        # 알아서 해주므로, 여기서는 "로딩 화면이 끝내 한 번도 안 뜨면 잠긴 선택지였다"는
        # 최종 타임아웃 폴백만 담당한다.
        self._lock_check_started_at = time.time()
        if not hasattr(self, '_lock_verify_timer'):
            self._lock_verify_timer = QTimer()
            self._lock_verify_timer.timeout.connect(self._verify_choice_actually_advanced)
        self._lock_verify_timer.start(1000)

    # 🛑 이 시간 동안 로딩 화면이 한 번도 안 뜨면 잠긴 선택지로 판단하고 반복 검사를 그만둔다
    # (예전엔 15초였지만, 클릭 후 화면 전환을 픽셀 단위로 직접 감지하는 _poll_click_transition이
    #  대부분의 진짜 전환/느린 연출을 이미 몇 초 안에 잡아주므로, 여긴 "정말 아무 일도 안 일어난
    #  잠긴 선택지"에만 걸리는 순수 최후 안전장치 역할로 축소해도 안전하다.)
    LOCK_RECOVERY_MAX_WAIT_SEC = 8.0

    def _verify_choice_actually_advanced(self):
        if not hasattr(self, 'event_ocr') or not self.event_ocr.is_waiting_for_next_event:
            # 이미 정상적으로 다음 화면/이벤트로 넘어갔거나(전역 로딩 감시기가 처리),
            # 다음 이벤트가 매칭됨 → 더 볼 필요 없음
            if hasattr(self, '_lock_verify_timer'):
                self._lock_verify_timer.stop()
            return

        elapsed = time.time() - getattr(self, '_lock_check_started_at', time.time())
        if elapsed >= self.LOCK_RECOVERY_MAX_WAIT_SEC:
            # 로딩 화면이 한 번도 안 뜬 채로 충분히 오래 지남 → 잠긴 선택지를 눌러 무시된 것으로 판단
            print(f"🔓 [잠긴 선택지 자동 복구] {elapsed:.1f}초간 로딩 화면이 감지되지 않음 → 선택 무효로 판단, 락 해제")
            self.event_ocr.is_waiting_for_next_event = False
            self.event_ocr.completed_event_title = ""
            self.event_ocr.pending_event = ""
            self.event_ocr.last_event = ""
            self.event_ocr._last_title_gray = None
            self.click_block_until = 0
            self._lock_verify_timer.stop()
            # 🧹 [툴팁 잔류 방지] 로딩 화면이 감지되지 않는 전환(예: 명상실 등 서브 화면)의 경우
            # 여기가 유일하게 정리를 담당하는 지점이므로, 로딩 감지 분기와 동일하게
            # 선택지 zone과 툴팁도 함께 지운다. 안 지우면 좌측 툴팁 박스가 화면에 그대로 남는다.
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
            self.tooltip.hide_and_clear()
            if hasattr(self, 'choice_scanner'):
                self.choice_scanner.reset_cache()
            self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")
        # else: 아직 최대 대기시간 안 지났으니 다음 주기(1초 뒤)에 다시 확인
        # (그 사이에 전역 로딩 감시기가 로딩을 보면 is_waiting_for_next_event를 알아서 꺼주므로
        #  다음 호출에서 위쪽 가드에 걸려 자동으로 종료된다)

    # 🖼️ 현재 활성화된 선택지들이 차지하는 화면 영역(버튼들의 y범위 전체 + 고정 x범위)을
    # 작은 그레이스케일 이미지로 캡처. active_choice_zones가 비어있으면 None.
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

    # 🖼️ [화면 전환 시각 감지 폴링] 클릭 시점에 찍어둔 baseline과 현재 화면을 주기적으로 비교한다.
    # 이벤트 제목이 바뀌었는지/로딩 화면이 떴는지와 무관하게, "그 자리가 실질적으로 달라졌는지"만
    # 보기 때문에 이벤트가 아닌 서브 화면으로 넘어가는 경우도 똑같이 잡아낸다.
    def _poll_click_transition(self):
        if not hasattr(self, 'event_ocr') or not self.event_ocr.is_waiting_for_next_event:
            # 이미 다른 경로(로딩 감지, 새 제목 확정, 15초 타임아웃)로 정리가 끝남
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

        # 🛡️ 클릭 순간의 짧은 흰색 플래시 등 찰나의 변화와 실제 화면 전환을 구분하기 위해
        # 연속 2회(약 240ms 간격) 확인되어야 "진짜 바뀌었다"고 인정한다.
        self._transition_pending_confirm += 1
        if self._transition_pending_confirm < 2:
            return

        print(f"🖼️ [화면 전환 시각 감지] 선택지 영역 변화량 {diff:.1f} → 선택 확정으로 판단, 즉시 정리")
        self._transition_check_timer.stop()

        # ⚠️ [레이스 컨디션 방지] event_ocr.is_waiting_for_next_event와 _lock_verify_timer는
        # 여기서 건드리지 않는다.
        #   1) 버튼 영역(여기서 감지하는 곳)과 제목 영역(OCR 스레드가 읽는 곳)은 화면상 서로
        #      다른 부분이라, 버튼은 이미 바뀌었어도 제목 영역은 아직 "예전 제목"을 캐시해둔
        #      상태일 수 있다. 그 타이밍에 잠금을 풀면 OCR 스레드가 캐시된 예전 제목을 "새
        #      이벤트"로 착각해서 다시 던지고, 예전 선택지가 재등장하는 버그가 있었다.
        #   2) 다음 화면이 이벤트 제목 자체가 없는 서브 화면(명상실 등)이면 OCR 스레드가 끝내
        #      스스로 잠금을 풀 기회가 없다. _lock_verify_timer(8초 타임아웃)를 계속 살려둬야
        #      그 경우에도 결국 잠금이 풀리고 새로운 감지가 재개된다.
        # 여기서는 화면(툴팁/선택지 표시)만 정리하고, 잠금 해제 자체는 OCR 스레드의 자체 판단
        # 또는 8초 타임아웃에게 맡긴다.
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
        self.tooltip.hide_and_clear()
        if hasattr(self, 'choice_scanner'):
            self.choice_scanner.reset_cache()
        self.status_overlay.set_custom_status("⚪ [헬퍼 대기 중] 여정 이벤트 화면을 찾고 있습니다...", "#888888", "#aaaaaa")

    # 🛡️ 마우스 커서가 등록된 선택지 상자 정밀 영역 안쪽에 있으면 해당 zone(옵션 데이터 포함)을 반환.
    # 아무 zone에도 안 들어와 있으면 None.
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

    # 🛡️ 마우스 커서가 등록된 선택지 상자 정밀 영역 안쪽에 완전히 들어와 있는지 검사 (호환용 래퍼)
    def is_cursor_inside_choice_zone(self, pos, game_rect):
        return self.get_choice_zone_at(pos, game_rect) is not None

    def check_instant_hover(self):
        user32 = ctypes.windll.user32

        # 1. 🖱️ 마우스 좌클릭(0x01) 또는 숫자키 1,2,3,4 감지
        # (click_block_until 동안은 방금 처리한 클릭이 중복으로 다시 잡히는 것만 막는다.
        #  더 이상 이 시간 동안 툴팁을 강제로 숨기지는 않는다 — 로딩 화면이 실제로 확인되기
        #  전까지는 방금 보던 선택지 내용을 그대로 보여주는 게 목적이므로.)
        if is_game_hovered_or_active() and time.time() >= self.click_block_until:
            game_rect = get_game_window_rect()
            pos = QCursor.pos()

            # 마우스 좌클릭 감지: 선택지 상자 안쪽을 클릭했을 때만 인정 (빈 공간 클릭 방지!)
            if (user32.GetAsyncKeyState(0x01) & 0x8000) != 0:
                clicked_zone = self.get_choice_zone_at(pos, game_rect)
                if clicked_zone:
                    self.on_choice_selected_action(clicked_zone.get('option'))
                    return

            # 숫자키 1~4 및 넘버패드 1~4 감지 (실제 선택지 개수 가드 조건)
            num_keys = [(0x31, 1), (0x32, 2), (0x33, 3), (0x34, 4),
                        (0x61, 1), (0x62, 2), (0x63, 3), (0x64, 4)]
            
            for vk, choice_num in num_keys:
                if (user32.GetAsyncKeyState(vk) & 0x8000) != 0:
                    if len(self.active_choice_zones) >= choice_num:
                        chosen_option = self.active_choice_zones[choice_num - 1].get('option')
                        self.on_choice_selected_action(chosen_option)
                        return

        if not self.is_in_journey or not is_game_hovered_or_active():
            self.tooltip.hide_and_clear()
            return

        if getattr(self, '_is_loading_global', False):
            # 🌕 로딩 화면이 확인된 동안에는 어떤 경우에도 툴팁을 띄우지 않는다.
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

        # ⚡ [X좌표 정밀 가림막] 선택지 버튼의 실제 인게임 위치인 화면 우측 64% ~ 95% 내부만 허용!
        x_min = base_left + int(base_w * 0.64)
        x_max = base_left + int(base_w * 0.95)

        if not (x_min <= pos.x() <= x_max):
            self.tooltip.hide_and_clear()
            return

        # Y좌표 범위 검사 (0.00초 즉시 반응)
        hovered_zone = None
        for zone in self.active_choice_zones:
            if zone['y_min'] <= pos.y() <= zone['y_max']:
                hovered_zone = zone
                break

        if hovered_zone:
            if getattr(self, '_last_logged_hover_text', '') != hovered_zone['text']:
                self._last_logged_hover_text = hovered_zone['text']
                print(f"🎯 [선택지 상자 호버 감지] 마우스 Y: {pos.y()}px ➔ [{hovered_zone['text']}] (범위: {hovered_zone['y_min']}~{hovered_zone['y_max']}px)")

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

        # 🛡️ [초기화 순서 방어] 이 신호가 __init__ 도중(다른 속성들이 아직 다 안 만들어진
        # 시점)에 처리되는 경우를 대비해, 필요한 속성이 준비될 때까지 살짝 늦춰서 재시도한다.
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
        self.is_in_journey = True

        self.arcana_indexer = ArcanaCardIndexThread(cards=result['cards'])
        self.arcana_indexer.finished_signal.connect(self.on_arcana_indexed)
        self.arcana_indexer.start()

        if not self.event_ocr.isRunning(): self.event_ocr.start()
        if not self.choice_scanner.isRunning(): self.choice_scanner.start()

        self.status_overlay.set_custom_status(f"🟢 [통합 DB 완료] 총 {len(self.db_event_cache)}개 이벤트 로딩 완료", "#00e5ff", "#00e5ff")

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
            print(f"🆕 [신규 카드 자동 등록] {len(hash_cache)}개 카드가 인식 대상에 추가됨 (누적 {len(self.card_hash_cache)}개)")
        self._incremental_indexer = None

    def _is_force_confirm_active(self):
        # 🃏 지금 매칭된 이벤트가 강제 사진 확인 대상인지 여부.
        # "이격"이 가리키는 실제 대상은 "같은 제목의 이벤트가 여러 카드에 걸쳐 있고 카드마다
        # 보상이 다른 상황" 그 자체이므로, 이미 감지돼 있는 ambiguous_arcana_titles
        # (동명 이벤트 목록)를 그대로 체크한다. 이렇게 하면 동명 이벤트로 판정된 모든 경우에
        # 카드 후보 풀 좁히기 + lenient 모드(pdist 임계값은 무시하되 점수 19점 상한은 유지)가 적용된다.
        if not self.matched_db_title:
            return False
        clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
        return clean_title in self.ambiguous_arcana_titles

    def _card_search_pool(self):
        # 🃏 [카드 검색 범위 축소] 동명 이벤트라 강제 사진 확인 대상인 이벤트가 현재
        # 매칭돼 있으면(matched_db_title), 그 이벤트를 가진 카드들로만 비교 후보를 좁힌다.
        # 전체 카드와 비교하는 것보다 후보가 적을수록 phash/색상 유사도 오차로 엉뚱한 카드가
        # 뽑힐 확률이 줄어든다. 대상이 아니거나 카드 목록이 아직 비어있으면 평소처럼 전체
        # 캐시를 그대로 사용한다.
        if self._is_force_confirm_active():
            clean_title = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
            allowed_names = self.title_to_cards.get(clean_title)
            if allowed_names:
                pool = [c for c in self.card_hash_cache if c.get('name') in allowed_names]
                if pool:
                    return pool
        return self.card_hash_cache

    def _debug_reward_snippet(self):
        # 🔍 [디버그용] 현재 등록된 첫 선택지의 보상 원문 일부를 요약해서 로그에 남긴다.
        # 선택지 "이름"만으로는 동명 이벤트 오염 여부를 구분할 수 없어서, 실제 보상 내용까지
        # 비교할 수 있도록 추가함.
        try:
            if not self.current_event_options:
                return "(선택지 없음)"
            first = self.current_event_options[0]
            reward = (first.get('reward', '') or '').replace('\n', ' | ')
            return f"[{first.get('choice','')}] {reward[:60]}"
        except Exception:
            return "(요약 실패)"

    def on_card_art_matched(self, card_id, card_name):
        # 🛡️ [클릭 직후 전환 구간 보호] 방금 선택지를 클릭해서 화면 전환 여부를 확인 중인
        # 동안(is_waiting_for_next_event)에는 카드 아트 인식 결과를 신뢰하지 않는다. 이
        # 구간엔 화면이 전환 애니메이션 중이라 카드 아트 영역이 불안정해서 엉뚱한 카드로
        # 오인식되기 쉽고, 하필 그 오인식된 카드의 제목이 "동명 이벤트"에 걸리면 품질
        # 검증을 건너뛰는 관대 모드(_is_force_confirm_active)까지 켜져서 더 나쁜 매칭을
        # 그대로 확정해버리는 연쇄 오류가 있었다. 화면 전환이 실제로 확인된 뒤에 들어오는
        # 카드 인식 결과만 신뢰한다.
        if hasattr(self, 'event_ocr') and self.event_ocr.is_waiting_for_next_event:
            return

        # 🛡️ [오염 방지] 이미 제목 매칭으로 확실하게(strong) 결과가 정해진 상태라면,
        # 뒤늦게 도착한 카드 이미지 매칭 결과가 틀렸을 수 있으므로 current_matched_card_id 자체를
        # 건드리지 않는다. (여기서 먼저 덮어쓰면, 나중에 match_current_event_from_cache가
        # 재호출될 때 엉뚱한 카드의 기록으로 잘못 덮어쓰이거나, _record_card_history가
        # 올바른 결과를 잘못된 카드 이름으로 저장해버리는 문제가 있었다)
        if self.event_confirmed_strong:
            return

        if card_name == self.current_matched_card_id:
            return
        self.current_matched_card_id = card_name

        best_guess = None
        if self.current_event_title:
            clean_ocr_ev = re.sub(r'[^가-힣0-9]', '', self.current_event_title)
            clean_ocr_ev = re.sub(r'여정|이벤트|아르카나|알카|노아|NOA', '', clean_ocr_ev)
            # 🛡️ [사진 오인식 방어] 예전엔 hist_best가 있기만 하면 유사도 점수를 안 보고
            # 그냥 받아들였다. 카드 사진 인식(phash/색상) 자체가 비슷하게 생긴 다른 카드로
            # 잘못 매칭됐을 때, 그 잘못된 카드의 과거 기록 중 "그나마 제일 가까운" 걸 골라서
            # 아무 검증 없이 확정해버리는 게 오매칭의 원인이었다. 최소 유사도 기준을 둬서
            # 지금 화면 제목과 정말 무관한 기록은 걸러낸다.
            hist_best, hist_score = self._pick_closest_card_history(card_name, clean_ocr_ev)
            if hist_best and hist_score >= 0.35:
                best_guess = hist_best
            if best_guess is None:
                cand = self.resolved_card_events.get(card_name)
                # 🛡️ [사진 오인식 방어] 카드별 기본 이벤트(resolved_card_events)도 마찬가지로
                # 지금 화면 제목과 아예 무관하면 신뢰하지 않는다. 여기서 그냥 받아들이면,
                # 카드 인식이 엉뚱한 카드로 튄 순간 그 카드의 완전히 다른 이벤트 데이터가
                # "확정"으로 표시돼버린다.
                if cand and self._score_event_match(clean_ocr_ev, cand.get('cleanTitle', '')) >= 0.35:
                    best_guess = cand
        else:
            # 🛡️ [성급한 추정 방지] 제목 OCR이 아직 아무것도 못 읽은 상태에서는 카드 그림만 보고
            # 어떤 이벤트인지 교차검증할 방법이 없다. card_event_history에 이 카드가 가질 수 있는
            # 이벤트가 2개 이상 등록돼 있다면(=한 카드가 여러 이벤트를 가질 수 있는 카드라면),
            # resolved_card_events의 "첫 번째로 알려진 이벤트"는 사실상 도박이다. 이런 카드에서
            # 성급하게 추정치를 보여주면, 실제 제목이 확정된 뒤 다른 이벤트로 뒤늦게 바뀌는 게
            # 눈에 띄게 보인다(예: '서류 작업은...'이 잠깐 떴다가 '살롱 인 더 애프터눈'으로 바뀜).
            # 이 카드가 가질 수 있는 이벤트가 정확히 1개뿐이라 확실할 때만 즉시 보여준다.
            known_events = self.card_event_history.get(card_name, [])
            if len(known_events) <= 1:
                best_guess = self.resolved_card_events.get(card_name)
            else:
                self.status_overlay.set_custom_status(f"🟣 [카드 인식] {card_name} (제목 확인 대기 — 여러 이벤트 보유 카드)", "#c084fc", "#c084fc")

        if best_guess:
            self.matched_db_title = best_guess.get("rawTitle", card_name)
            self.current_event_options = best_guess.get("options", [])
            self.status_overlay.set_custom_status(f"🟣 [카드 인식/추정] {self.matched_db_title} (문구 확인 대기)", "#c084fc", "#c084fc")
            print(f"⚡ [캐시 즉시 매칭-추정] 카드 '{card_name}' ➔ DB: '{self.matched_db_title}' (선택지 {len(self.current_event_options)}개) | 보상요약: {self._debug_reward_snippet()}")
            if self.current_event_title: self.match_current_event_from_cache()
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()

    def update_event_title(self, title):
        title = fix_ocr_typos(title)
        clean_new = re.sub(r'[^가-힣0-9a-zA-Z]', '', title)
        clean_curr = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.current_event_title)

        if clean_new and clean_new != clean_curr:
            # 🛡️ [OCR 노이즈로 인한 불필요한 재처리 방지] 원문 OCR 텍스트는 같은 화면이어도
            # 매번 살짝 다르게 읽힐 수 있다(예: '살홍 애프터눈' / '살롱인 애프터눈' /
            # '살롱 인 더 애프터눈' — 전부 같은 이벤트). 이미 이벤트가 확정돼 있는 상태에서
            # 이런 노이즈성 재읽기까지 매번 툴팁 숨김 + zone 재등록 + 카드 정보 리셋을 하면
            # 화면이 불필요하게 깜빡이고 연산도 낭비된다. 새로 읽힌 텍스트를 DB와 빠르게
            # 대조해서 지금 확정된 이벤트와 같은 걸 가리키면, 원문만 갱신하고 무거운
            # 재처리는 건너뛴다.
            if self.event_confirmed and self.matched_db_title and self.db_event_cache:
                quick_best, quick_score = self._find_best_event(clean_new, self.db_event_cache)
                clean_matched = re.sub(r'[^가-힣0-9a-zA-Z]', '', self.matched_db_title)
                if quick_best and quick_score >= 0.5 and quick_best.get('cleanTitle', '') == clean_matched:
                    self.current_event_title = title  # 다음 비교 기준으로 원문은 갱신해둔다
                    return

            # 🃏 [카드 인식 보존] 카드 그림 인식이 제목 OCR보다 먼저 끝나는 경우가 흔한데,
            # 그 시점엔 current_event_title이 아직 비어있어서 카드 인식 결과가 current_matched_card_id에만
            # 저장되고 실제 매칭엔 못 쓰인다. 그 직후 제목이 도착했을 때 "새 제목이니 카드도 다시"라며
            # 무조건 current_matched_card_id를 지워버리면, 방금 맞게 인식해둔 카드 정보를 스스로
            # 날려버리고 동명 이벤트 상황에서 엉뚱한 카드 데이터가 잠깐 표시되는 원인이 된다.
            # → 직전 제목이 "아예 없었을 때"(=이번이 이 이벤트의 첫 제목)는 같은 이벤트로 보고 보존하고,
            #   직전 제목이 있었는데 바뀐 경우(=진짜 다른 이벤트로 전환)만 카드 인식도 새로 한다.
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
            self.active_choice_zones = []
            # 🧹 [버그 수정] 여기서 active_choice_zones만 비우고 tooltip.hide_and_clear()를
            # 안 불러주면, 마우스가 이전 화면의 선택지 버튼 위치에 그대로 있는 동안 툴팁이
            # 남아있는 문제가 생긴다(다음 hover 폴링 때 자동으로 지워지긴 하지만, 그 사이에
            # 화면이 아직 전환 애니메이션 중이면 눈에 띄게 오래 남아 보일 수 있다). 다른 모든
            # 정리 지점(로딩 감지, 수동 새로고침 등)과 동일하게 여기서도 즉시 같이 지운다.
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
        # 지금까지도 여전히 같은(다른 이벤트로 안 넘어간) 제목이고, 아직 강하게 확정이 안 됐다면
        # → 카드 인식이 끝내 실패한 것으로 보고, "확인 안 됨" 표시를 단 채로 최선의 추정치를 보여준다.
        if self.current_event_title != snapshot_title or self.event_confirmed_strong:
            return  # 이미 다른 이벤트로 넘어갔거나, 그 사이 정상적으로 카드가 확인됨 → 개입 불필요

        self.current_event_options = fallback_event.get('options', [])
        self.status_overlay.set_custom_status(
            f"⚠️ [카드 확인 실패 - 추정치] {self.matched_db_title} (동명 이벤트, 다른 카드일 수 있음)",
            "#ff9800", "#ff9800"
        )
        print(f"⚠️ [동명 이벤트 - 카드 확인 실패, 추정치로 대체 표시] '{snapshot_title}' ➔ DB: '{self.matched_db_title}' (점수:{score:.2f}) | 보상요약: {self._debug_reward_snippet()}")
        self.register_instant_estimated_zones()
        self.choice_scanner.reset_cache()

    def match_current_event_from_cache(self):
        if not self.current_event_title: return
        clean_ocr_ev = re.sub(r'[^가-힣0-9]', '', self.current_event_title)
        clean_ocr_ev = re.sub(r'여정|이벤트|아르카나|알카|노아|NOA', '', clean_ocr_ev)
        if not clean_ocr_ev or len(clean_ocr_ev) < 2: return

        # ⚡ 소요시간 측정 타임스탬프 기록
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
                print(f"🎯 [카드별 역사 매칭 성공, 유사도:{card_hist_score:.2f}] 카드 '{self.current_matched_card_id}' ➔ '{self.matched_db_title}' (선택지 {len(self.current_event_options)}개) | 보상요약: {self._debug_reward_snippet()}")
                self.register_instant_estimated_zones()
                self.choice_scanner.reset_cache()
                return

            card_cached = self.resolved_card_events.get(self.current_matched_card_id)
            # 🛡️ [사진 오인식 방어] 위와 동일한 이유로, 카드 인식 결과가 가리키는 "카드별 기본
            # 이벤트"도 지금 화면에 뜬 제목(clean_ocr_ev)과 아예 무관하면 신뢰하지 않는다.
            # 예전엔 여기서 검증 없이 바로 event_confirmed_strong=True로 확정해버려서, 사진
            # 인식이 비슷하게 생긴 다른 카드로 오인식됐을 때 그 카드의 완전히 다른 이벤트가
            # 표시되는 사고가 있었다("동명 이벤트" 목록에 걸려 카드 확인 대기 중이었는데도
            # 카드 결과 자체가 틀려서 그대로 통과돼버린 경우).
            if card_cached and self._score_event_match(clean_ocr_ev, card_cached.get('cleanTitle', '')) >= 0.35:
                self.matched_db_title = card_cached.get('rawTitle', '')
                self.current_event_options = card_cached.get('options', [])
                self.event_confirmed = True
                self.event_confirmed_strong = True
                self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
                print(f"🎯 [카드 보완 매칭] 카드 '{self.current_matched_card_id}' 전용 데이터 사용 ➔ '{self.matched_db_title}' (선택지 {len(self.current_event_options)}개) | 보상요약: {self._debug_reward_snippet()}")
                self.register_instant_estimated_zones()
                self.choice_scanner.reset_cache()
                return
            elif card_cached:
                print(f"⚠️ [카드 인식 불일치 의심] 카드 '{self.current_matched_card_id}'의 기본 이벤트('{card_cached.get('rawTitle','')}')가 현재 제목('{self.current_event_title}')과 안 맞음 → 이 카드 매칭은 무시하고 제목 기반 매칭으로 폴백")

        STRONG_TITLE_MATCH_THRESHOLD = 0.5
        if best_event and best_score >= STRONG_TITLE_MATCH_THRESHOLD:
            # 🃏 [동명 이벤트 카드 방어] 이 제목을 여러 카드가 공유한다면(ambiguous_arcana_titles),
            # 제목만으로는 어느 카드인지 알 수 없다. 이 경우 제목 유사도 매칭이 다른 카드의
            # 이벤트와 우연히 비슷하게 걸려 잘못 확정되는 사고를 막기 위해, "강한 확정"을
            # 걸지 않는 것은 물론, 아예 좌표/툴팁도 등록하지 않는다 — 카드 미확인 상태에서
            # 아무 카드에서나 가져온 추정 데이터를 화면에 잠깐이라도 보여주면, 그 틈에 호버해서
            # 잘못된 보상을 볼 수 있다. 카드 인식이 도착할 때까지는 "확인 중"이라고만 표시하고
            # 실제로 확인된 뒤에만 보여준다.
            is_ambiguous_title = best_event.get('cleanTitle', '') in self.ambiguous_arcana_titles
            self.matched_db_title = best_event.get('rawTitle', '')
            self.event_confirmed = True
            self.event_confirmed_strong = not is_ambiguous_title

            if is_ambiguous_title:
                self.current_event_options = []
                self.active_choice_zones = []
                self.status_overlay.set_custom_status(f"🟣 [카드 확인 중] {self.matched_db_title} (동명 이벤트, 카드 인식 대기)", "#c084fc", "#c084fc")
                print(f"🃏 [동명 이벤트 - 카드 확인 대기, 좌표 미등록] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}' (점수:{best_score:.2f})")
                self.choice_scanner.reset_cache()
                # 🛡️ [폴백] 카드 인식이 애초에 실패하는 카드(예: 새로 추가되어 이미지 특징이
                # 아직 없는 카드)일 경우, 이 상태로 영원히 아무것도 안 뜰 수 있다. 정확성을
                # 위해 만든 안전장치가 오히려 "아예 못 씀"이 되는 걸 막기 위해, 일정 시간
                # 기다려도 카드가 확정되지 않으면 "확인 안 됨" 표시를 달고 최선의 추정치를
                # 대신 보여준다 (틀릴 수 있음을 명시).
                snapshot_title = self.current_event_title
                QTimer.singleShot(1200, lambda: self._fallback_ambiguous_event(snapshot_title, best_event, best_score))
                return

            self.current_event_options = best_event.get('options', [])
            self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
            print(f"🎯 [제목 기반 매칭 성공, 점수:{best_score:.2f}] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}' (선택지 {len(self.current_event_options)}개) | 보상요약: {self._debug_reward_snippet()}")
            if self.current_matched_card_id:
                self._record_card_history(self.current_matched_card_id, best_event)
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()
            return

        if best_event:
            # 🃏 [약한 매칭도 동일하게 동명 이벤트 강제 확인 적용]
            is_ambiguous_title = best_event.get('cleanTitle', '') in self.ambiguous_arcana_titles
            self.matched_db_title = best_event.get('rawTitle', '')
            self.event_confirmed = True

            if is_ambiguous_title:
                # 🃏 강한 매칭 경로와 동일한 방어: 카드 미확인 상태에서 동명 이벤트의 추정 데이터를
                # 보여주지 않는다.
                self.current_event_options = []
                self.active_choice_zones = []
                self.status_overlay.set_custom_status(f"🟣 [카드 확인 중] {self.matched_db_title} (동명 이벤트, 카드 인식 대기)", "#c084fc", "#c084fc")
                print(f"🃏 [동명 이벤트(약한 매칭) - 카드 확인 대기, 좌표 미등록] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}' (점수:{best_score:.2f})")
                self.choice_scanner.reset_cache()
                snapshot_title = self.current_event_title
                QTimer.singleShot(1200, lambda: self._fallback_ambiguous_event(snapshot_title, best_event, best_score))
                return

            self.current_event_options = best_event.get('options', [])
            self.status_overlay.set_custom_status(f"🟢 [여정 진행 중] {self.matched_db_title}", "#00e5ff", "#00e5ff")
            print(f"🎯 [제목 기반 매칭 성공(약함), 점수:{best_score:.2f}] '{self.current_event_title}' ➔ DB: '{self.matched_db_title}' (선택지 {len(self.current_event_options)}개) | 보상요약: {self._debug_reward_snippet()}")
            self.register_instant_estimated_zones()
            self.choice_scanner.reset_cache()

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
            # 🛡️ [스태일 데이터 방지] 여기서 툴팁만 숨기고 좌표/선택지 데이터를 안 지우면,
            # 나중에 is_in_journey가 (OCR 오탐 등으로) 잠깐이라도 다시 True가 되는 순간
            # 이미 남아있던 예전 데이터가 그대로 다시 튀어나올 수 있다. 여정을 벗어났다고
            # 판단되는 시점엔 확실하게 다 같이 정리한다.
            self.active_choice_zones = []
            self.current_event_options = []
            self.current_event_title = ""
            self.matched_db_title = ""
            self.event_confirmed = False
            self.event_confirmed_strong = False
            # 🛡️ [OCR 스레드 기억도 같이 리셋] 여기서 우리 쪽 상태만 지우고 제목 OCR 스레드가
            # "직전에 본 제목"을 계속 기억하고 있으면, 나중에 같은 제목이 다시 떠도 "중복이네"
            # 하고 무시해버려서 우리 상태가 텅 빈 채로 영원히 안 채워지는 문제가 있었다
            # (다른 화면 봤다가 돌아왔을 때 재현됨). 스레드의 기억도 같이 지워서, 이후엔
            # 어떤 제목이 뜨든 "새로운 제목"으로 취급해 반드시 다시 매칭이 일어나게 한다.
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
        column_content_widths = []  # 🖼️ 각 블록의 실제 텍스트 폭(px) — 가로 2단 배치 시 폭 계산에 사용
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
                    
                    # ⚠️ "할인" in nxt (포함 여부만 체크)는 원래 쪼개진 조각을 다시 붙이려는
                    # 용도였는데, 지금은 완성된 보상 문장("특수 잠재력: ... 10% 할인")까지
                    # 통째로 앞줄(조건)에 삼켜버리는 부작용이 있어 제거. "할인"으로 시작하는
                    # 조각(진짜 쪼개진 경우)만 병합한다.
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

            # 🎯 조건 아이콘: "재화/아이템을 소모"하는 것과 "스탯이 기준 이상이면 통과"하는 것은
            # 성격이 다르므로(전자는 지불, 후자는 단순 요구조건) 아이콘을 구분한다.
            def cond_icon(text):
                if "사용" in text or "소모" in text:
                    return "💰"
                return "📊"  # 이상 필요 등 소모 없는 스탯 요구조건

            def render_conds(conds):
                return " &nbsp; ".join(f"{cond_icon(c)} {esc(c)}" for c in conds)

            html_parts = []
            _scale = getattr(self, 'tooltip_font_scale', 1.0)
            _badge_px = max(9, round(12 * _scale))
            _cond_px = max(9, round(12 * _scale))
            _bullet_px = max(10, round(14 * _scale))

            # 📏 [실제 텍스트 폭 측정] 대충 정한 배율로 폭을 넓히면 글자가 길 때 여전히
            # 잘려서 줄바꿈될 수 있다. 이 블록에서 실제로 렌더링될 줄들(배지+조건 한 줄,
            # 그리고 불릿마다 한 줄씩) 중 가장 긴 줄의 실제 픽셀 폭을 측정해두고, 나중에
            # 가로 2단 배치 폭을 정할 때 이 값을 기준으로 정확히 맞춘다.
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
            # 🖼️ [가로 2단 배치] 폰트가 "보통"(1.0)을 초과(크게/아주 크게)하는 상태에서
            # 이지/노말/하드처럼 보상 블록이 여러 개면, 세로로 계속 쌓았을 때 툴팁이
            # 화면 아래로 뚫고 나가기 쉽다. 이 경우 세로 나열 대신 표(<table>)로 옆에
            # 나란히 배치해서 아래 대신 옆으로 늘어나게 한다. 블록이 1개뿐이거나
            # 폰트가 보통 이하면 기존처럼 세로로 쌓는 게 더 읽기 편하므로 그대로 둔다.
            use_columns = _scale > 1.0 and len(formatted_outcomes) > 1
            min_width = None
            if use_columns:
                cell_parts = []
                # 📏 [정밀 폭 계산] 대충 정한 배율 대신, 컬럼별로 실측한 텍스트 폭
                # (column_content_widths)을 그대로 더해서 필요한 최소 폭을 계산한다.
                # 각 셀의 좌우 padding(16px×2, 첫 칸은 왼쪽 패딩 없음), 구분선 셀(1px + 여백),
                # 실패 배경 블록의 자체 padding(8px×2), 라벨 외곽 padding/테두리까지 전부 반영해
                # "잠재력 포인트 +30"처럼 긴 줄도 잘리지 않게 한다.
                CELL_SIDE_PADDING = 16
                FAIL_BLOCK_PADDING = 16 if has_failure else 0  # div padding:4px 8px 의 좌우 8px×2
                DIVIDER_WIDTH = 1 + CELL_SIDE_PADDING * 2
                LABEL_OUTER = 18 * 2 + 2 * 2  # QLabel padding(14px 18px)의 좌우 18px×2 + border 2px×2
                SAFETY_MARGIN = 20  # 이모지/아이콘 폭 오차 보정 여유

                total = LABEL_OUTER + SAFETY_MARGIN
                for i, outcome in enumerate(formatted_outcomes):
                    left_pad = 0 if i == 0 else CELL_SIDE_PADDING
                    right_pad = CELL_SIDE_PADDING
                    total += column_content_widths[i] + left_pad + right_pad + FAIL_BLOCK_PADDING
                    cell_parts.append(
                        f'<td style="vertical-align:top; white-space:nowrap; padding:2px {right_pad}px 2px {left_pad}px;">{outcome}</td>'
                    )
                    if i < len(formatted_outcomes) - 1:
                        # 컬럼 사이 구분선: Qt 리치텍스트는 td에 border-left를 안정적으로
                        # 못 그리는 경우가 있어, 1px짜리 배경색 셀로 확실하게 그린다.
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
    # 🔒 [중복 실행 방지] 이미 켜져 있으면 새 인스턴스는 알림만 띄우고 바로 종료
    if not ensure_single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(0, "JourneyHelper가 이미 실행 중입니다.\n작업표시줄 트레이 아이콘을 확인해주세요.", "이미 실행 중", 0x40)
        except Exception:
            pass
        sys.exit(0)

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