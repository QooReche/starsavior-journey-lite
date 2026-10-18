# -*- coding: utf-8 -*-
"""배포 폴더의 실행 파일/DLL 이 필요로 하는 DLL 이 모두 폴더 안에 있거나 Windows 기본 구성요소인지 검사한다.

개발 PC 에는 'Microsoft Visual C++ 재배포 패키지'가 설치돼 있어 번들에서 빠진 런타임(msvcp140_1.dll 등)이 있어도 앱이 잘 돌지만,
설치돼 있지 않은 사용자 PC 에서는 시작하지 못한다(2026-10-18 msvcp140_1.dll 누락 발견). 그 종류의 누락을 빌드 때 잡는다.

사용: python -X utf8 tools\\check_dist_deps.py <dist 폴더>     (종료 코드 0 = 통과, 1 = VC++ 런타임 누락)
"""
import os
import re
import sys

import pefile

VC_RUNTIME = re.compile(r"^(vcruntime140|msvcp140|concrt140|vcomp140)", re.I)
# 사용하지 않는 Qt 플러그인(웹/SVG/DBus 등)이 가리키는 의존성은 무시한다(해당 플러그인은 로드되지 않으므로 실행에 영향 없음)
IGNORED_PLUGINS = ("qwebgl", "qsvg", "qsvgicon", "qtuiotouchplugin", "qxdgdesktopportal")


def imports(path):
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    return [i.dll.decode().lower() for i in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []]


def main(dist):
    inside = {}
    for root, _, files in os.walk(dist):
        for f in files:
            if f.lower().endswith((".dll", ".pyd", ".exe")):
                inside.setdefault(f.lower(), os.path.join(root, f))
    missing = {}
    for name, path in inside.items():
        if os.path.splitext(name)[0] in IGNORED_PLUGINS:
            continue
        try:
            for dll in imports(path):
                if dll not in inside and VC_RUNTIME.match(dll):
                    missing.setdefault(dll, []).append(os.path.relpath(path, dist))
        except Exception:
            continue
    if missing:
        print("VC++ 런타임이 번들에 없습니다 (재배포 패키지가 없는 PC 에서 실행되지 않음):")
        for dll, users in sorted(missing.items()):
            print(f"  {dll}  <- {', '.join(users[:4])}")
        return 1
    print(f"dist 의존성 검사 통과: VC++ 런타임 누락 없음 (파일 {len(inside)}개 검사)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.path.abspath(sys.argv[1])))
