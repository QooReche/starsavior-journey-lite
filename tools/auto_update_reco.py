# -*- coding: utf-8 -*-
"""구원자 추천 JSON 자동 갱신 (GitHub Actions 용, 로컬에서도 실행 가능).

가이드 사이트의 app-core.js / app.js / arcanas.json 3개를 받아 extract_savior_reco 로 뽑고,
현재 assets/savior_reco.json 과 (generated 날짜를 빼고) 같으면 아무것도 하지 않는다.
달라졌으면 앱이 받을 때 쓰는 검증(jh_reco.validate)과 추가 조건을 통과해야만 파일을 덮어쓴다.

종료 코드: 0 변경 없음 / 10 갱신함(파일 덮어씀) / 1 실패(파일은 그대로)
사용: python -X utf8 tools/auto_update_reco.py [--site-dir <이미 받은 폴더>] [--work <받을 폴더>]
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import extract_savior_reco as ex  # noqa: E402

BASE = "https://starsavior-guide.github.io/"
FILES = ("app-core.js", "app.js", "data/arcanas/arcanas.json")
UA = "starsavior-journey-lite-reco-bot (+https://github.com/QooReche/starsavior-journey-lite)"
TARGET = os.path.join(ROOT, "assets", "savior_reco.json")


def fail(msg):
    print("실패:", msg)
    return 1


def download(work):
    for rel in FILES:
        req = urllib.request.Request(BASE + rel, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
        if len(body) < 1000:
            raise ValueError(f"{rel}: 응답이 너무 작음({len(body)}B)")
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)
    with open(os.path.join(work, "data", "arcanas", "arcanas.json"), "rb") as src, open(os.path.join(work, "arcanas.json"), "wb") as dst:
        dst.write(src.read())            # 추출기는 루트의 arcanas.json 을 읽는다


def strip_generated(d):
    return {k: v for k, v in d.items() if k != "generated"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir")
    ap.add_argument("--work", default=os.path.join(ROOT, "_site_copy"))
    a = ap.parse_args()
    site = a.site_dir or a.work
    if not a.site_dir:
        try:
            download(site)
        except Exception as e:
            return fail(f"사이트 받기: {type(e).__name__} {str(e)[:120]}")

    def rd(n):
        with open(os.path.join(site, n), "r", encoding="utf-8-sig") as f:
            return f.read()
    logic = ex.run_site_logic(site)
    if not logic:
        return fail("node 로 사이트 JS 를 실행하지 못함(포팅값으로 대체하지 않는다)")
    try:
        data, R = ex.build(rd("app-core.js"), rd("app.js"), json.loads(rd("arcanas.json")), logic)
    except Exception as e:
        return fail(f"추출 예외: {type(e).__name__} {str(e)[:160]}")
    if R.unresolved:
        return fail(f"이름 해석 실패: {sorted(set(R.unresolved))[:10]}")
    errs = [k for k, v in logic.items() if isinstance(v, dict) and "error" in v]
    if errs:
        return fail(f"사이트 JS 실행 중 오류가 난 구원자: {errs[:10]}")

    with open(TARGET, "r", encoding="utf-8-sig") as f:
        cur = json.load(f)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new = ex.stamp(data, max(today, cur.get("generated") or ""))
    if strip_generated(new) == strip_generated(cur):
        print("변경 없음")
        return 0

    import jh_reco
    try:
        jh_reco.validate(new, jh_reco.summarize(cur))
    except Exception as e:
        return fail(f"앱 검증 불통과: {type(e).__name__} {str(e)[:160]}")
    old_keys = {s["key"] for s in cur["saviors"]}
    gone = old_keys - {s["key"] for s in new["saviors"]}
    if gone:
        return fail(f"기존 구원자가 사라짐: {sorted(gone)[:10]}")
    lost = [s["key"] for s in cur["saviors"] if ex.has_reco(s) and not ex.has_reco(next(x for x in new["saviors"] if x["key"] == s["key"]))]
    if lost:
        return fail(f"추천이 있던 구원자가 미정이 됨: {lost[:10]}")

    with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        json.dump(new, f, ensure_ascii=False, separators=(",", ":"))
    added = sorted(old_keys ^ {s["key"] for s in new["saviors"]})
    print(f"갱신함: 구원자 {len(cur['saviors'])} -> {len(new['saviors'])}, 추가 {added}, generated {new['generated']}")
    return 10


if __name__ == "__main__":
    sys.exit(main())
