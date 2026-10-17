# -*- coding: utf-8 -*-
"""휴대형(portable) 데이터 위치: 설정/캐시/로그를 모두 앱 폴더 안에 둔다.

    <앱폴더>\\data\\   settings.json, cache\\ (DB/V2/카드 썸네일), custom_events.json, card_debug\\, qt_plugins\\
    <앱폴더>\\log\\    log.txt, log.old.txt, crash_log.txt
앱 폴더 = frozen 이면 실행 파일(sys.executable) 폴더, 개발 실행이면 이 소스 폴더.
앱 폴더에 쓸 수 없으면 %APPDATA%\\JourneyHelperLite (구 위치)로 폴백한다.
(번들 읽기 전용 데이터는 data\\ 가 아니라 assets\\ -> _internal\\assets 에 있어 이름이 겹치지 않는다.)
data\\ 나 log\\ 가 실행 중에 지워져도 접근할 때마다 다시 만든다. 테스트는 환경변수 JH_APP_DIR 로 앱 폴더를 바꾼다.
이 모듈은 Qt/journey_helper 를 import 하지 않는다 (순환 방지).
"""
import os
import shutil
import sys
import threading
import time

APP_NAME = "JourneyHelperLite"
LOG_MAX_BYTES = 1024 * 1024
README_NAME = "README_먼저읽어주세요.txt"
LANG = "ko"                      # journey_helper.set_language 가 갱신 (README 문구 언어)

README_TEXT = {
    "log": {
        "ko": ("이 폴더는 JourneyHelperLite 의 진단용 로그(log.txt, 지난 로그 log.old.txt)가 저장되는 곳입니다.\r\n"
               "문제가 생겼을 때 개발자에게 전달하는 용도이며, 게임 창 정보와 화면 인식 결과만 기록됩니다.\r\n"
               "용량이 차지한다고 느껴지면 이 폴더의 파일을 지워도 됩니다(프로그램이 다시 만듭니다).\r\n"
               "로그는 일정 크기를 넘으면 자동으로 정리됩니다.\r\n"),
        "en": ("This folder holds JourneyHelperLite's diagnostic logs (log.txt, and the previous log.old.txt).\r\n"
               "They are meant to be sent to the developer when something goes wrong; only game-window info and recognition results are recorded.\r\n"
               "You may delete the files here to free space (the app recreates them).\r\n"
               "Logs are trimmed automatically once they exceed a size limit.\r\n"),
    },
    "data": {
        "ko": ("이 폴더에는 설정(settings.json), 내려받은 이벤트 데이터와 카드 이미지 캐시(cache), 사용자 정의 이벤트(custom_events.json)가 저장됩니다.\r\n"
               "설정을 초기화하고 싶으면 settings.json 을 지우면 되고, 용량이 커졌다고 느껴지면 cache 폴더를 지워도 됩니다(다시 내려받습니다).\r\n"
               "이 폴더 전체를 지워도 프로그램은 정상 동작합니다(설정은 기본값으로 돌아갑니다).\r\n"),
        "en": ("This folder stores your settings (settings.json), downloaded event data and card-image cache (cache), and custom events (custom_events.json).\r\n"
               "Delete settings.json to reset settings; delete the cache folder if it grows large (it is downloaded again).\r\n"
               "Deleting this whole folder is safe: the app keeps working (settings return to defaults).\r\n"),
    },
}

_lock = threading.RLock()
_state = {}          # app_dir -> {"mode": "portable"|"fallback"}
_notes = []          # 로그에 한 줄씩 남길 안내 (마이그레이션/폴백)


def app_dir():
    env = os.environ.get("JH_APP_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def legacy_dir():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".jh_write_test.tmp")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def _copy_missing(src, dst):
    """src -> dst 로 복사하되 이미 있는 파일은 덮어쓰지 않는다 (원본은 건드리지 않음)."""
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            _copy_missing(os.path.join(src, name), os.path.join(dst, name))
    elif os.path.isfile(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)


def migrate_legacy(data, legacy=None):
    """새 data\\ 에 settings.json 이 없고 구 폴더가 있으면 settings.json·custom_events.json·cache\\ 를 복사. 했으면 True."""
    legacy = legacy or legacy_dir()
    try:
        if os.path.exists(os.path.join(data, "settings.json")) or not os.path.isdir(legacy):
            return False
        if os.path.abspath(legacy) == os.path.abspath(data):
            return False
        done = []
        for name in ("settings.json", "custom_events.json", "cache"):
            s = os.path.join(legacy, name)
            if os.path.exists(s):
                _copy_missing(s, os.path.join(data, name))
                done.append(name)
        if done:
            _notes.append("[이전] 기존 설정 폴더에서 새 data 폴더로 복사함 (" + ", ".join(done) + "); 원본은 그대로 둠")
        return bool(done)
    except Exception as e:
        _notes.append(f"[이전 실패] {e}")
        return False


def mode():
    """'portable' 또는 'fallback'. 앱 폴더별로 처음 한 번만 판정(쓰기 시험 + 마이그레이션)."""
    root = app_dir()
    with _lock:
        st = _state.get(root)
        if st is None:
            data, logd = os.path.join(root, "data"), os.path.join(root, "log")
            if _writable(data) and _writable(logd):
                st = {"mode": "portable"}
                _state[root] = st
                migrate_legacy(data)
            else:
                st = {"mode": "fallback"}
                _state[root] = st
                _notes.append("[경로] 앱 폴더에 쓸 수 없어 %APPDATA%\\" + APP_NAME + " 를 사용합니다")
        return st["mode"]


def is_portable():
    return mode() == "portable"


def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def ensure_readme(folder, kind):
    """폴더에 안내 텍스트가 없으면 만든다 (UTF-8 BOM, 메모장 호환). 실패는 무시."""
    try:
        p = os.path.join(folder, README_NAME)
        if not os.path.isfile(p):
            texts = README_TEXT[kind]
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                f.write(texts.get(LANG, texts["ko"]))
    except Exception:
        pass


def data_dir():
    m = mode()
    d = os.path.join(app_dir(), "data") if m == "portable" else legacy_dir()
    ensure_dir(d)
    if m == "portable":
        ensure_readme(d, "data")
    return d


def log_dir():
    m = mode()
    d = os.path.join(app_dir(), "log") if m == "portable" else legacy_dir()
    ensure_dir(d)
    if m == "portable":
        ensure_readme(d, "log")
    return d


def cache_dir():
    return ensure_dir(os.path.join(data_dir(), "cache"))


def seed_dir():
    """번들 DB 시드 폴더(읽기 전용, assets/seed). 환경변수 JH_SEED_DIR 가 있으면 그것(테스트용). 없으면 None 가능."""
    env = os.environ.get("JH_SEED_DIR")
    if env is not None:
        return env or None
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", "seed")


def log_path():
    return os.environ.get("JH_LOG_PATH") or os.path.join(log_dir(), "log.txt")


def old_log_path(path=None):
    base, ext = os.path.splitext(path or log_path())
    return base + ".old" + ext


def rotate_if_big(path, limit=None):
    try:
        if os.path.getsize(path) > (limit or LOG_MAX_BYTES):
            os.replace(path, old_log_path(path))
    except OSError:
        pass


def write_log(line):
    """한 줄(이미 시각 포함)을 log.txt 에 추가. 어떤 실패도 예외를 내지 않는다."""
    try:
        with _lock:
            path = log_path()               # (먼저 판정해야 안내 _notes 가 쌓인다)
            lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')} {n}" for n in _notes] + [line]
            del _notes[:]
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            rotate_if_big(path)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 표시 / 용량 / 지우기
def display_path(kind):
    """사용자 이름이 보이지 않는 상대 표기. kind: 'data' | 'log' | 'cache'"""
    if is_portable():
        return {"data": "앱 폴더\\data", "log": "앱 폴더\\log", "cache": "앱 폴더\\data\\cache"}[kind]
    return "%APPDATA%\\" + APP_NAME + ("\\cache" if kind == "cache" else "")


def dir_size(path, skip_readme=True):
    total = 0
    for root, _dirs, files in os.walk(path):
        for n in files:
            if skip_readme and n == README_NAME:
                continue
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def fmt_size(n):
    return f"{n / 1024 / 1024:.1f}MB" if n >= 1024 * 1024 else f"{n / 1024:.0f}KB"


def usage_summary():
    """진단용: 데이터 위치 종류와 용량 요약 (경로 없음). README 는 용량에 넣지 않는다."""
    m = mode()
    cache = dir_size(os.path.join(data_dir(), "cache"))
    logs = 0
    for p in (log_path(), old_log_path()):
        try:
            logs += os.path.getsize(p)
        except OSError:
            pass
    data_total = dir_size(data_dir())
    return (f"data location: {'portable (app folder)' if m == 'portable' else 'fallback (%APPDATA%)'}; "
            f"data={fmt_size(data_total)} (cache={fmt_size(cache)}), log={fmt_size(logs)}")


def clear_cache():
    """data\\cache 를 비운다 (설정/사용자 정의 이벤트는 보존). 폴더는 다시 만든다. 성공 여부 반환."""
    d = os.path.join(data_dir(), "cache")
    ok = True
    try:
        shutil.rmtree(d)
    except FileNotFoundError:
        pass
    except Exception:
        ok = False
    ensure_dir(d)
    return ok


def clear_logs():
    ok = True
    for p in (log_path(), old_log_path(), os.path.join(os.path.dirname(log_path()), "crash_log.txt")):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except Exception:
            ok = False
    return ok


def reset_for_tests():
    with _lock:
        _state.clear()
        del _notes[:]
