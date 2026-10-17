# -*- coding: utf-8 -*-
"""빌드 직전 실행: jh_version.py 의 BUILD_DATE 를 오늘 날짜(로컬, YYMMDD)로 덮어쓴다.
사용: python -X utf8 tools/stamp_version.py [--date 261017] [--file <jh_version.py 경로>]  -> 결과 버전을 출력"""
import os
import re
import sys
import time


def stamp(path, date=None):
    date = date or time.strftime("%y%m%d")
    if not re.fullmatch(r"\d{6}", date):
        raise ValueError("date must be YYMMDD")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    new, n = re.subn(r'^BUILD_DATE\s*=\s*"[^"]*"', f'BUILD_DATE = "{date}"', src, flags=re.M)
    if n != 1:
        raise RuntimeError("BUILD_DATE line not found")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    base = re.search(r'^BASE_VERSION\s*=\s*"([^"]+)"', new, flags=re.M).group(1)
    return f"{base}.{date}"


def main(argv):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jh_version.py")
    date = None
    if "--file" in argv:
        path = argv[argv.index("--file") + 1]
    if "--date" in argv:
        date = argv[argv.index("--date") + 1]
    print(stamp(path, date))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
