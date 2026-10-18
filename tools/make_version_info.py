# -*- coding: utf-8 -*-
"""PyInstaller 용 Windows 버전 정보(파일 속성 > 자세히)를 만든다.

버전 정보가 없는 PyInstaller 실행 파일은 백신의 머신러닝 휴리스틱에서 의심 점수를 더 받는 것으로 알려져 있어
(서명 없음, 자체 압축 해제와 함께 오탐의 흔한 원인), 제품명·설명·버전·라이선스를 파일에 넣는다.

사용: python -X utf8 tools\\make_version_info.py <출력 파일>      (build_release.ps1 이 PyInstaller 직전에 호출, 소스 폴더 기준 실행)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jh_version import APP_VERSION, BASE_VERSION  # noqa: E402
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,  # noqa: E402
                                                 VarFileInfo, VarStruct, VSVersionInfo)


def build():
    # 숫자 버전은 16비트라 빌드 날짜(261018)를 못 담는다: (1, 0, 2, 0). 전체 버전은 문자열 항목에 둔다.
    nums = [int(x) for x in BASE_VERSION.split(".")] + [0, 0, 0, 0]
    tup = tuple(nums[:4])
    strings = [
        ("CompanyName", "QooReche (비공식 팬 프로젝트)"),
        ("FileDescription", "여정 내비게이터 - 스타세이비어 PC용 여정 도우미 (비공식 팬 도구)"),
        ("FileVersion", APP_VERSION),
        ("InternalName", "JourneyNavigator"),
        ("LegalCopyright", "GPL-3.0 | 게임 관련 권리는 STUDIOBSIDE 와 각 권리자에게 있음"),
        ("OriginalFilename", "JourneyNavigator.exe"),
        ("ProductName", "여정 내비게이터"),
        ("ProductVersion", APP_VERSION),
        ("Comments", "https://github.com/QooReche/starsavior-journey-lite"),
    ]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tup, prodvers=tup, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("041204b0", [StringStruct(k, v) for k, v in strings])]),
              VarFileInfo([VarStruct("Translation", [0x0412, 1200])])])


def main(out):
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(str(build()))
    print("version info:", out, APP_VERSION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
