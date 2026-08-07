"""
🔔 새 버전 알림 모듈 (자동 다운로드/교체 없음 - 수동 업데이트 방식)

사용법:
    if __name__ == '__main__':
        from updater import check_for_updates
        check_for_updates()   # 새 버전이 있으면 알림창만 띄움. 프로그램은 그대로 계속 실행됨.
        app = QApplication(sys.argv)
        ...

동작 방식:
    1. VERSION_MANIFEST_URL에서 최신 버전 정보(version.json)를 가져온다.
    2. 서버 버전이 내 CURRENT_VERSION보다 높으면, Windows 기본 알림창(MessageBox)을 띄운다.
    3. 사용자가 "예"를 누르면 다운로드 페이지를 기본 브라우저로 열어준다.
    4. 자동으로 파일을 다운로드하거나 교체하지 않는다 — 사용자가 직접 새 zip을 받아서
       기존 폴더에 덮어써야 한다. (자기 파일을 자동으로 바꿔치기하는 동작은 백신이
       악성코드처럼 오탐하는 경우가 많아서, 안전하게 "알림만" 하는 방식으로 만들었다.)

주의:
    - .py로 직접 실행 중(개발 모드)일 때는 업데이트 확인을 건너뛴다 (exe로 빌드된 경우에만 동작).
    - 네트워크 문제 등으로 실패해도 예외를 삼키고 조용히 넘어간다 — 업데이트 확인 실패가
      프로그램 실행 자체를 막으면 안 되기 때문.
"""
import sys
import os
import ctypes
import webbrowser

import requests

# ⚙️ [배포할 때마다 여기 버전만 올리면 됨]
CURRENT_VERSION = "1.0.0"
VERSION_MANIFEST_URL = "https://raw.githubusercontent.com/p1021/journeyhelper/main/version.json"

# version.json 형식 예시:
# {
#   "version": "1.0.1",
#   "download_page": "https://github.com/p1021/journeyhelper/releases",
#   "notes": "동명 이벤트 카드 매칭 오류 수정"
# }


def _parse_version(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


def _is_frozen():
    # PyInstaller로 빌드된 exe에서 실행 중이면 True
    return bool(getattr(sys, "frozen", False))


def check_for_updates(timeout=5, silent=True):
    """
    새 버전이 있는지 확인만 한다. 있으면 알림창을 띄우고, 사용자가 "예"를 누르면
    다운로드 페이지를 브라우저로 열어준다. 자동으로 파일을 받거나 교체하지 않는다.

    silent=True(기본, 시작 시 자동 확인용): 최신 버전이거나 확인 실패해도 조용히 넘어감.
    silent=False(설정창 "지금 확인" 버튼용): 최신 버전이면 그것도 알림창으로 알려준다.
    """
    if not _is_frozen():
        print("ℹ️ [업데이트] 개발 모드(.py 직접 실행)에서는 업데이트 확인을 건너뜁니다.")
        if not silent:
            try:
                ctypes.windll.user32.MessageBoxW(0, "개발 모드에서는 업데이트 확인을 지원하지 않습니다.", "업데이트 확인", 0x40)
            except Exception:
                pass
        return

    try:
        resp = requests.get(VERSION_MANIFEST_URL, timeout=timeout)
        resp.raise_for_status()
        manifest = resp.json()
    except Exception as e:
        print(f"⚠️ [업데이트 확인 실패] {e} → 그냥 현재 버전으로 계속 진행합니다.")
        if not silent:
            try:
                ctypes.windll.user32.MessageBoxW(0, f"업데이트 서버에 연결하지 못했습니다.\n{e}", "업데이트 확인 실패", 0x30)
            except Exception:
                pass
        return

    remote_version = manifest.get("version", "0.0.0")
    if _parse_version(remote_version) <= _parse_version(CURRENT_VERSION):
        print(f"✅ [버전 확인] 현재 최신 버전입니다 ({CURRENT_VERSION})")
        if not silent:
            try:
                ctypes.windll.user32.MessageBoxW(0, f"현재 최신 버전입니다. ({CURRENT_VERSION})", "업데이트 확인", 0x40)
            except Exception:
                pass
        return

    notes = manifest.get("notes", "")
    download_page = manifest.get("download_page", "https://github.com/p1021/journeyhelper/releases")
    print(f"🆕 [새 버전 발견] {CURRENT_VERSION} → {remote_version}")

    _show_update_notice(remote_version, notes, download_page)


def _show_update_notice(remote_version, notes, download_page):
    message = (
        f"새 버전 {remote_version}이 나왔습니다! (현재 버전: {CURRENT_VERSION})\n\n"
        f"{notes}\n\n"
        f"다운로드 페이지를 여시겠습니까?"
    )
    MB_YESNO = 0x04
    MB_ICONINFORMATION = 0x40
    IDYES = 6
    try:
        result = ctypes.windll.user32.MessageBoxW(0, message, "새 버전 알림", MB_YESNO | MB_ICONINFORMATION)
        if result == IDYES:
            webbrowser.open(download_page)
    except Exception as e:
        print(f"⚠️ [알림창 표시 실패] {e} (새 버전: {remote_version}, 다운로드: {download_page})")
