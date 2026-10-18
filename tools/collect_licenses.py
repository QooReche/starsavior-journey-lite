# -*- coding: utf-8 -*-
"""배포 폴더(dist\\JourneyNavigator)에 라이선스 고지문을 채운다 (GPL-3.0 / Apache-2.0 / MIT 등이 사본 동봉을 요구).

사용: python -X utf8 tools\\collect_licenses.py <dist 폴더> [--site <venv site-packages>] [--src <소스 폴더>]
만드는 것:
  <dist>\\LICENSE                    이 프로그램의 라이선스(GPL-3.0)
  <dist>\\README.md                  사용자용 안내
  <dist>\\THIRD_PARTY_NOTICES.txt    번들한 라이브러리/모델/데이터의 이름·버전·라이선스·출처
  <dist>\\licenses\\<패키지>\\...     각 라이브러리가 배포하는 LICENSE/NOTICE 원문
빌드 스크립트(tools\\build_release.ps1)가 PyInstaller 직후에 호출하고, make_release_zip.ps1 이 위 파일이 있는지 검사한다.
"""
import argparse
import glob
import os
import re
import shutil
import sys
from importlib import metadata as md

# 배포 zip 안에 실제로 들어가는 런타임 패키지 (개발/빌드 도구는 제외). 이름은 정규화(소문자, -/. -> _)한 값.
RUNTIME = ["pyqt5", "pyqt5_qt5", "pyqt5_sip", "numpy", "opencv_python", "pillow", "pyclipper", "shapely", "mss",
           "requests", "urllib3", "certifi", "charset_normalizer", "idna", "protobuf", "pyyaml", "six", "tqdm", "colorama",
           "onnxruntime", "rapidocr_onnxruntime"]
LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*", "THIRD*PARTY*", "ThirdParty*", "AUTHORS*")
# 패키지가 원문을 따로 배포하지 않을 때를 위한 알려진 정보 (정식 이름, 라이선스, 주소)
KNOWN = {
    "onnxruntime": ("onnxruntime", "MIT", "https://github.com/microsoft/onnxruntime"),
    "rapidocr_onnxruntime": ("rapidocr-onnxruntime (RapidOCR)", "Apache-2.0", "https://github.com/RapidAI/RapidOCR"),
    "pyqt5": ("PyQt5", "GPL-3.0 (또는 상용)", "https://www.riverbankcomputing.com/software/pyqt/"),
}
EXTRA = """
[함께 들어 있는 모델과 데이터]
- OCR 모델 (models\\): PaddleOCR PP-OCRv4 한국어 인식 모델 (ONNX 변환본), Apache-2.0
  https://github.com/PaddlePaddle/PaddleOCR , https://github.com/RapidAI/RapidOCR
- 이벤트·아르카나 데이터 시드 (assets\\seed): starsavior-guide.github.io 와 starsavior-journey-choice.pages.dev 의 공개 데이터 스냅샷
- 구원자 추천 데이터 (assets\\savior_reco.json): starsavior-guide.github.io 의 공개 정보를 가공한 것
- 일정·여정 버프 설명 일부: star-savior-arcana-db.pages.dev (옛 DB, 2026-10-15 백업), starsavior-databook.pages.dev 로 확인·보정
- 게임 관련 이름·이미지 등의 권리는 STUDIOBSIDE 와 각 권리자에게 있습니다. 비공식 팬 도구입니다.

[원작]
- p1021/journeyhelper (https://github.com/p1021/journeyhelper): 화면 영역 비율, 호버 방식 등 아이디어와 일부 코드를 참고했습니다.
  원작에서 가져온 부분의 권리는 원작자에게 있습니다.

[이 프로그램]
- 코드는 GPL-3.0 입니다 (LICENSE 파일). 소스: https://github.com/QooReche/starsavior-journey-lite
"""


def norm(name):
    return re.sub(r"[-_.]+", "_", name).lower()


def find_dist(name):
    for d in md.distributions():
        if norm(d.metadata["Name"] or "") == name:
            return d
    return None


def license_files(d, site):
    """패키지가 배포하는 라이선스 파일 경로 목록 (dist-info 와 최상위 패키지 폴더 1단계)."""
    found = []
    di = os.path.join(site, f"{d.metadata['Name'].replace('-', '_')}-{d.version}.dist-info")
    cands = glob.glob(os.path.join(site, "*.dist-info"))
    for c in cands:
        if norm(os.path.basename(c).rsplit("-", 1)[0]) == norm(d.metadata["Name"]):
            di = c
            break
    for base in (di, os.path.join(di, "licenses")):
        for pat in LICENSE_GLOBS:
            found += [p for p in glob.glob(os.path.join(base, pat)) if os.path.isfile(p)]
    for top in (d.read_text("top_level.txt") or "").split():
        pkg = os.path.join(site, top)
        if os.path.isdir(pkg):
            for pat in LICENSE_GLOBS:
                found += [p for p in glob.glob(os.path.join(pkg, pat)) if os.path.isfile(p)]
    return list(dict.fromkeys(found))


def license_name(d):
    meta = d.metadata
    lic = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if not lic or len(lic) > 60 or "\n" in lic:
        cl = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = ", ".join(cl) or lic.split("\n")[0][:60] or "(패키지 메타데이터 참고)"
    return lic


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("dist")
    ap.add_argument("--site", default=None)
    ap.add_argument("--src", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args(argv)
    site = a.site or next(p for p in sys.path if p.endswith("site-packages"))
    dist = os.path.abspath(a.dist)
    if not os.path.isfile(os.path.join(dist, "JourneyNavigator.exe")):
        print("빌드 결과를 찾지 못했습니다:", dist)
        return 1
    # 이 프로그램의 LICENSE / README
    for fn in ("LICENSE", "README.md"):
        shutil.copyfile(os.path.join(a.src, fn), os.path.join(dist, fn))
    lic_root = os.path.join(dist, "licenses")
    if os.path.isdir(lic_root):
        shutil.rmtree(lic_root)
    os.makedirs(lic_root)
    lines = ["이 프로그램에 포함된 제3자 구성요소의 고지문입니다.", "각 라이브러리의 라이선스 원문은 licenses 폴더에 있습니다.", "",
             "[라이브러리]"]
    missing = []
    for key in RUNTIME:
        d = find_dist(key)
        if d is None:
            if key in KNOWN:
                nm, lic, url = KNOWN[key]
                lines.append(f"- {nm}: {lic} - {url}")
                missing.append(key)
            continue
        nm = d.metadata["Name"]
        lic = license_name(d)
        home = (d.metadata.get("Home-page") or "").strip()
        if not home:
            for u in d.metadata.get_all("Project-URL") or []:
                if "ome" in u.split(",")[0] or "ource" in u.split(",")[0]:
                    home = u.split(",", 1)[1].strip()
                    break
        if key in KNOWN:
            lic, home = KNOWN[key][1], KNOWN[key][2]
        files = license_files(d, site)
        if files:
            out = os.path.join(lic_root, norm(nm))
            os.makedirs(out, exist_ok=True)
            for f in files:
                shutil.copyfile(f, os.path.join(out, os.path.basename(f)))
        else:
            missing.append(key)
        lines.append(f"- {nm} {d.version}: {lic}" + (f" - {home}" if home else "") + ("" if files else "  (원문 파일 없음: 위 라이선스 명칭과 주소 참고)"))
    # 원문을 따로 배포하지 않는 패키지(RapidOCR, OCR 모델: Apache-2.0)를 위해 표준 Apache-2.0 원문을 함께 둔다(requests 가 같은 원문을 배포).
    req = os.path.join(lic_root, "requests", "LICENSE")
    if os.path.isfile(req):
        shutil.copyfile(req, os.path.join(lic_root, "Apache-2.0.txt"))
    lines += ["", "[원문 안내]",
              "- PyQt5: 이 프로그램이 GPL-3.0 으로 배포되므로 LICENSE 파일(GPL-3.0 원문)을 따릅니다.",
              "- RapidOCR, OCR 모델(Apache-2.0): licenses\\Apache-2.0.txt"]
    text = "\n".join(lines) + "\n" + EXTRA
    with open(os.path.join(dist, "THIRD_PARTY_NOTICES.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    n = sum(len(f) for _, _, f in os.walk(lic_root))
    print(f"licenses: {n} files, notices: {len(lines) - 4} libraries" + (f", 원문 없음: {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
