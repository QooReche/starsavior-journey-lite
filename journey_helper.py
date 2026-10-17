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
import jh_net
import jh_paths
import jh_savior
import jh_schedule

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
from jh_version import APP_VERSION  # noqa: E402  (버전 단일 출처: jh_version.py)

DEFAULT_SETTINGS = {
    "language": "ko",               # "ko" | "en"  (화면 문구 + DB 언어, 변경 시 재시작)
    "startup_state": "collapsed",   # 상단바 시작 상태: "collapsed" | "expanded"
    "tooltip_font_scale": 1.0,
    "show_info": True,              # 잠재력/버프 설명 표시
    "low_spec_mode": False,         # OCR 주기를 늘려 CPU 절약
    "status_badge_locked": True,
    "status_badge_offset": None,
    "difficulty": "all",            # (이전 방식, 읽기 전용) 한 난이도만 표시. hidden_difficulties 로 옮겨진다
    "hidden_difficulties": [],      # 툴팁에서 숨길 난이도 ["easy","normal","hard"] 부분집합 (원작 1.1.3 과 같은 키)
    "show_schedule": True,          # 좌측 상단 날짜 위젯에 마우스를 올리면 턴별 고정 일정 표시
    "show_savior_reco": True,       # '여정 시작' 화면에서 구원자 식별 동안 계속 추천 아르카나(썸네일 포함) 표시
    "show_capture_debug": False,    # True: 상단바/툴팁이 스크린샷에도 보이게 함 (진단용, OCR 방해 가능)
}

# 원작 1.1.3 '왼쪽 툴팁 글자 크기' 선택지와 같은 배율
FONT_SCALE_CHOICES = (("작게", 0.85), ("보통", 1.0), ("크게", 1.2), ("아주 크게", 1.4))


def snap_font_scale(v):
    """저장된 배율을 가장 가까운 선택지로 맞춘다 (이전 버전의 1.25/1.5 도 크게/아주 크게로)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    return min((val for _, val in FONT_SCALE_CHOICES), key=lambda c: abs(c - v))

# 게임 창 기준 비율 (원작 값 유지). 라벨은 '여정 이벤트' 문구가 있는 작은 띠.
REGION_FRACTIONS = {
    "event_title": {"top": 0.205, "left": 0.110, "width": 0.240, "height": 0.056},
    "event_label": {"top": 0.170, "left": 0.110, "width": 0.200, "height": 0.032},
    "schedule_date": {"top": 0.078, "left": 0.116, "width": 0.058, "height": 0.040},      # 좌측 상단 '3월 초순' 문구 (게임 클라이언트 비율)
}
REGION_FRACTIONS.update(jh_savior.SAVIOR_REGIONS)      # savior_header / savior_subtitle / savior_name
REGION_FRACTIONS.update(jh_savior.ARCANA_SCREEN_REGIONS)   # 편성 화면 고정 글자(전체 해제/자동 편성/특수재화 우선 사용/여정 시작)
REGION_FRACTIONS.update(jh_savior.PICKER_REGIONS)          # 아르카나 고르기 화면 부정 신호(하단 안내줄/보유 수)
SAVIOR_CLICK_HIDE_SEC = 0.5     # 왼쪽 클릭 직후 추천 툴팁을 잠깐 숨기고 다음 스캔에서 화면을 다시 판정
SAVIOR_SCAN_SEC = 1.0
SAVIOR_CONFIRM = 2        # 같은 구원자가 이만큼 연속으로 읽혀야 확정
SAVIOR_LOST = 3           # '여정 시작' 머리글이 이만큼 연속으로 안 보이면 화면을 떠난 것으로 본다
ARCANA_CONFIRM = 2        # 편성 화면 글자가 이만큼 연속으로 읽혀야 '편성 화면'으로 확정(툴팁 허용)
ARCANA_LOST = 2           # 이만큼(약 2초) 연속으로 못 읽어야 해제 (간헐적 OCR 실패로 툴팁이 깜빡이지 않게)
SAVIOR_NOREAD_LOST = 6    # 머리글은 보이는데 이름을 이만큼 연속으로 못 읽으면 인식 해제
SCHEDULE_HOVER = (0.03, 0.03, 0.18, 0.13)      # 날짜 위젯 전체(목표 달력 + 목표/날짜 문구) 호버 범위 (left, top, right, bottom)
DATE_SCAN_SEC = 1.0
DATE_CONFIRM = 2          # 같은 턴이 이만큼 연속으로 읽혀야 확정
DATE_LOST = 6             # 날짜가 이만큼 연속으로 안 읽히면 '턴 모름'으로 되돌림
EVENT_LABEL_PHRASES = ("여정이벤트", "훈련이벤트", "아르카나이벤트")
EVENT_LABEL_PHRASES_EN = ("journeyevent", "trainingevent", "arcanaevent")
NOISE_TITLE_WORDS = ("여정이벤트", "훈련이벤트", "아르카나이벤트", "제목", "journeyevent", "trainingevent", "arcanaevent")
_GAME_KEYWORDS = ["스타세이비어", "스타 세이비어", "starsavior", "star savior"]
_BROWSER_CLASSES = {"chrome_widgetwin_1", "mozillawindowclass", "applicationframewindow", "operawindowclass"}
# 제목에 'starsavior' 가 들어가기 쉬운 비게임 창(폴더 이름이 제목인 탐색기, 편집기, 터미널 등)은 게임으로 보지 않는다
_NON_GAME_CLASSES = _BROWSER_CLASSES | {"cabinetwclass", "notepad", "consolewindowclass", "cascadia_hosting_window_class",
                                         "pseudoconsolewindow", "progman", "workerw", "shell_traywnd"}
_NON_GAME_EXES = {"explorer.exe", "code.exe", "notepad.exe", "notepad++.exe", "devenv.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
                  "windowsterminal.exe", "conhost.exe", "pycharm64.exe", "idea64.exe", "sublime_text.exe", "obsidian.exe",
                  "chrome.exe", "msedge.exe", "firefox.exe", "whale.exe", "opera.exe", "brave.exe", "slack.exe", "discord.exe"}


def is_non_game_host(win_class, exe_name):
    """창 클래스 / 실행 파일 이름으로 '게임일 수 없는' 창을 가려낸다 (제목 문자열만으로는 폴더·문서 창을 구분 못 함)."""
    return (win_class or "").lower() in _NON_GAME_CLASSES or (exe_name or "").lower() in _NON_GAME_EXES


# ----------------------------------------------------------------------------
# 경로 / 설정 / 로그
# ----------------------------------------------------------------------------
def resource_path(*parts):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def get_settings_dir():
    """settings.json / custom_events.json / cache / card_debug 가 있는 data 폴더 (휴대형: 앱 폴더/data, 폴백: %APPDATA%)."""
    return jh_paths.data_dir()


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(get_settings_dir(), "settings.json"), "r", encoding="utf-8-sig") as f:   # BOM 허용
            saved = json.load(f)
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except Exception:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if settings.get("language") not in jh_db.SUPPORTED_LANGS:
        settings["language"] = "ko"
    if settings.get("difficulty") not in jh_db.DIFFICULTIES:
        settings["difficulty"] = "all"
    hidden = settings.get("hidden_difficulties")
    if "hidden_difficulties" not in saved:          # 이전 '한 난이도만 표시' 설정을 숨김 목록으로 옮긴다
        hidden = jh_db.hidden_set(settings["difficulty"])
    settings["hidden_difficulties"] = sorted(jh_db.hidden_set(hidden if isinstance(hidden, (list, tuple, set)) else []),
                                             key=jh_db.DIFFICULTIES.index)
    settings["tooltip_font_scale"] = snap_font_scale(settings.get("tooltip_font_scale", 1.0))
    if settings.get("startup_state") not in ("collapsed", "expanded"):
        settings["startup_state"] = "collapsed"
    for k, dv in DEFAULT_SETTINGS.items():             # bool 설정은 진짜 bool 만 인정 ("false" 문자열 등은 기본값)
        if isinstance(dv, bool) and not isinstance(settings.get(k), bool):
            settings[k] = dv
    off = settings.get("status_badge_offset")
    if off is not None and not (isinstance(off, (list, tuple)) and len(off) == 2
                                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in off)):
        settings["status_badge_offset"] = None
    return settings


LOG_MAX_BYTES = jh_paths.LOG_MAX_BYTES            # 1MB 초과 시 log.old.txt 로 교체(1개 보관)
_log_lock = threading.Lock()


def log(msg):
    """앱 폴더/log/log.txt 에 한 줄 기록 (1MB 에서 log.old.txt 로 교체). 폴더가 지워져도 다시 만든다. 어떤 실패도 앱을 멈추지 않는다."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with _log_lock:
            jh_paths.write_log(line)
    except Exception:
        pass


def save_settings(settings):
    try:
        with open(os.path.join(get_settings_dir(), "settings.json"), "w", encoding="utf-8") as f:     # (get_settings_dir 가 폴더를 다시 만든다)
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
    "🔴 [OCR 모델 로딩 실패] 잠시 후 자동으로 다시 시도합니다 (log.txt 확인)": "🔴 [OCR model failed to load] Retrying automatically (see log.txt)",
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
    "🎯 인식 상태 초기화": "🎯 Reset recognition state",
    "🔄 [새로고침됨] 여정 이벤트 화면을 다시 찾고 있습니다...": "🔄 [Refreshed] Looking for the Journey event screen again...",
    "🔵 [선택 반응 확인 중] 화면 전환 여부 확인 중...": "🔵 [Checking Choice] Checking whether the screen changed...",
    "▶️ 헬퍼 재개": "▶️ Resume helper",
    "⏸️ 헬퍼 일시정지": "⏸️ Pause helper",
    "🛑 프로그램 종료": "🛑 Quit Program",
    "⚙️ 설정": "⚙️ Settings",
    "🔤 왼쪽 툴팁 글자 크기": "🔤 Left tooltip font size",
    "🎚️ 툴팁에서 숨길 난이도": "🎚️ Difficulties to hide from tooltip",
    "작게": "Small", "보통": "Normal", "크게": "Large", "아주 크게": "Extra Large",
    "🚀 시작 시 상단바": "🚀 Top bar on startup",
    "접힘": "Collapsed", "펼침": "Expanded",
    "🌐 언어": "🌐 Language",
    "🎚️ 난이도 표시": "🎚️ Difficulty filter",
    "전체": "All", "쉬움": "Easy", "어려움": "Hard",
    "잠재력/버프 설명 표시": "Show potential/buff descriptions",
    "⚡ 저사양 모드 (OCR 주기 늘림)": "⚡ Low-spec mode (slower OCR polling)",
    "상단바 위치 고정": "Lock top bar position",
    "🃏 구원자 추천 아르카나 (여정 시작 화면에서 이름에 마우스)": "🃏 Recommended arcana (hover the savior name on the Journey Start screen)",
    "🃏 [구원자 인식] {}": "🃏 [Savior] {}",
    "상단바 위치 초기화": "Reset top bar position",
    "현재 버전: {}": "Current version: {}",
    "JourneyHelper가 실행 중입니다 (우클릭: 메뉴)": "JourneyHelper is running (right-click for menu)",
    "⚙️ 설정 열기": "⚙️ Open settings",
    "설정": "Settings", "⚙ 설정": "⚙ Settings", "일반": "General", "게임 표시": "Game display", "고급·진단": "Advanced / Diagnostics",
    "시작": "Startup", "시작할 때 상단바 상태": "Top bar state on startup",
    "프로그램을 켰을 때 상단바를 접어 둘지 펼칠지 정해요.": "Choose whether the top bar starts collapsed or expanded.",
    "언어": "Language", "표시 언어": "Display language",
    "바꾸면 이벤트 데이터를 새 언어로 다시 불러오기 위해 재시작해야 해요.": "Changing it restarts the app to reload event data in the new language.",
    "상단바": "Top bar", "끄면 상단바를 드래그해서 옮길 수 있어요.": "When off, you can drag the top bar to move it.",
    "성능": "Performance", "저사양 모드 (OCR 주기 늘림)": "Low-spec mode (slower OCR polling)",
    "화면을 읽는 주기를 늘려 CPU 사용량을 줄여요. 반응이 조금 느려질 수 있어요.": "Reads the screen less often to save CPU. Reaction may be slightly slower.",
    "툴팁": "Tooltip", "왼쪽 툴팁 글자 크기": "Left tooltip font size",
    "선택지에 마우스를 올렸을 때 나오는 툴팁의 글자 크기예요.": "Font size of the tooltip shown when hovering a choice.",
    "툴팁에 잠재력·버프 효과 설명을 함께 보여줘요.": "Also shows potential/buff effect descriptions in the tooltip.",
    "체크한 난이도의 수치는 툴팁에서 빼요.": "Checked difficulties are left out of the tooltip.",
    "기능": "Features", "📅 일정 미리보기": "📅 Schedule preview",
    "게임 좌측 상단 날짜에 마우스를 올리면 이번 턴 일정을 보여줘요.": "Hover the date at the top-left of the game to see this turn's schedule.",
    "🃏 구원자 추천 아르카나": "🃏 Recommended arcana",
    "아르카나 편성 화면에서 추천·대체 카드를 보여줘요.\n카드 고르기 화면에서는 자동으로 숨겨요.":
        "Shows recommended/alternative cards on the arcana setup screen.\nAutomatically hidden on the card picker screen.",
    "진단": "Diagnostics", "상단바/툴팁을 스크린샷에 보이게 하기": "Show top bar/tooltip in screenshots",
    "스크린샷·녹화에 상단바와 툴팁이 찍혀요.": "The top bar and tooltip appear in screenshots and recordings.",
    "⚠ 진단 전용이에요. 켜 두면 화면 인식(OCR)에 영향을 줄 수 있어요.": "⚠ Diagnostic use only. Leaving it on may affect screen recognition (OCR).",
    "관리": "Maintenance", "DB 새로고침 (10분 쿨다운)": "Refresh DB (10 min cooldown)",
    "가이드 사이트에서 최신 데이터를 다시 받아요.": "Downloads the latest data from the guide site.",
    "인식 상태 초기화": "Reset recognition state",
    "추적 중인 이벤트·선택 잠금을 비우고 화면을 다시 찾아요.": "Clears the tracked event/choice lock and searches the screen again.",
    "문제 확인": "Troubleshooting", "로그 폴더 열기": "Open log folder", "진단 정보 복사": "Copy diagnostics", "복사됨": "Copied",
    "버전·설정·게임 창·최근 로그를 클립보드에 복사해요. 사용자 이름과 경로는 가려져요.":
        "Copies version, settings, game window and recent log to the clipboard. User name and paths are masked.",
    "프로그램 종료": "Quit program", "닫기": "Close",
    "재시작 필요": "Restart Required",
    "언어를 바꾸면 여정 이벤트 데이터를 새 언어로 다시 불러와야 해요.\n지금 재시작할까요? (재시작 안 하면 일부 이벤트 인식이 이전 언어\n기준으로 남아있어 오작동할 수 있어요)":
        "Changing the language requires reloading Journey event data in the new language.\nRestart now? (If you don't, some event recognition may stay on the old language and misbehave)",
}


def set_language(lang):
    global _CURRENT_LANGUAGE
    _CURRENT_LANGUAGE = lang if lang in jh_db.SUPPORTED_LANGS else "ko"
    jh_paths.LANG = _CURRENT_LANGUAGE


def get_language():
    return _CURRENT_LANGUAGE


def tr(text):
    """현재 언어가 "en"이면 번역표에서 찾아 반환하고, 없으면 한글 원문을 그대로 돌려준다."""
    if _CURRENT_LANGUAGE == "en":
        return _TRANSLATIONS_EN.get(text, text)
    return text



def install_crash_logger():
    def hook(exc_type, exc, tb):
        try:
            path = os.path.join(jh_paths.log_dir(), "crash_log.txt")
            jh_paths.rotate_if_big(path)                          # 반복 예외로 무한히 커지지 않게 회전
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


def _exe_name(hwnd):
    """창을 소유한 프로세스의 실행 파일 이름(소문자). 알 수 없으면 빈 문자열."""
    try:
        k32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _is_game_candidate(user32, hwnd):
    """제목이 게임처럼 보이고, 브라우저/탐색기/편집기 같은 비게임 창이 아닌가."""
    if not hwnd or not _title_is_game(_window_text(user32, hwnd)):
        return False
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return not is_non_game_host(buf.value, _exe_name(hwnd))


def _title_is_game(title):
    t = title.lower()
    return any(k in t for k in _GAME_KEYWORDS)


def find_game_hwnd():
    global _cached_hwnd
    try:
        user32 = ctypes.windll.user32
        if _cached_hwnd and user32.IsWindow(_cached_hwnd) and _title_is_game(_window_text(user32, _cached_hwnd)):
            return _cached_hwnd
        my_pid, found = os.getpid(), {"h": None}
        proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != my_pid and _is_game_candidate(user32, hwnd):
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
        return any(h and _is_game_candidate(user32, h) for h in cands)
    except Exception:
        return False


def _is_game_window(user32, hwnd):
    return bool(hwnd and _is_game_candidate(user32, hwnd))


def is_game_foreground():
    """지금 포커스(포그라운드)가 게임 창인가. 숫자키 선택 감지는 이때만 인정한다."""
    try:
        user32 = ctypes.windll.user32
        return _is_game_window(user32, user32.GetForegroundWindow())
    except Exception:
        return False


def is_cursor_over_game():
    """마우스 커서 아래의 최상위 창이 게임 창인가 (다른 창이 게임을 가리고 있으면 False)."""
    try:
        user32 = ctypes.windll.user32
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hwnd = user32.WindowFromPoint(pt)
        return _is_game_window(user32, (user32.GetAncestor(hwnd, 2) or hwnd) if hwnd else None)
    except Exception:
        return False


def filter_game_inputs(keys, game_foreground, cursor_over_game):
    """전역 입력(GetAsyncKeyState) 중 게임에 향한 것만 남긴다: 숫자키 = 게임이 포그라운드일 때만,
    좌클릭 = 커서가 게임 창 위에 있을 때(또는 게임이 포그라운드일 때)만."""
    out = set(keys)
    if not game_foreground:
        out -= set(CHOICE_KEYS)
    if not (cursor_over_game or game_foreground):
        out.discard(VK_LBUTTON)
    return out


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
    ko = re.sub(r"[^가-힣]", "", text or "")
    ko = re.sub(r"(?:알카나|아드가나|아르가나)(?=이벤)", "아르카나", ko)      # 원작이 처리하는 OCR 변형 ('이벤트' 가 뒤따를 때만)
    pairs = [(ko, EVENT_LABEL_PHRASES, 4)]
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


def toggle_hidden_difficulty(hidden, value, checked):
    """숨길 난이도 목록에서 value 를 켜고/끈 새 목록(정렬됨)을 돌려준다. 바로 툴팁에 반영된다(재시작 불필요)."""
    s = jh_db.hidden_set(hidden if isinstance(hidden, (list, tuple, set)) else [])
    if checked:
        s.add(value)
    else:
        s.discard(value)
    return sorted(jh_db.hidden_set(s), key=jh_db.DIFFICULTIES.index)
TOOLTIP_MIN_TOP_FRAC = 0.13   # 구원자 추천 툴팁이 위로 밀려 올라갈 수 있는 한계(창 높이 비율): 상단바 바로 아래
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


class RetryBackoff:
    """실패한 작업의 재시도 간격(초)을 단계적으로 늘린다: DELAYS 를 차례로 쓰고 마지막 값에서 고정."""
    DELAYS = (5.0, 15.0, 30.0, 60.0)

    def __init__(self, clock=time.monotonic):
        self.clock, self.fails, self._next = clock, 0, 0.0

    def ready(self):
        return self.clock() >= self._next

    def failed(self):
        self.fails += 1
        self._next = self.clock() + self.DELAYS[min(self.fails, len(self.DELAYS)) - 1]
        return self.fails

    def success(self):
        self.fails, self._next = 0, 0.0


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


CHOICE_KEYS = {0x31: 1, 0x32: 2, 0x33: 3, 0x34: 4, 0x61: 1, 0x62: 2, 0x63: 3, 0x64: 4}   # 숫자 1~4, 숫자패드 1~4
VK_LBUTTON = 0x01
TRANSITION_POLL_MS = 120      # 선택 후 선택지 패널 변화 확인 주기 (원작 값)
TRANSITION_DIFF = 18          # 64x36 축소 gray 평균 차이가 이 값을 넘는 프레임이 2번 연속이면 '화면 전환' (원작 값)
CLICK_BLOCK_SEC = 2.5         # 선택 직후 추가 입력 무시 (원작 값)
HOVER_DWELL_SEC = 0.2         # (경량판 보강) 선택지 존 위에 이만큼 머문 뒤의 클릭만 '선택'으로 인정 - 대사 넘기기 클릭 오감지 완화
NO_REACTION_RELEASE_SEC = 3.0   # (경량판 보강) 선택 감지 후 이 시간 동안 패널이 안 변하면 '선택이 안 먹힘/오감지'로 보고 잠금 해제
BUTTONS_GONE_RATIO = 0.75     # 패널 경계선 에너지가 클릭 때의 이 비율 이하로 줄면 '선택지 버튼이 사라짐'


def detect_choice_input(pressed, prev_pressed, cursor_idx, n_zones):
    """선택지 선택 입력 판정 (원작 1.0.6/1.1.3 check_instant_hover 와 같은 키 구성).
    pressed/prev_pressed: 지금/직전에 눌려 있던 가상 키 코드 집합. 새로 눌린 키만 본다(누르고 있는 동안 반복 안 함).
    cursor_idx: 마우스가 올라가 있는 선택지 번호(0부터) 또는 None. 반환: 선택된 선택지 번호(0부터) 또는 None."""
    new = set(pressed) - set(prev_pressed)
    if VK_LBUTTON in new and cursor_idx is not None:
        return cursor_idx
    for vk in sorted(new):
        n = CHOICE_KEYS.get(vk)
        if n and n <= n_zones:
            return n - 1
    return None


def panel_diff(base, cur):
    """선택지 패널 축소 이미지(64x36 gray) 두 장의 평균 밝기 차이. 크기가 다르면 None."""
    if base is None or cur is None or base.shape != cur.shape:
        return None
    return float(np.mean(np.abs(cur.astype(np.int16) - base.astype(np.int16))))


def panel_energy(gray):
    """패널 축소 이미지의 경계선 에너지(가로·세로 인접 픽셀 차이의 평균). 선택지 버튼(테두리·글자)이 있으면 커진다."""
    if gray is None or gray.ndim != 2 or gray.shape[0] < 2 or gray.shape[1] < 2:
        return 0.0
    g = gray.astype(np.int16)
    return float(np.mean(np.abs(np.diff(g, axis=1))) + np.mean(np.abs(np.diff(g, axis=0))))


class HoverDwell:
    """커서가 같은 선택지 존에 머문 시간. 짧게 스친 뒤의 클릭(대사 넘기기)을 선택으로 보지 않기 위해 쓴다."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.idx, self.since = clock, None, 0.0

    def update(self, idx):
        if idx != self.idx:
            self.idx, self.since = idx, self.clock()

    def dwelled(self, sec=None):
        return self.idx is not None and self.clock() - self.since >= (HOVER_DWELL_SEC if sec is None else sec)


class SelectionWatch:
    """선택 입력이 감지된 뒤 선택지 패널(64x36 gray)을 지켜보며 '정말 선택돼서 버튼이 사라졌는지' 판정한다.
    feed(cur) 결과: "confirm"(변화 2회 연속 + 버튼 사라짐), "release"(버튼이 새로 나타났거나 일정 시간 변화 없음 = 선택 아님), None(계속 관찰)."""

    def __init__(self, base, clock=time.monotonic):
        self.base, self.clock, self.t0 = base, clock, clock()
        self.base_energy, self.hits = panel_energy(base), 0

    def feed(self, cur):
        d = panel_diff(self.base, cur)
        if d is None:
            return None
        if d <= TRANSITION_DIFF:
            self.hits = 0
            return "release" if self.clock() - self.t0 >= NO_REACTION_RELEASE_SEC else None
        self.hits += 1
        if self.hits < 2:
            return None
        e = panel_energy(cur)
        if self.base_energy > 1.0 and e <= self.base_energy * BUTTONS_GONE_RATIO:
            return "confirm"
        if e >= max(self.base_energy / BUTTONS_GONE_RATIO, self.base_energy + 2.0):
            return "release"        # 클릭 뒤에 오히려 버튼이 나타남 = 대사를 넘긴 것
        return None


class ChoiceLock:
    """선택지를 고른 뒤 '완료된 이벤트' 제목을 잠가 같은 이벤트를 다시 인식·표시하지 않게 한다.
    원작의 completed_event_title / is_waiting_for_next_event / LOCK_RECOVERY_MAX_WAIT_SEC 과 같은 규칙:
    - 잠긴 동안 유사도 0.70 이상인 제목은 무시, 다른 제목이 보이거나 로딩 화면이 오면 해제
    - MAX_WAIT_SEC 동안 해제되지 않으면 '선택이 안 먹힌 것'으로 보고 해제(호출 측이 상태 초기화 후 재인식)"""
    SAME_RATIO = 0.70
    MAX_WAIT_SEC = 8.0
    CONFIRMED_MAX_SEC = 60.0    # (경량판 추가) 화면 전환으로 선택이 확정된 잠금은 더 오래 유지 - 결과 대화가 길어도 재표시 안 함

    def __init__(self, clock=time.monotonic):
        self.clock, self.title, self.since, self.confirmed, self.key = clock, "", None, False, ""

    def lock(self, title, key=""):
        """key: 잠글 때 매칭돼 있던 DB 이벤트의 cleanTitle. 비슷한 이름의 '다른' 이벤트를 구분하는 데 쓴다."""
        self.title, self.since, self.confirmed, self.key = title or "", self.clock(), False, key or ""

    def confirm(self):
        if self.active:
            self.confirmed = True

    def release(self):
        self.title, self.since, self.confirmed, self.key = "", None, False, ""

    @property
    def active(self):
        return bool(self.title)

    def expired(self):
        limit = self.CONFIRMED_MAX_SEC if self.confirmed else self.MAX_WAIT_SEC
        return self.active and self.clock() - self.since >= limit

    def blocks(self, titles):
        """titles(문자열 또는 후보 리스트) 중 하나라도 잠긴 제목과 같은 이벤트로 보이면 True."""
        if not self.active:
            return False
        lst = [titles] if isinstance(titles, str) else list(titles or [])
        lk = self.title.replace(" ", "")
        return any(t and difflib.SequenceMatcher(None, t.replace(" ", ""), lk).ratio() >= self.SAME_RATIO for t in lst)


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
    date_changed = pyqtSignal(object)    # 좌측 상단 날짜로 확정한 일정 항목(jh_schedule 튜플) 또는 None
    arcana_screen_changed = pyqtSignal(bool)   # '아르카나 편성 화면'인가 (구원자 추천 툴팁은 이 화면에서만)
    savior_changed = pyqtSignal(object)  # '여정 시작' 화면에서 식별한 구원자 리스트(동명이인이면 여러 명) 또는 None
    model_failed = pyqtSignal(str)       # OCR 모델 로딩 실패 (연속 실패 중 한 번만 보냄)

    LEAVE_AFTER_OTHER = 3     # 라벨 자리에 다른 글자가 읽힘 -> 이벤트 화면이 아니라고 확신 (연속 횟수)
    LEAVE_AFTER_UNSEEN = 8    # 아무것도 못 읽음 -> 스캔 실패일 수 있으니 더 오래 기다림
    LEAVE_GRACE_SEC = 1.5     # 위 횟수를 채워도 이만큼 계속 안 보여야 이탈 확정 (깜빡임 디바운스)
    REENTRY_MEMO_SEC = 4.0    # 이탈 직후 이 시간 안에 같은 제목 이미지가 다시 보이면 OCR 생략

    def __init__(self, pause_getter=None, low_spec_getter=None, lang_getter=None, savior_getter=None):
        super().__init__()
        self.savior_getter = savior_getter or (lambda: False)       # show_savior_reco
        self._saviors = None                                        # jh_savior.load_reco() (지연 로딩)
        self._sv_next = 0.0
        self._sv_reset()
        self.lang_getter = lang_getter or (lambda: "ko")
        self._last_rect = None
        self.pause_getter = pause_getter or (lambda: False)
        self.low_spec_getter = low_spec_getter or (lambda: False)
        self.ocr = None
        self._ocr_retry = RetryBackoff()
        self._ocr_failed_notified = False
        self._stop = False
        # (2) 실제로 확인되기 전까지는 항상 False
        self.label_visible = False
        self.on_event_screen = False
        self._reverify = True
        self._was_inactive = False
        self._grace = LeaveGrace(self.LEAVE_GRACE_SEC)
        self._memo = TitleMemo(self.REENTRY_MEMO_SEC)
        self._date_gray, self._date_next = None, 0.0
        self._date_cand, self._date_streak, self._date_miss, self._date_row = None, 0, 0, None
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
        self._sv_clear("(화면 재확인)")

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

    def _sv_reset(self):
        self._sv_cur = None                  # 확정(emit)한 구원자 키 튜플
        self._sv_cand, self._sv_streak = None, 0
        self._sv_gray = None                 # (부제 gray, 이름 gray) 직전 이미지
        self._sv_last = None                 # 직전 식별 결과 (matches, how)
        self._sv_miss = self._sv_noread = 0
        self._ar_on, self._ar_streak, self._ar_miss = False, 0, 0

    def _sv_clear(self, why=""):
        """구원자 인식 상태를 비우고, 켜져 있었으면 해제를 알린다."""
        was, was_ar = self._sv_cur is not None, self._ar_on
        self._sv_reset()
        if was_ar:
            self.arcana_screen_changed.emit(False)
        if was:
            log(f"[구원자] 인식 해제 {why}")
            self.savior_changed.emit(None)

    def _grab_gray(self, sct, rect, name):
        return cv2.cvtColor(np.array(sct.grab(region_to_monitor(REGION_FRACTIONS[name], rect))), cv2.COLOR_BGRA2GRAY)

    def _read_region(self, sct, rect, name, scales=(2.0, 1.6)):
        gray = self._grab_gray(sct, rect, name)
        return [self.ocr.read_line(cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)) for sc in scales]

    def _scan_arcana_screen(self, sct, rect):
        """아르카나 '편성' 화면인지 판별한다 (구원자 추천 툴팁은 이 화면에서만).
        - 부정 신호(아르카나 고르기 화면: 하단 '동일한 타이틀' 안내줄 / 우측 상단 보유 수 '78/81')가 한 번이라도 읽히면 즉시 해제.
        - 긍정 신호는 변하지 않는 고정 글자만(프리셋 이름은 사용자가 바꿀 수 있어 쓰지 않는다): '전체 해제'/'자동 편성' 버튼 하나,
          또는 '특수재화 우선 사용' + '여정 시작' 버튼 둘 다. 연속 ARCANA_CONFIRM 회 읽혀야 확정, 연속 ARCANA_LOST 회 못 읽어야 해제."""
        def read(name, scales=(2.0,)):
            return self._read_region(sct, rect, name, scales)

        if any(c >= 0.5 and jh_savior.match_picker(t, "count") for t, c in read("picker_count")) or                 any(c >= 0.5 and jh_savior.match_picker(t, "guide") for t, c in read("picker_guide")):
            self._ar_streak, self._ar_miss = 0, ARCANA_LOST
            if self._ar_on:
                self._ar_on = False
                log("[구원자] 아르카나 고르기 화면 글자 감지 -> 추천 툴팁 즉시 숨김")
                self.arcana_screen_changed.emit(False)
            return
        found = set()
        for name in ("arcana_special", "arcana_start"):
            if any(c >= 0.6 and jh_savior.match_arcana_screen(t, jh_savior.ARCANA_KINDS[name]) for t, c in read(name)):
                found.add(jh_savior.ARCANA_KINDS[name])
        if not jh_savior.arcana_screen_hit(found):
            for name in ("arcana_btn_clear", "arcana_btn_auto"):
                if any(c >= 0.4 and jh_savior.match_arcana_screen(t, jh_savior.ARCANA_KINDS[name]) for t, c in read(name, (2.0, 3.0))):
                    found.add(jh_savior.ARCANA_KINDS[name])
        if jh_savior.arcana_screen_hit(found):
            self._ar_miss = 0
            self._ar_streak += 1
            if self._ar_streak >= ARCANA_CONFIRM and not self._ar_on:
                self._ar_on = True
                log(f"[구원자] 아르카나 편성 화면 확인 {sorted(found)} -> 추천 툴팁 허용")
                self.arcana_screen_changed.emit(True)
        else:
            self._ar_streak = 0
            self._ar_miss += 1
            if self._ar_on and self._ar_miss >= ARCANA_LOST:
                self._ar_on = False
                log("[구원자] 편성 화면 글자가 안 보임 -> 추천 툴팁 숨김")
                self.arcana_screen_changed.emit(False)

    def request_arcana_recheck(self):
        """마우스 클릭 직후: 편성 화면 확정을 풀고(툴팁은 호출 측이 이미 숨김) 다음 루프에서 바로 다시 판정한다(1초 대기 건너뜀).
        다음 스캔에서 긍정 신호가 한 번만 읽혀도 확정(재표시), 부정/불확실이면 숨김 유지."""
        self._ar_on = False
        self._ar_streak = max(0, ARCANA_CONFIRM - 1)
        self._ar_miss = 0
        self._sv_next = 0.0

    def _scan_savior(self, sct, rect):
        """'여정 시작' 머리글이 보이면 [부제][이름] 줄을 읽어 구원자를 식별한다(1초 간격). 이벤트 화면에서는 호출하지 않는다."""
        now = time.monotonic()
        if now < self._sv_next:
            return
        self._sv_next = now + SAVIOR_SCAN_SEC
        if self._saviors is None:
            self._saviors = jh_savior.load_reco()
            log(f"[구원자] 추천 데이터 {len(self._saviors)}명 로드")
        if not self._saviors:
            return
        text, conf = self.ocr.read_line(cv2.resize(self._grab_gray(sct, rect, "savior_header"), None, fx=2.0, fy=2.0,
                                                   interpolation=cv2.INTER_CUBIC))
        if not (conf >= 0.4 and jh_savior.match_header(text)):
            self._sv_miss += 1
            self._sv_cand, self._sv_streak = None, 0
            if self._sv_miss >= SAVIOR_LOST:
                self._sv_clear("('여정 시작' 화면이 아님)")
            return
        self._sv_miss = 0
        self._scan_arcana_screen(sct, rect)
        sub_g, name_g = self._grab_gray(sct, rect, "savior_subtitle"), self._grab_gray(sct, rect, "savior_name")
        last = self._sv_gray
        same = (last is not None and last[0].shape == sub_g.shape and last[1].shape == name_g.shape
                and cv2.absdiff(sub_g, last[0]).mean() < 1.0 and cv2.absdiff(name_g, last[1]).mean() < 1.0)
        if not same or self._sv_last is None:
            self._sv_gray = (sub_g, name_g)
            subs = read_title_candidates(self.ocr, sub_g, min_conf=0.3)
            names = read_title_candidates(self.ocr, name_g, min_conf=0.3)
            self._sv_last = (jh_savior.identify(self._saviors, subs, names), subs, names)
        (matches, how), subs, names = self._sv_last
        if not matches:
            self._sv_noread += 1
            self._sv_cand, self._sv_streak = None, 0
            if self._sv_noread >= SAVIOR_NOREAD_LOST:
                self._sv_clear(f"(이름을 읽을 수 없음 부제={subs} 이름={names})")
            return
        self._sv_noread = 0
        key = jh_savior.savior_key(matches)
        self._sv_streak = self._sv_streak + 1 if key == self._sv_cand else 1
        self._sv_cand = key
        if self._sv_streak >= SAVIOR_CONFIRM and key != self._sv_cur:
            self._sv_cur = key
            log(f"[구원자] 식별 {how}: 부제={subs} 이름={names} -> {[(m['subtitle'], m['name']) for m in matches]}")
            self.savior_changed.emit(list(matches))

    def _scan_date(self, sct, rect):
        """좌측 상단 날짜 문구('3월 초순')를 읽어 일정 항목으로 바꾼다. 같은 값이 연속 DATE_CONFIRM 번 나와야 확정, 연속 DATE_LOST 번 못 읽으면 None."""
        now = time.monotonic()
        if now < self._date_next:
            return
        self._date_next = now + DATE_SCAN_SEC
        gray = cv2.cvtColor(np.array(sct.grab(region_to_monitor(REGION_FRACTIONS["schedule_date"], rect))), cv2.COLOR_BGRA2GRAY)
        last = self._date_gray
        if last is not None and last.shape == gray.shape and cv2.absdiff(gray, last).mean() < 1.0:
            if self._date_cand is not None:                 # 화면이 그대로면 다시 읽지 않고, 직전 읽기를 한 번 더 확인한 것으로 센다
                self._date_streak += 1
                self._confirm_date(self._date_cand, "(변화 없음)")
            return
        self._date_gray = gray
        row, text = None, ""
        for sc in (2.0, 1.6):
            g = cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_CUBIC)
            text, conf = self.ocr.read_line(g)
            if conf >= 0.6:
                row = jh_schedule.find_turn(text)
                if row:
                    break
        if row is None:
            self._date_miss += 1
            self._date_cand, self._date_streak = None, 0
            if self._date_miss >= DATE_LOST and self._date_row is not None:
                self._date_row = None
                log(f"[일정] 날짜를 읽을 수 없음 ('{text}') -> 턴 정보 해제")
                self.date_changed.emit(None)
            return
        self._date_miss = 0
        if row is self._date_cand:
            self._date_streak += 1
        else:
            self._date_cand, self._date_streak = row, 1
        self._confirm_date(row, text)

    def _confirm_date(self, row, text):
        if self._date_streak >= DATE_CONFIRM and row is not self._date_row:
            self._date_row = row
            log(f"[일정] 날짜 인식 '{text}' -> 턴 {row[0]} ({row[1]}, {row[2]})")
            self.date_changed.emit(row)

    def _ensure_ocr(self):
        """OCR 모델을 (필요하면) 로드한다. 실패하면 백오프 간격으로만 재시도하고 상단바에는 한 번만 알린다. 사용 가능하면 True."""
        if self.ocr is not None:
            return True
        if not self._ocr_retry.ready():
            return False
        first = self._ocr_retry.fails == 0
        if first:
            self.model_loading.emit()
            log("[지연 로딩] 게임 감지됨 -> 제목/라벨 인식용 OCR 모델 로딩 시작...")
        try:
            self.ocr = KoreanOCR()
        except Exception as e:
            n = self._ocr_retry.failed()
            if n <= 3:
                log(f"[OCR 모델 로딩 실패 {n}회] {e}\n{traceback.format_exc()}")
            else:
                log(f"[OCR 모델 로딩 실패 {n}회] {e}")
            if not self._ocr_failed_notified:
                self._ocr_failed_notified = True
                self.model_failed.emit(str(e))
            return False
        self._ocr_retry.success()
        self._ocr_failed_notified = False
        log("[지연 로딩] 제목/라벨 인식용 OCR 모델 로딩 완료")
        self.model_ready.emit()
        return True

    def run(self):
        sct = mss.mss()
        try:
            self._run_loop(sct)
        finally:
            try:
                sct.close()
            except Exception:
                pass

    def _run_loop(self, sct):
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
                    self._sv_clear("(게임 창 없음)")
                    self._date_gray = None
                    self._was_inactive = False
                    self.msleep(1000)
                    continue
                if self._was_inactive:
                    self._was_inactive = False
                    self._last_label_gray = self._last_title_gray = None
                if not self._ensure_ocr():
                    self.msleep(500)
                    continue
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

                self._scan_date(sct, rect)
                state = self._scan_label(sct, rect)
                if state == "found" or self.on_event_screen or not self.savior_getter():
                    self._sv_clear("(이벤트 화면 또는 설정 꺼짐)")      # 이벤트 화면에서는 구원자 툴팁을 쓰지 않는다
                else:
                    self._scan_savior(sct, rect)
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


def save_card_debug(img, keep=5):
    """카드 식별 실패 시 잘라낸 영역을 앱 폴더/data/card_debug 에 저장(최근 keep 장만 유지) - 영역 보정 진단용."""
    try:
        d = os.path.join(get_settings_dir(), "card_debug")
        os.makedirs(d, exist_ok=True)
        ok, buf = cv2.imencode(".png", img)
        if ok:
            buf.tofile(os.path.join(d, time.strftime("%H%M%S") + ".png"))
        for f in sorted(os.listdir(d))[:-keep]:
            os.remove(os.path.join(d, f))
    except Exception:
        pass


class CardThread(QThread):
    """제목이 여러 아르카나에 있을 때 카드 썸네일로 어느 아르카나인지 식별 (참조 이미지 다운로드가 있어 별도 스레드)."""
    resolved = pyqtSignal(str, object, str)      # (제목, arcanaId 또는 None, 로그 문구)

    def __init__(self, title, ids, save_debug=False):
        super().__init__()
        self.title, self.ids, self.save_debug = title, list(ids), save_debug

    def run(self):
        aid, info = None, ""
        try:
            with mss.mss() as sct:                      # 닫지 않으면 스레드마다 GDI 핸들이 샌다

                last = [None]

                def grab():
                    rect = get_game_window_rect()
                    last[0] = grab_card_crop(sct, rect) if rect else None
                    return last[0]

                aid, score, margin, tries = jh_cards.identify_stable(grab, self.ids)
            info = f"score={score:.2f} margin={margin:.2f} tries={tries} 후보점수={ {k: round(v, 2) for k, v in jh_cards.LAST_SCORES.items()} }"
            if aid is None and last[0] is not None and self.save_debug:     # 진단 모드(show_capture_debug)에서만 저장 -> 평소엔 용량을 쓰지 않음
                save_card_debug(last[0])
        except Exception as e:
            info = f"예외 {e}"
        self.resolved.emit(self.title, aid, info)


class ThumbThread(QThread):
    """구원자 추천 툴팁의 아르카나 썸네일을 지연 다운로드해 카드 캐시(jh_cards)에 저장한다. 실패해도 텍스트만 표시된다."""
    done = pyqtSignal()

    def __init__(self, ids):
        super().__init__()
        self.ids = list(dict.fromkeys(ids))

    def run(self):
        try:
            store = jh_cards.default_store()
            store.on_event = log
            fails = 0
            for aid in self.ids:
                if jh_cards.cached_path(aid):
                    continue
                if store.get_image(aid) is None:
                    fails += 1
                    if fails >= jh_net.BREAKER_THRESHOLD or not store.breaker.allow(store.clock()):
                        log("[구원자] 썸네일 다운로드 연속 실패, 일괄 다운로드 중단")
                        break
                else:
                    fails = 0
        except Exception as e:
            log(f"[구원자] 썸네일 다운로드 예외 {e}")
        self.done.emit()


# ----------------------------------------------------------------------------
# DB 로딩 스레드
# ----------------------------------------------------------------------------
def _seed_in_use(store):
    """직전 load 가 번들 시드를 썼으면 True (로그 한 줄도 남김)"""
    if getattr(store, "using_seed", False) is True:
        log("[DB] 번들 시드 사용(사이트 갱신 대기)")
        return True
    return False


class DbThread(QThread):
    finished_ok = pyqtSignal(object, str, str)      # cache(list), status, message

    def __init__(self, store, manual=False):
        super().__init__()
        self.store, self.manual = store, manual

    def _build_cache(self):
        journey, arcanas = self.store.load()
        if not journey:
            return []
        v2 = None
        try:
            load_v2 = getattr(self.store, "load_v2", None)
            v2 = load_v2() if load_v2 else None          # V2 보충(없거나 깨졌으면 None -> 가이드 DB 만 사용)
        except Exception as e:
            log(f"[V2 로딩 예외] {e}")
        if v2 is None:
            return jh_db.build_event_cache(journey, arcanas)
        try:
            return jh_db.build_event_cache(journey, arcanas, v2=v2)
        except Exception as e:
            if v2 is None:
                raise
            log(f"[V2 반영 예외, 가이드 DB 만 사용] {e}\n{traceback.format_exc()}")
            return jh_db.build_event_cache(journey, arcanas)

    def work(self, emit):
        """캐시를 먼저 내보내 바로 쓰게 하고, 네트워크 갱신은 그 뒤에 한다. 갱신 예외가 유효한 캐시를 버리게 하지 않는다."""
        sent_cache = False
        if not self.manual:
            try:
                cache = self._build_cache()
                if cache:
                    if _seed_in_use(self.store):
                        emit(cache, "seed", "번들 시드 사용 (사이트 갱신 대기)")
                    else:
                        emit(cache, "cache", "저장된 DB 사용 (백그라운드에서 갱신 확인)")
                    sent_cache = True
            except Exception as e:
                log(f"[DB 캐시 로딩 예외] {e}\n{traceback.format_exc()}")
        status, message, changed = "error", "", False
        try:
            res = self.store.update(manual=self.manual)
            status, message, changed = res.status, res.message, bool(res.changed)
            if status in ("offline", "capped"):          # 실제 시도 실패/상한 도달 때만 기록 (backoff 대기 중 호출은 조용히)
                log(f"[DB] {status}: {message}")
        except Exception as e:
            log(f"[DB 갱신 예외] {e}\n{traceback.format_exc()}")
            status, message = "offline", str(e)
        try:                                        # V2 보충 데이터 갱신(백그라운드, 실패는 로그만 남기고 캐시 유지)
            upd_v2 = getattr(self.store, "update_v2", None)
            r2 = upd_v2(manual=self.manual and status != "cooldown") if upd_v2 else None
            if r2 is None:
                pass
            elif r2.status in ("updated", "offline"):
                log(f"[V2] {r2.status}: {r2.message}")
            changed = changed or bool(r2 and r2.changed)
        except Exception as e:
            log(f"[V2 갱신 예외] {e}\n{traceback.format_exc()}")
        if sent_cache and not changed:
            return                                  # 이미 캐시를 보냈고 바뀐 것이 없음
        try:
            cache = self._build_cache()
        except Exception as e:
            log(f"[DB 로딩 예외] {e}\n{traceback.format_exc()}")
            cache = []
        if not cache and sent_cache:
            return                                  # 갱신 뒤 읽기에 실패해도 이미 쓰고 있는 캐시는 유지
        if cache and status != "cooldown" and _seed_in_use(self.store):
            status, message = "seed", "번들 시드 사용 (사이트 갱신 대기): " + message
        emit(cache, status, message)

    def run(self):
        self.work(self.finished_ok.emit)


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
        self.avoid_regions = False       # show_capture_debug 일 때 True
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
        if off and self.avoid_regions and rect and any(
                boxes_overlap((left + off[0], top + off[1], left + off[0] + self.width(), top + off[1] + self.height()), b)
                for b in capture_region_boxes(rect)):
            off = None                      # 디버그 모드: 캡처되는 OCR 영역을 가리는 위치면 기본 위치(맨 위)로
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


def join_threads(threads, total_ms):
    """QThread 들을 최대 total_ms(밀리초) 동안 기다린다. 아직 실행 중인 것들의 목록을 돌려준다."""
    deadline = time.monotonic() + total_ms / 1000.0
    stuck = []
    for t in threads:
        try:
            if not t.isRunning():
                continue
            left = max(0, int((deadline - time.monotonic()) * 1000))
            if not t.wait(left):
                stuck.append(t)
        except Exception:
            pass
    return stuck


def capture_region_boxes(rect):
    """앱이 OCR/카드 식별로 읽는 영역(라벨·제목·카드)의 화면 좌표 상자 [(x0,y0,x1,y1)]."""
    boxes = []
    for fr in (REGION_FRACTIONS["event_label"], REGION_FRACTIONS["event_title"]):
        x0, y0 = rect["left"] + rect["width"] * fr["left"], rect["top"] + rect["height"] * fr["top"]
        boxes.append((x0, y0, x0 + rect["width"] * fr["width"], y0 + rect["height"] * fr["height"]))
    cx0, cy0, cx1, cy1 = jh_cards.CARD_REGION_FRAC
    boxes.append((rect["left"] + rect["width"] * cx0, rect["top"] + rect["height"] * cy0,
                  rect["left"] + rect["width"] * cx1, rect["top"] + rect["height"] * cy1))
    return boxes


def boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def tooltip_below_regions_y(rect):
    """캡처 영역 아래쪽 끝(여백 포함). show_capture_debug 일 때 툴팁은 이보다 아래에만 둔다."""
    return int(max(b[3] for b in capture_region_boxes(rect)) + rect["height"] * 0.01)


def pick_screen_geometry(rect, geoms):
    """geoms: [(left, top, right, bottom)] 모니터 영역들. 게임 창 중심이 들어 있는 모니터를, 없으면 가장 많이 겹치는 모니터를 고른다."""
    if not geoms:
        return None
    if not rect:
        return geoms[0]
    cx, cy = rect["left"] + rect["width"] / 2, rect["top"] + rect["height"] / 2
    for g in geoms:
        if g[0] <= cx < g[2] and g[1] <= cy < g[3]:
            return g
    box = (rect["left"], rect["top"], rect["left"] + rect["width"], rect["top"] + rect["height"])

    def overlap(g):
        return max(0, min(g[2], box[2]) - max(g[0], box[0])) * max(0, min(g[3], box[3]) - max(g[1], box[1]))
    return max(geoms, key=overlap)


def game_monitor_avail(rect):
    """게임 창이 있는 모니터의 사용 가능 영역 (left, top, right, bottom). Qt 를 못 쓰면 주 화면."""
    geoms = []
    try:
        for sc in QApplication.screens():
            g = sc.availableGeometry()
            geoms.append((g.left(), g.top(), g.left() + g.width(), g.top() + g.height()))
    except Exception:
        pass
    if not geoms:
        g = QApplication.desktop().screenGeometry()
        geoms = [(g.left(), g.top(), g.left() + g.width(), g.top() + g.height())]
    return pick_screen_geometry(rect, geoms)


def place_tooltip(rect, avail, height, avoid_regions=False, y_frac=0.30, x_frac=None, min_top_frac=None):
    """툴팁의 (x, y, 잘라낼 높이 또는 None). avail=(left, top, right, bottom): 게임 창이 있는 모니터의 사용 가능 영역.
    avoid_regions=True(show_capture_debug)면 툴팁이 캡처에 찍혀도 라벨/제목/카드 영역을 가리지 않게 그 아래에만 두고,
    넘치면 위로 밀지 않고 아래를 자른다."""
    al, at, ar, ab = avail
    if rect:
        x = rect["left"] + (int(rect["width"] * x_frac) if x_frac is not None else 25)
        y = rect["top"] + int(rect["height"] * y_frac)
    else:
        x, y = al + 25, at + int((ab - at) * y_frac)
    if avoid_regions and rect:
        y = max(y, tooltip_below_regions_y(rect))
        if y + height + 10 > ab:
            return x, y, max(60, ab - 10 - y)
        return x, y, None
    top = at
    if rect and x_frac is not None:                  # 구원자 추천 툴팁: 넘쳐도 상단바(창 top + 높이*TOOLTIP_MIN_TOP_FRAC) 위로는 올리지 않는다
        top = max(at, rect["top"] + int(rect["height"] * (TOOLTIP_MIN_TOP_FRAC if min_top_frac is None else min_top_frac)))
    bottom = ab
    if rect and x_frac is not None:
        bottom = min(ab, rect["top"] + rect["height"] - 6)
    return x, max(top, min(y, bottom - height - 10)), None


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
        self.avoid_regions = False       # show_capture_debug: 캡처에 찍혀도 OCR 영역을 가리지 않게
        self.set_scale(scale)
        exclude_from_capture(self)

    def set_scale(self, scale):
        self.scale = scale
        self.label.setStyleSheet(
            f"QLabel {{ background-color: rgba(10,12,18,250); border: 2px solid #00e5ff; border-radius: 12px; "
            f"color: #ffffff; font-size: {max(9, round(self.BASE_PX * scale))}px; font-weight: bold; padding: 14px 18px; }}")
        self.setFixedWidth(round(self.BASE_W * scale))

    def _fit(self, html):
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.label.setText(html)
        self.label.adjustSize()
        self.adjustSize()
        self.resize(self.sizeHint())
        hfw = self.label.heightForWidth(self.width())          # 표/이미지가 들어간 리치텍스트는 sizeHint 가 작게 나올 수 있어 높이를 다시 확인
        if hfw > self.height():
            self.resize(self.width(), hfw)

    def show_html(self, html, y_frac=None, x_frac=None, width_frac=None, min_top_frac=None, width_px=None):
        """html: 문자열 또는 detail(0~MAX_DETAIL) -> 문자열 함수. 함수면 화면에 들어올 때까지 단계적으로 줄여 본다."""
        rect = get_game_window_rect()
        avail = game_monitor_avail(rect)
        limit = avail[3] - avail[1] - 40
        yf = y_frac if y_frac is not None else 0.30
        if y_frac is not None and rect:
            # place_tooltip 은 넘치면 위로 밀 수 있으므로, 상단바 아래(창 top + 높이*TOOLTIP_MIN_TOP_FRAC)까지 올라가는 것까지 허용해 한도를 잡는다
            bottom = min(avail[3], rect["top"] + rect["height"] - 6)              # 게임 창 아래끝(작업표시줄/다른 창 영역)도 넘지 않게
            mt = TOOLTIP_MIN_TOP_FRAC if min_top_frac is None else min_top_frac
            limit = bottom - 10 - (rect["top"] + int(rect["height"] * min(yf, mt)))
        if self.avoid_regions and rect:
            limit = avail[3] - 10 - max(rect["top"] + int(rect["height"] * yf), tooltip_below_regions_y(rect))
        want_w = width_px or (round(rect["width"] * width_frac) if (width_frac and rect) else round(self.BASE_W * self.scale))
        if self.width() != want_w:                       # 넓은 모드(2세트)와 보통 폭을 호출마다 바꾼다
            self.setFixedWidth(want_w)
            self._src = None
        if callable(html):
            fn = html
            html = fn(0)
            if html != self._src:                       # 원본이 같으면 이미 맞춰 둔 크기/내용을 그대로 쓴다
                self._src = html
                self._fit(html)
                for d in range(1, getattr(fn, "max_detail", TOOLTIP_MAX_DETAIL) + 1):
                    if self.height() <= limit:
                        break
                    self._fit(fn(d))
        elif html != self._src:
            self._src = html
            self._fit(html)
        x, y, clip = place_tooltip(rect, avail, self.height(), self.avoid_regions, yf, x_frac, min_top_frac)
        if clip:
            self.setFixedHeight(clip)                   # 디버그 모드에서 넘치면 아래를 자른다
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
        self.stuck_threads = []
        self._sct = None
        self.choice_lock = ChoiceLock()
        self._prev_keys = set()
        self._click_block_until = 0.0
        self._watch = None
        self._pending_hide = False
        self._dwell = HoverDwell()
        self.arcana_screen = False          # '아르카나 편성 화면'일 때만 추천 툴팁을 띄운다 (구원자 고르기/인자작 화면 제외)
        self.savior_matches = None          # '여정 시작' 화면에서 식별한 구원자(들) (jh_savior)
        self._thumb_threads = []
        self._thumb_tried = set()
        self.schedule_row = None            # 좌측 상단 날짜로 인식한 현재 턴의 일정 항목 (jh_schedule)
        self._transition_sct = None
        self._transition_timer = None
        self.reset_event_state()

        self.status = StatusBadge(self.settings)
        self.status.on_menu = self.show_main_menu
        self.status.on_settings = self.open_settings
        self.tooltip = FixedLeftTooltip(self.settings["tooltip_font_scale"])
        self._apply_capture_visibility()
        self._build_tray()

        self.watcher = ScreenWatcher(pause_getter=lambda: self._hidden, low_spec_getter=lambda: self.settings["low_spec_mode"],
                                     lang_getter=lambda: self.lang,
                                     savior_getter=lambda: bool(self.settings.get("show_savior_reco", True)))
        self.watcher.savior_changed.connect(self.on_savior_changed)
        self.watcher.arcana_screen_changed.connect(self.on_arcana_screen_changed)
        self.watcher.title_detected.connect(self.on_title)
        self.watcher.journey_status_changed.connect(self.on_journey_status)
        self.watcher.date_changed.connect(self.on_date_changed)
        self.watcher.model_loading.connect(
            lambda: self.status.set_status(tr("🟡 [OCR 모델 로딩 중] 처음 한 번만 몇 초 걸려요..."), "#ffcc00", flash=True))
        self.watcher.model_failed.connect(
            lambda msg: self.status.set_status(tr("🔴 [OCR 모델 로딩 실패] 잠시 후 자동으로 다시 시도합니다 (log.txt 확인)"), "#ff5c5c",
                                               protect_sec=8, flash=True))
        self.watcher.model_ready.connect(lambda: self.status.set_status(self._idle_text(), "#888888"))
        self.watcher.start()
        log(f"[시작] {APP_NAME} {APP_VERSION} lang={self.lang} low_spec={self.settings['low_spec_mode']}")

        for interval, fn in ((60, self.check_hover), (300, self.poll_loading), (1000, self.maintain_overlays)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(interval)
        self._transition_timer = QTimer(self)                # 선택 후 화면 전환 확인 (원작: 120ms 간격)
        self._transition_timer.timeout.connect(self._poll_transition)

        self.status.set_status(tr("🟡 [DB 불러오는 중] 이벤트 데이터를 가져오는 중..."), "#ffcc00")
        self.start_db(manual=False)

    # --- 상태 ---
    def reset_event_state(self, keep_journey=False):
        """이벤트 필드/툴팁을 비운다. keep_journey=True 면 '이벤트 화면 안' 상태는 유지한다
        (선택 확정 뒤 라벨이 계속 보이면 워처가 진입 신호를 다시 보내지 않으므로, 다음 이벤트 툴팁이 막히지 않게)."""
        if not keep_journey:
            self.in_journey = False
        self.current_title = ""
        self.matched = None
        self.not_found = ""
        self.ambig = []
        self.zones = []
        self.options = []
        self.card_id = None
        self._pending_hide = False
        self._card_threads = getattr(self, "_card_threads", [])
        self._stop_transition_check()
        if hasattr(self, "tooltip"):
            self.tooltip.hide_and_clear()

    def manual_reset(self):
        """메뉴 '인식 상태 초기화' (원작: 상단 배지 클릭 새로고침). 잠금·이벤트 상태를 비우고 라벨부터 다시 확인한다."""
        log("[수동 새로고침] 추적 중이던 이벤트/선택 잠금 초기화")
        self.choice_lock.release()
        self.reset_event_state()
        self.watcher.request_reverify()
        self.status.set_status(tr("🔄 [새로고침됨] 여정 이벤트 화면을 다시 찾고 있습니다..."), "#888888", flash=True)

    # --- 선택지 선택 감지 (원작 on_choice_selected_action / _poll_click_transition / 잠금 자동 복구) ---
    def _read_input_keys(self):
        try:
            gaks = ctypes.windll.user32.GetAsyncKeyState
            raw = {vk for vk in (VK_LBUTTON, *CHOICE_KEYS) if gaks(vk) & 0x8000}
            if not raw:
                return raw
            return filter_game_inputs(raw, is_game_foreground(), is_cursor_over_game())
        except Exception:
            return set()

    def _zone_index_at(self, pos, rect):
        x0, x1 = (rect["left"] + int(rect["width"] * self.HOVER_X[0]), rect["left"] + int(rect["width"] * self.HOVER_X[1])) \
            if rect else (int(self.screen_w * self.HOVER_X[0]), int(self.screen_w * self.HOVER_X[1]))
        if x0 <= pos.x() <= x1:
            for z in self.zones:
                if z["y_min"] <= pos.y() <= z["y_max"]:
                    return z["idx"]
        return None

    def _capture_choice_panel(self):
        """선택지 버튼 영역(가로 0.64~0.95, 세로 선택지 범위)을 64x36 gray 로 축소 캡처 (원작과 같은 크기)."""
        try:
            rect = get_game_window_rect()
            if not rect or not self.zones:
                return None
            if self._transition_sct is None:
                self._transition_sct = mss.mss()
            y0, y1 = min(z["y_min"] for z in self.zones), max(z["y_max"] for z in self.zones)
            x0, x1 = rect["left"] + int(rect["width"] * self.HOVER_X[0]), rect["left"] + int(rect["width"] * self.HOVER_X[1])
            mon = {"left": int(x0), "top": int(y0), "width": max(1, int(x1 - x0)), "height": max(1, int(y1 - y0))}
            gray = cv2.cvtColor(np.array(self._transition_sct.grab(mon)), cv2.COLOR_BGRA2GRAY)
            return cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
        except Exception:
            return None

    def _stop_transition_check(self):
        if self._transition_timer is not None:
            self._transition_timer.stop()
        self._watch = None

    def on_choice_selected(self, idx):
        if idx is None or idx >= len(self.zones):
            return
        option = self.zones[idx]["option"]
        title = self.current_title or (self.matched or {}).get("rawTitle", "")
        log(f"[선택지 선택 감지] '{title}' {idx + 1}번 '{option.get('choice', '')}' -> 완료 이벤트 잠금")
        self.choice_lock.lock(title, (self.matched or {}).get("cleanTitle", ""))
        self._click_block_until = time.monotonic() + CLICK_BLOCK_SEC
        base = self._capture_choice_panel()
        self._pending_hide = not option.get("has_condition", True)
        if self._pending_hide:
            # 조건 없는 선택지: 툴팁만 숨기고 잠금은 '미확정(8초)'으로 둔다. 패널에서 버튼이 사라지는 것이 확인돼야 확정한다.
            log("[조건 없는 선택지] 항상 성공하는 선택지로 판단 -> 툴팁만 숨김 (화면 전환 확인 전에는 확정 안 함)")
            self.tooltip.hide_and_clear()
        self.status.set_status(tr("🔵 [선택 반응 확인 중] 화면 전환 여부 확인 중..."), "#00aaff")
        if base is not None and self._transition_timer is not None:
            self._watch = SelectionWatch(base)
            self._transition_timer.start(TRANSITION_POLL_MS)

    def _poll_transition(self):
        if not self.choice_lock.active or self._watch is None:
            self._stop_transition_check()
            return
        res = self._watch.feed(self._capture_choice_panel())
        if res == "confirm":
            log("[화면 전환 감지] 선택지 버튼이 사라짐 -> 선택 확정으로 판단, 정리")
            self.choice_lock.confirm()
            self.reset_event_state(keep_journey=True)
            self.status.set_status(self._idle_text(), "#888888")
        elif res == "release":
            log("[선택 오감지로 판단] 패널이 안 변했거나 선택지가 새로 나타남 -> 잠금 해제, 툴팁 유지")
            self.choice_lock.release()
            self._stop_transition_check()
            self._pending_hide = False
            self.status.set_status(self._journey_text() if self.in_journey else self._idle_text(), "#00aaff")

    def _check_lock_expiry(self):
        if self.choice_lock.expired():
            log(f"[잠긴 선택지 자동 복구] {ChoiceLock.MAX_WAIT_SEC:.0f}초간 다음 이벤트/로딩이 없음 -> 잠금 해제 후 재인식")
            self.choice_lock.release()
            self.reset_event_state()
            self.watcher.request_reverify()
            self.status.set_status(self._idle_text(), "#888888")

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
        had_db = bool(self.db_event_cache)
        if cache:
            customs = jh_db.load_custom_events(os.path.join(get_settings_dir(), jh_db.CUSTOM_EVENTS_FILE))
            self.db_event_cache = jh_db.merge_custom_events(cache, customs)
            log(f"[DB] 이벤트 {len(cache)}개 + 사용자 정의 {len(self.db_event_cache) - len(cache)}개 ({status}: {message})")
            text = tr("🟢 [DB 준비 완료] 이벤트 {}개").format(len(cache))
            if status == "seed":
                text = tr("🟡 [DB 준비 완료 - 번들 시드] 이벤트 {}개, 사이트 갱신 대기").format(len(cache))
            if manual and status == "cooldown":
                text = tr("⏳ 수동 새로고침은 잠시 후 다시 가능합니다 (10분 쿨다운)")
            elif manual and status == "offline":
                text = tr("⚠️ 갱신 실패 - 저장된 DB를 사용합니다")
            self.status.set_status(text, "#3ddc84", protect_sec=4, flash=True)
            QTimer.singleShot(4200, lambda: self.status.set_status(self._journey_text() if self.in_journey else self._idle_text(), "#888888"))
            if not had_db and self.current_title and not self.matched and not self.not_found and getattr(self, "_last_cands", None):
                # DB 가 늦게 로드됨: 그 사이 본 제목은 워처가 다시 보내지 않으므로 직접 다시 매칭한다
                log(f"[DB] 늦게 로드됨 -> 이미 본 제목 {self._last_cands} 다시 매칭")
                self.current_title = ""
                self.on_title(self._last_cands, getattr(self, "_last_kind", None))
        else:
            log(f"[DB] 없음 ({status}: {message})")
            self.status.set_status(tr("🔴 DB 없음 - 인터넷 연결 후 메뉴에서 새로고침"), "#ff5c5c", protect_sec=8 if manual else 0, flash=True)

    def refresh_db(self):
        self.start_db(manual=True)

    # --- 워처 콜백 ---
    def on_date_changed(self, row):
        self.schedule_row = row

    def _schedule_hover_html(self, rect, pos):
        """좌측 상단 날짜 위젯에 마우스가 올라가 있으면 현재 턴 일정 툴팁 HTML, 아니면 None."""
        if not (self.schedule_row and rect and self.settings.get("show_schedule", True)):
            return None
        l, t, r, b = SCHEDULE_HOVER
        x0, x1 = rect["left"] + int(rect["width"] * l), rect["left"] + int(rect["width"] * r)
        y0, y1 = rect["top"] + int(rect["height"] * t), rect["top"] + int(rect["height"] * b)
        if x0 <= pos.x() <= x1 and y0 <= pos.y() <= y1:
            return jh_schedule.format_schedule_html(self.schedule_row, self.settings["tooltip_font_scale"])
        return None

    def on_arcana_screen_changed(self, on):
        self.arcana_screen = bool(on)

    def on_savior_changed(self, matches):
        self.savior_matches = list(matches) if matches else None
        if not self.savior_matches:
            return
        m = self.savior_matches
        label = " / ".join(f"{x['name']}({x['subtitle']})" for x in m)
        self.status.set_status(tr("🃏 [구원자 인식] {}").format(label), "#00aaff", flash=True)
        ids = [i for s in m for i in jh_savior.thumb_ids(s)]
        key = jh_savior.savior_key(m)
        if ids and key not in self._thumb_tried:
            self._thumb_tried.add(key)
            t = ThumbThread(ids)
            t.done.connect(lambda: None)         # 다운로드가 끝나면(또는 하나 받을 때마다) 다음 호버 때 HTML 이 달라져 자동 반영된다
            self._thumb_threads = [x for x in self._thumb_threads if x.isRunning()] + [t]
            t.start()

    @staticmethod
    def _thumb_path(aid):
        try:
            return jh_cards.cached_path(aid)
        except Exception:
            return None

    def _savior_click_hold(self):
        """추천 툴팁이 떠 있는 동안 게임 위 왼쪽 클릭이 눌리면 SAVIOR_CLICK_HIDE_SEC 동안 True(툴팁 숨김) + 워처에 즉시 재판정 요청.
        선택지 감지(_read_input_keys/ChoiceLock)와 별개로 마우스 버튼만 본다."""
        now = time.monotonic()
        if not (self.arcana_screen and self.savior_matches):
            self._sv_click_prev = False
            return False
        try:
            down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) and bool(
                filter_game_inputs({VK_LBUTTON}, is_game_foreground(), is_cursor_over_game()))
        except Exception:
            down = False
        if down and not getattr(self, "_sv_click_prev", False):
            self._sv_hold_until = now + SAVIOR_CLICK_HIDE_SEC
            self.arcana_screen = False                      # 클릭하면 새 판독에서 편성 화면이 '긍정 확정'될 때까지 계속 숨긴다(재노출 깜빡임 방지)
            self.watcher.request_arcana_recheck()
        self._sv_click_prev = down
        return now < getattr(self, "_sv_hold_until", 0.0)

    def _savior_hover_html(self, rect, pos):
        """'여정 시작' 화면에서 구원자를 식별한 동안(호버 없이) 계속 보이는(아르카나 편성 화면에서만) 추천 아르카나 툴팁 HTML 함수(detail -> html), 아니면 None."""
        if not (self.savior_matches and rect and self.settings.get("show_savior_reco", True)) or self.in_journey or not self.arcana_screen:
            return None
        m = self.savior_matches
        wide = jh_savior.wide_mode(m)
        scale, width_px, cw = jh_savior.tooltip_geometry(rect, self.settings["tooltip_font_scale"], wide)   # 창 크기 비례 배율 k 반영
        fn = lambda d=0: jh_savior.format_html(m, scale, self._thumb_path, d, cw)
        fn.wide = wide
        fn.width_px = width_px
        fn.max_detail = jh_savior.MAX_DETAIL                       # 썸네일을 끝까지 유지하는 단계(0~2) 뒤 마지막 수단(3)까지
        return fn

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

    def _is_different_event(self, cands_txt, kind):
        """잠금과 글자는 비슷하지만 DB 에서 확실히 '다른' 이벤트로 매칭되는가 (예: 의뢰 성공 / 의뢰 대성공)."""
        key = self.choice_lock.key
        if not key or not self.db_event_cache:
            return False
        try:
            cands, amb = jh_db.match_title(cands_txt, self.db_event_cache, kind)
        except Exception:
            return False
        if not cands or len(amb) != 1:
            return False
        return amb[0].get("cleanTitle", "") != key

    def on_title(self, titles, kind=None):
        """titles: OCR 후보 문자열 리스트(또는 문자열 하나), kind: 라벨 종류 'arcana'/'general'/None"""
        cands_txt = [titles] if isinstance(titles, str) else list(titles or [])
        title = cands_txt[0] if cands_txt else ""
        if title == self.current_title:
            return
        self._last_cands, self._last_kind = cands_txt, kind
        if self.choice_lock.active:
            if self.choice_lock.blocks(cands_txt) and not self._is_different_event(cands_txt, kind):
                log(f"[완료 이벤트 잠금] {cands_txt} 는 방금 선택을 마친 '{self.choice_lock.title}' 와 같아 무시")
                return
            log(f"[완료 이벤트 잠금 해제] 다른 제목 {cands_txt}")
            self.choice_lock.release()
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
            t = CardThread(title, ids, save_debug=bool(self.settings.get("show_capture_debug")))
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
        diff = list(self.settings.get("hidden_difficulties") or [])
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
            log("[전역 로딩 화면 감지] 이전 이벤트 상태 전체 초기화")
            self.choice_lock.release()
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
        self._check_lock_expiry()
        if not self._hidden:
            reassert_topmost(self.status)       # 게임 창이 위로 올라와 상단바가 가려지는 경우(2026-10-16 실기) 매초 복구
            self.status.reposition()

    # --- 호버 ---
    def check_hover(self):
        if not self._hidden and not self._loading_screen and is_game_hovered_or_active():
            sched = self._schedule_hover_html(get_game_window_rect(), QCursor.pos())
            if sched:
                self.tooltip.show_html(sched)
                return
            if self._savior_click_hold():                 # 클릭 직후 0.5초: 카드 고르기 화면으로 넘어가는 중일 수 있어 숨김
                self.tooltip.hide_and_clear()
                return
            sv = self._savior_hover_html(get_game_window_rect(), QCursor.pos())
            if sv:
                if getattr(sv, "wide", False):               # 2세트 구원자: 좌측 스탯 패널까지 넓게(이름 블록 아래, 우측 편성 카드 왼쪽까지)
                    self.tooltip.show_html(sv, y_frac=jh_savior.WIDE_TOP_FRAC, x_frac=jh_savior.WIDE_X_FRAC,
                                           width_frac=jh_savior.WIDE_WIDTH_FRAC, min_top_frac=jh_savior.WIDE_TOP_FRAC)
                else:
                    self.tooltip.show_html(sv, y_frac=jh_savior.TOOLTIP_TOP_FRAC, x_frac=jh_savior.TOOLTIP_X_FRAC,
                                           width_px=getattr(sv, "width_px", None))
                return
        # DB에 없는 제목(예: '계승')은 상단바 문구로만 알리고 툴팁은 띄우지 않는다(선택지가 있는 이벤트만 툴팁)
        if self._hidden or self._loading_screen or not self.in_journey or not self.matched or not is_game_hovered_or_active():
            self.tooltip.hide_and_clear()
            return
        scale, show_info = self.settings["tooltip_font_scale"], self.settings["show_info"]
        if len(self.options) <= 1:                      # 선택지가 없거나 하나(자동 진행)면 툴팁 없음. 단 사용자 정의 안내문(note)은 표시
            self._prev_keys = set()
            if self.options and self.options[0].get("note"):
                self.tooltip.show_html(self._option_html(0, self.options[0], scale, show_info))
            else:
                self.tooltip.hide_and_clear()
            return
        rect, pos = get_game_window_rect(), QCursor.pos()
        idx = self._zone_index_at(pos, rect)
        self._dwell.update(idx)
        keys = self._read_input_keys() if self.zones else set()
        prev, self._prev_keys = self._prev_keys, keys
        if self.zones and time.monotonic() >= self._click_block_until:
            sel = detect_choice_input(keys, prev, idx if self._dwell.dwelled() else None, len(self.zones))
            if sel is not None:
                self.on_choice_selected(sel)
                if not self.zones:                      # 조건 없는 선택지 -> 이미 정리됨
                    return
        if idx is not None and not (self._pending_hide and self.choice_lock.active):
            z = self.zones[idx]
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
        m.addAction(tr("🎯 인식 상태 초기화"), self.manual_reset)
        m.addAction(tr("🔄 DB 새로고침 (10분 쿨다운)"), self.refresh_db)
        m.addAction(tr("▶️ 헬퍼 재개") if self._hidden else tr("⏸️ 헬퍼 일시정지"), self._toggle_hidden)
        m.addSeparator()
        m.addAction(tr("⚙️ 설정 열기"), self.open_settings)
        m.addAction(tr("🛑 프로그램 종료"), self.quit_app)
        return m

    def open_settings(self, *_):
        """설정창(단일 인스턴스, 항상 위). 이미 있으면 값을 새로 반영하고 앞으로 가져온다. 닫아도 앱은 유지."""
        try:
            dlg = getattr(self, "_settings_dlg", None)
            if dlg is None:
                import jh_settings_ui
                dlg = self._settings_dlg = jh_settings_ui.SettingsDialog(self, sys.modules[__name__])
            dlg.refresh()
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            exclude_from_capture(dlg, not self.settings.get("show_capture_debug", False))
        except Exception as e:
            log(f"[설정창 열기 실패] {type(e).__name__}: {e}")

    def build_settings_menu(self):
        m = QMenu()
        m.addAction(tr("⚙️ 설정")).setEnabled(False)
        scale_menu = m.addMenu(tr("🔤 왼쪽 툴팁 글자 크기"))
        group = QActionGroup(m)
        for label, val in FONT_SCALE_CHOICES:
            a = QAction(tr(label), m, checkable=True)
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
        diff_menu = m.addMenu(tr("🎚️ 툴팁에서 숨길 난이도"))
        hidden = set(self.settings.get("hidden_difficulties") or [])
        for label, val in DIFFICULTY_CHOICES[1:]:
            a = QAction(tr(label), m, checkable=True)
            a.setChecked(val in hidden)
            a.triggered.connect(lambda checked, v=val: self.set_value(
                "hidden_difficulties", toggle_hidden_difficulty(self.settings.get("hidden_difficulties"), v, checked)))
            diff_menu.addAction(a)
        lang_menu = m.addMenu(tr("🌐 언어"))
        g3 = QActionGroup(m)
        for label, val in (("한국어", "ko"), ("English", "en")):
            a = QAction(label, m, checkable=True)
            a.setChecked(self.lang == val)
            a.triggered.connect(lambda _, v=val: self.on_language_selected(v))
            g3.addAction(a)
            lang_menu.addAction(a)
        for text, key in ((tr("잠재력/버프 설명 표시"), "show_info"), (tr("📅 일정 미리보기 (좌측 상단 날짜에 마우스)"), "show_schedule"),
                          (tr("🃏 구원자 추천 아르카나 (여정 시작 화면에서 표시)"), "show_savior_reco"),
                          (tr("⚡ 저사양 모드 (OCR 주기 늘림)"), "low_spec_mode"),
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
        main = self.build_main_menu()                 # 메인 항목 + '설정 열기' + 종료 (서브메뉴 없음)
        self._tray_keep = [main]                      # 액션의 소유자(메뉴)를 살려 둬야 한다 (GC 되면 크래시)
        for a in main.actions():
            m.addAction(a)

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
        self.tooltip.avoid_regions = self.status.avoid_regions = not hide

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
        threads = [self.watcher] + list(self._db_threads) + list(self._card_threads) + list(self._thumb_threads)
        self.stuck_threads = join_threads(threads, 3000)
        if self.stuck_threads:
            log(f"[종료] {len(self.stuck_threads)}개 스레드가 시간 안에 끝나지 않음 -> 프로세스 종료로 정리")
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
        # 휴대형 data 폴더가 ASCII 면 거기(data\qt_plugins)에 복사. 아니면(한글 경로 앱 폴더) 기존처럼 ASCII 경로 폴백.
        bases = [(jh_paths.data_dir(), "qt_plugins")]
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PUBLIC"), os.environ.get("ProgramData"),
                     (os.environ.get("SystemDrive") or "C:") + "\\"):
            bases.append((base, os.path.join(APP_NAME, "qt_plugins")))
        for base, rel in bases:
            if not base or not base.isascii():
                continue
            dst = os.path.join(base, rel)
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
    jh_paths.log_dir()                      # log\ 와 안내 파일 보장(폴백 판정·이전도 여기서)
    set_language(load_settings()["language"])
    prepare_qt_plugins()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    manager = MouseHoverManager()
    rc = app.exec_()
    if getattr(manager, "stuck_threads", None):
        os._exit(rc)           # 아직 도는 QThread 가 있는 채 파괴되면 abort 되므로 바로 종료한다
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
